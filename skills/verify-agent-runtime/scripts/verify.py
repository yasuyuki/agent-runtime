#!/usr/bin/env python3
"""Install a checkout's agent-runtime into a disposable venv, launch a synthetic
vendor through its standard `codex` entry and a selected workspace-lifecycle,
and retain the proof."""

from __future__ import annotations

import argparse
from datetime import datetime, timezone
import hashlib
import json
import os
from pathlib import Path
import platform
import shutil
import subprocess
import sys
import tempfile


VENDOR_EXIT = 23
STDIN = "stdin 日本語\n"
VENDOR = """import json, os, pathlib, sys
data = {'cwd': os.getcwd(), 'argv': sys.argv[1:], 'stdin': sys.stdin.read()}
pathlib.Path(%r).open('a', encoding='utf-8').write(json.dumps(data) + '\\n')
print(json.dumps(data))
print('vendor-stderr', file=sys.stderr)
sys.exit(%d)
"""
EXPECTATIONS = {
    "install": "The checkout installs into a new venv; installed cli.py bytes equal the checkout's and --version equals pyproject.",
    "launch": "From the nested workspace directory `nested 日本語`, `codex exec \"two words\" 日本語` runs the vendor once in that directory "
              "with argv [--cd, <nested>, exec, two words, 日本語], the same stdin, its stderr and exit %d." % VENDOR_EXIT,
    "native-cwd": "From the workspace root, `codex -C \"nested 日本語\" exec x` runs the vendor in the root with argv unchanged.",
    "no-lifecycle-state": "The unmanaged workspace repository gains no workspace-lifecycle state.",
}
NOT_RUN = ["unmanaged launch", "launch plan (--explain)", "duplicate root refusal", "pin mismatch refusal",
           "HANDOFF receiver", "maintenance commands", "claude/grok entries", "managed lifecycle task",
           "real vendor CLI", "Ctrl+C/console signals"]
SKIP = shutil.ignore_patterns(".git", "build", "dist", "*.egg-info", "__pycache__", ".venv")


def sha(data):
    return hashlib.sha256(data).hexdigest()


def command(argv, cwd, **kwargs):
    return subprocess.run(argv, cwd=cwd, text=True, encoding="utf-8", errors="replace",
                          capture_output=True, **kwargs)


def doctor(repo, lifecycle):
    if sys.version_info < (3, 10):
        raise ValueError("Python 3.10 or newer is required")
    pyproject = repo / "pyproject.toml"
    if not pyproject.is_file() or 'name = "agent-runtime"' not in pyproject.read_text(encoding="utf-8"):
        raise ValueError("--repo is not an agent-runtime checkout: " + str(repo))
    if not lifecycle.is_absolute() or not lifecycle.is_file():
        raise ValueError("--lifecycle must be an existing absolute workspace-lifecycle executable")
    if shutil.which("git") is None:
        raise ValueError("Git is required")
    version = command([str(lifecycle), "--version"], repo)
    if version.returncode:
        raise ValueError("workspace-lifecycle --version failed: " + version.stderr.strip())
    revision = command(["git", "rev-parse", "HEAD"], repo)
    status = command(["git", "status", "--porcelain", "--untracked-files=normal"], repo)
    readable = not (revision.returncode or status.returncode)
    inputs = [pyproject, *sorted((repo / "src").rglob("*.py"))]
    return {
        "revision": revision.stdout.strip() if readable else "unknown",
        "dirty": bool(status.stdout.strip()) if readable else "unknown",
        "inputs_sha256": {p.relative_to(repo).as_posix(): sha(p.read_bytes()) for p in inputs},
        "helper_sha256": sha(Path(__file__).read_bytes()),
        "lifecycle": {"path": str(lifecycle), "sha256": sha(lifecycle.read_bytes()),
                      "version": version.stdout.strip()},
        "python": platform.python_version(), "platform": platform.platform(),
        "entry_point": "installed standard-name `codex` console entry with AGENT_RUNTIME_CONFIG",
    }


