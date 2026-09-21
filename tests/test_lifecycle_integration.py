"""Installed runtime and lifecycle packages compose through their public CLIs."""
import hashlib
import json
import os
from pathlib import Path
import shutil
import subprocess
import sys
import tempfile
import unittest


def run(cwd, *argv, check=True, **kwargs):
    return subprocess.run(argv, cwd=cwd, text=True, encoding="utf-8", stdout=subprocess.PIPE,
                          stderr=subprocess.PIPE, check=check, **kwargs)


def git(cwd, *args):
    return run(cwd, "git", "-C", str(cwd), *args).stdout.strip()


def digest(path):
    return hashlib.sha256(Path(path).read_bytes()).hexdigest()


class InstalledLifecycleIntegrationTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.base = Path(self.temp.name).resolve()
        suffix = ".exe" if os.name == "nt" else ""
        scripts = Path(sys.executable).absolute().parent
        runtime_entry = scripts / ("agent-runtime" + suffix)
        lifecycle_entry = scripts / ("workspace-lifecycle" + suffix)
        self.runtime = str(runtime_entry) if runtime_entry.is_file() else shutil.which("agent-runtime")
        self.lifecycle = str(lifecycle_entry) if lifecycle_entry.is_file() else shutil.which("workspace-lifecycle")
        if not self.runtime or not self.lifecycle:
            self.fail("installed agent-runtime and workspace-lifecycle entries are required")
        self.runtime = str(Path(self.runtime).resolve())
        self.lifecycle = str(Path(self.lifecycle).resolve())
        self.environment = {k: v for k, v in os.environ.items() if k != "PYTHONPATH"}
        self.environment["PYTHONIOENCODING"] = "utf-8"

        self.remote = self.base / "remote.git"
        self.root = self.base / "root"
        self.topic = self.base / "topic"
        run(self.base, "git", "init", "--bare", "--initial-branch=trunk", str(self.remote))
        run(self.base, "git", "clone", str(self.remote), str(self.root))
        git(self.root, "config", "user.name", "Runtime Integration")
        git(self.root, "config", "user.email", "runtime@example.invalid")
        (self.root / "README").write_text("base\n", encoding="utf-8")
        (self.root / "nested 日本語").mkdir()
        (self.root / "nested 日本語" / ".keep").write_text("tracked\n")
        git(self.root, "add", "README", "nested 日本語/.keep")
        git(self.root, "commit", "-m", "base")
        git(self.root, "push", "origin", "trunk")
        preflight = [sys.executable, "-m", "workspace_lifecycle.push", "{repo}",
                     "--user-intent", "push"]
        begun = run(self.root, self.lifecycle, "--repo", str(self.root), "begin",
                    "--task", "runtime", "--request", "issue/runtime", "--remote", "origin",
                    "--branch", "topic/runtime", "--worktree", str(self.topic),
                    "--validation-json", json.dumps(["git", "diff", "--check"]),
                    "--preflight-json", json.dumps(preflight), env=self.environment)
        self.assertEqual(json.loads(begun.stdout)["ok"], True)

        self.vendor = self.base / "vendor.py"
        self.marker = self.base / "vendor-ran"
        self.vendor.write_text(
            """import hashlib,json,os,pathlib,subprocess,sys,tempfile
context=json.loads(os.environ['WORKSPACE_LIFECYCLE_CONTEXT'])
repo=pathlib.Path(context['repo'])
launch=os.getcwd(); os.chdir(repo)
path=repo/'feature.txt'; path.write_text('integrated\\n',encoding='utf-8')
descriptor,plan_name=tempfile.mkstemp(suffix='.json'); os.close(descriptor)
plan=pathlib.Path(plan_name)
plan.write_text(json.dumps({'commit':[{'path':'feature.txt','classification':'source',
 'owner':context['task'],'evidence':'installed integration fixture','safe_to_commit':True,
 'sha256':hashlib.sha256(path.read_bytes()).hexdigest()}]}),encoding='utf-8')
result=subprocess.run([*context['finish_argv'],'--plan',str(plan),
 '--result-ref','issue/runtime','--users-released'],text=True,capture_output=True)
if result.returncode:
 print(result.stderr,file=sys.stderr); raise SystemExit(result.returncode)
pathlib.Path(%r).write_text(json.dumps({'cwd':launch,'argv':sys.argv[1:]}),encoding='utf-8')
""" % str(self.marker), encoding="utf-8")
        self.config = self.base / "runtime.json"

    def lifecycle_spec(self):
        return {"argv": [self.lifecycle], "source": self.lifecycle,
                "interface": "resolve-run-v1",
                "pins": [{"path": self.lifecycle, "sha256": digest(self.lifecycle)}]}

    def write_config(self, roots):
        self.config.write_text(json.dumps({"version": 1,
            "tools": {"grok": {"argv": [sys.executable, str(self.vendor)]}},
            "workspaces": [{"root": str(root), "state": "active", "tools": ["grok"],
                            "lifecycle": self.lifecycle_spec()} for root in roots]}),
            encoding="utf-8")

    def invoke(self, cwd, *args):
        return run(cwd, self.runtime, "--config", str(self.config), "grok", "--", *args,
                   check=False, env=self.environment)

    def test_managed_relative_cwd_executes_advertised_finish_and_retires(self):
        nested = self.topic / "nested 日本語"
        self.assertTrue(nested.is_dir())
        self.write_config([self.topic])
        # An external launcher has released the worktree. On Windows a caller
        # whose cwd is inside the tree still owns a directory handle and cannot
        # truthfully request users-released retirement.
        relative = str(nested.relative_to(self.base))
        result = self.invoke(self.base, "--cwd", relative, "two words", "日本語")
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual(result.stdout, "")
        self.assertFalse(self.topic.exists())
        self.assertEqual(git(self.root, "show", "HEAD:feature.txt"), "integrated")
        observed = json.loads(self.marker.read_text(encoding="utf-8"))
        self.assertEqual(observed["cwd"], str(self.base))
        self.assertEqual(observed["argv"], ["--cwd", relative, "two words", "日本語"])

    def test_owner_boundaries_refuse_and_unmanaged_creates_no_state(self):
        marker = self.marker
        self.write_config([self.root, self.topic])
        unbound = self.invoke(self.root)
        self.assertEqual(unbound.returncode, 2)
        self.assertIn("binding", unbound.stderr)
        self.assertFalse(marker.exists())

        held = run(self.topic, self.lifecycle, "--repo", str(self.topic), "hold",
                   "--task", "runtime", "--reason", "fixture hold",
                   "--next-action", "await release", env=self.environment)
        self.assertTrue(json.loads(held.stdout)["ok"])
        refused = self.invoke(self.topic)
        self.assertEqual(refused.returncode, 2)
        self.assertIn("held", refused.stderr)
        self.assertFalse(marker.exists())

        common = Path(git(self.root, "rev-parse", "--path-format=absolute", "--git-common-dir"))
        legacy = common / "agent-branches"
        legacy.mkdir()
        (legacy / "state.json").write_text("{}", encoding="utf-8")
        legacy_result = self.invoke(self.root)
        self.assertEqual(legacy_result.returncode, 2)
        self.assertIn("legacy", legacy_result.stderr)
        self.assertFalse(marker.exists())

        unmanaged = self.base / "unmanaged"
        run(self.base, "git", "init", "--initial-branch=trunk", str(unmanaged))
        nested = unmanaged / "nested"; nested.mkdir()
        self.write_config([unmanaged])
        plain_vendor = self.base / "plain-vendor.py"
        plain_vendor.write_text("import os; print(os.getcwd())\n", encoding="utf-8")
        raw = json.loads(self.config.read_text(encoding="utf-8"))
        raw["tools"]["grok"]["argv"] = [sys.executable, str(plain_vendor)]
        self.config.write_text(json.dumps(raw), encoding="utf-8")
        direct = self.invoke(nested)
        self.assertEqual(direct.returncode, 0, direct.stderr)
        self.assertEqual(direct.stdout.strip(), str(nested))
        unmanaged_common = Path(git(unmanaged, "rev-parse", "--path-format=absolute", "--git-common-dir"))
        self.assertFalse((unmanaged_common / "workspace-lifecycle").exists())


if __name__ == "__main__":
    unittest.main()