def run(repo, lifecycle, evidence_root=None, doctor_only=False, agent="unknown"):
    repo, lifecycle = Path(repo).resolve(), Path(lifecycle)
    if evidence_root is not None:
        evidence_root = Path(evidence_root).resolve()
        evidence_root.mkdir(parents=True, exist_ok=True)
    evidence = Path(tempfile.mkdtemp(prefix="agent-runtime-evidence-", dir=evidence_root))
    result = {
        "run_id": evidence.name, "started_at": datetime.now(timezone.utc).isoformat(),
        "status": "blocked", "feature": "transparent-launch", "feature_status": "not-run",
        "agent": agent, "expectations": EXPECTATIONS, "commands": [], "assertions": [],
        "artifacts": [], "not_run": NOT_RUN, "cleanup": {"ok": True, "scratch": None},
    }
    scratch = None

    def record():
        (evidence / "result.json").write_text(json.dumps(result, indent=2, ensure_ascii=False) + "\n",
                                              encoding="utf-8")

    def assertion(name, expected, observed):
        ok = observed == expected
        result["assertions"].append({"name": name, "expected": expected, "observed": observed, "ok": ok})
        record()
        if not ok:
            raise AssertionError(name)

    def execute(label, argv, cwd, **kwargs):
        proc = command(argv, cwd, **kwargs)
        result["commands"].append({"label": label, "argv": argv, "cwd": str(cwd),
                                   "exit_code": proc.returncode, "stdout": proc.stdout,
                                   "stderr": proc.stderr})
        record()
        return proc

    def vendor_calls():
        return [json.loads(line) for line in marker.read_text(encoding="utf-8").splitlines()] \
            if marker.exists() else []

    record()  # Expectations exist before the first operation.
    try:
        result["target"] = doctor(repo, lifecycle)
        if doctor_only:
            result["status"] = "not-run"
            return result, evidence
        scratch = Path(tempfile.mkdtemp(prefix="agent-runtime-drive-")).resolve()
        result["cleanup"]["scratch"] = str(scratch)
        source = scratch / "source"
        shutil.copytree(repo, source, ignore=SKIP)
        venv = scratch / "venv"
        if execute("venv", [sys.executable, "-m", "venv", str(venv)], scratch).returncode:
            raise ValueError("venv creation failed")
        bindir = venv / ("Scripts" if os.name == "nt" else "bin")
        suffix = ".exe" if os.name == "nt" else ""
        python = bindir / ("python" + suffix)
        if execute("install", [str(python), "-m", "pip", "install", "--no-deps", "--disable-pip-version-check",
                               "-q", str(source)], scratch).returncode:
            raise ValueError("pip install of the checkout failed (build backend download needs network)")

        result["status"] = result["feature_status"] = "fail"
        installed = command([str(python), "-c", "import agent_runtime.cli as c; print(c.__file__)"], scratch)
        assertion("installed cli.py matches checkout", sha((repo / "src/agent_runtime/cli.py").read_bytes()),
                  sha(Path(installed.stdout.strip()).read_bytes()) if not installed.returncode else installed.stderr)
        expected_version = next(line.split('"')[1] for line in
                                (repo / "pyproject.toml").read_text(encoding="utf-8").splitlines()
                                if line.startswith("version"))
        version = execute("version", [str(bindir / ("agent-runtime" + suffix)), "--version"], scratch)
        assertion("installed version", expected_version, version.stdout.strip())

        workspace = scratch / "workspace"
        nested = workspace / "nested 日本語"
        nested.mkdir(parents=True)
        command(["git", "init", "-q", str(workspace)], scratch)
        marker = scratch / "vendor-calls.jsonl"
        vendor = scratch / "vendor.py"
        vendor.write_text(VENDOR % (str(marker), VENDOR_EXIT), encoding="utf-8")
        config = scratch / "runtime.json"
        config.write_text(json.dumps({"version": 1,
            "tools": {"codex": {"argv": [sys.executable, str(vendor)]}},
            "workspaces": [{"root": str(workspace),
                            "lifecycle": {"argv": [str(lifecycle)], "source": str(lifecycle),
                                          "interface": "resolve-run-v1",
                                          "pins": [{"path": str(lifecycle),
                                                    "sha256": result["target"]["lifecycle"]["sha256"]}]}}]},
            indent=2), encoding="utf-8")
        for item in (config, vendor):
            shutil.copy2(item, evidence / item.name)
            result["artifacts"].append(item.name)
        env = {k: v for k, v in os.environ.items() if k not in ("PYTHONPATH", "AGENT_RUNTIME_DEPTH")}
        env.update(AGENT_RUNTIME_CONFIG=str(config), PYTHONIOENCODING="utf-8")
        codex = str(bindir / ("codex" + suffix))

        launched = execute("launch", [codex, "exec", "two words", "日本語"], nested, input=STDIN, env=env)
        calls = vendor_calls()
        assertion("launch exit code", VENDOR_EXIT, launched.returncode)
        assertion("launch vendor calls", 1, len(calls))
        assertion("launch vendor cwd", str(nested), calls[0]["cwd"])
        assertion("launch vendor argv", ["--cd", str(nested), "exec", "two words", "日本語"], calls[0]["argv"])
        assertion("launch vendor stdin", STDIN, calls[0]["stdin"])
        assertion("launch stdout is vendor output", calls[0], json.loads(launched.stdout))
        assertion("launch stderr carries vendor stderr", True, "vendor-stderr" in launched.stderr)

        override = execute("native-cwd", [codex, "-C", nested.name, "exec", "x"], workspace, input="", env=env)
        calls = vendor_calls()
        assertion("native-cwd exit code", VENDOR_EXIT, override.returncode)
        assertion("native-cwd vendor calls", 2, len(calls))
        assertion("native-cwd vendor cwd", str(workspace), calls[1]["cwd"])
        assertion("native-cwd vendor argv", ["-C", nested.name, "exec", "x"], calls[1]["argv"])

        common = command(["git", "rev-parse", "--path-format=absolute", "--git-common-dir"], workspace)
        assertion("no lifecycle state", False, (Path(common.stdout.strip()) / "workspace-lifecycle").exists())
        shutil.copy2(marker, evidence / marker.name)
        result["artifacts"].append(marker.name)
        result["status"] = result["feature_status"] = "pass"
    except (OSError, ValueError, AssertionError, KeyError, IndexError, KeyboardInterrupt) as exc:
        result["error"] = type(exc).__name__ + ": " + str(exc)
        # Preparation errors stay blocked; mismatches after Drive stay fail.
    finally:
        if scratch is not None:
            try:
                shutil.rmtree(scratch)
                result["cleanup"]["ok"] = not scratch.exists()
            except OSError as exc:
                result["cleanup"].update(ok=False, error=str(exc))
        if not result["cleanup"]["ok"]:
            result["status"] = "fail"
        result["finished_at"] = datetime.now(timezone.utc).isoformat()
        result["evidence_retained"] = all((evidence / item).exists() for item in result["artifacts"])
        if not result["evidence_retained"]:
            result["status"] = "fail"
        record()
    return result, evidence


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--repo", type=Path, required=True, help="agent-runtime checkout to install and verify")
    parser.add_argument("--lifecycle", type=Path, required=True,
                        help="absolute workspace-lifecycle executable providing resolve-run-v1")
    parser.add_argument("--evidence-root", type=Path, help="parent of the new evidence directory (default: system temp)")
    parser.add_argument("--doctor-only", action="store_true", help="identify inputs only; feature stays not-run")
    parser.add_argument("--agent", default="unknown", help="driving agent recorded in result.json")
    args = parser.parse_args()
    result, evidence = run(args.repo, args.lifecycle, args.evidence_root, args.doctor_only, args.agent)
    print(json.dumps({"status": result["status"], "result": str(evidence / "result.json")}, indent=2))
    return {"pass": 0, "fail": 1, "blocked": 2, "not-run": 3}[result["status"]]


if __name__ == "__main__":
    sys.exit(main())
