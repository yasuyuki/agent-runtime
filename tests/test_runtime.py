import hashlib
import json
import os
from pathlib import Path
import subprocess
import sys
import sysconfig
import tempfile
import unittest

from agent_runtime import cli


def digest(path):
    return hashlib.sha256(Path(path).read_bytes()).hexdigest()


class RuntimeTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name).resolve()
        self.work = self.root / "work 日本語"
        self.nested = self.work / "nested"
        self.nested.mkdir(parents=True)
        self.vendor = self.root / "vendor.py"
        self.life = self.root / "life.py"
        self.vendor.write_text("import json,os,sys\nprint(json.dumps({'cwd':os.getcwd(),'argv':sys.argv[1:],'stdin':sys.stdin.read()},ensure_ascii=False))\nprint('vendor-stderr',file=sys.stderr)\nsys.exit(23)\n", encoding="utf-8")
        self.life.write_text("import json,os,subprocess,sys\na=sys.argv[1:]; i=a.index('--'); print(json.dumps({'lifecycle':a[:i]},ensure_ascii=False),file=sys.stderr); sys.exit(subprocess.run(a[i+1:]).returncode)\n", encoding="utf-8")
        self.config = self.root / "runtime.json"
        self.write_config()

    def write_config(self, **workspace_updates):
        item = {"root": str(self.work), "tools": ["grok", "codex", "claude"], "state": "active", "lifecycle": {"argv": [sys.executable, str(self.life)], "source": str(self.life), "interface": "resolve-run-v1", "pins": [{"path": str(self.life), "sha256": digest(self.life)}]}}
        item.update(workspace_updates)
        self.config.write_text(json.dumps({"version": 1, "tools": {name: {"argv": [sys.executable, str(self.vendor)]} for name in ("grok", "codex", "claude")}, "workspaces": [item]}), encoding="utf-8")

    def invoke(self, tool, *args, cwd=None, input="", independent=False):
        python = sys.executable
        env = {**os.environ, "PYTHONIOENCODING": "utf-8"}
        env.pop("PYTHONPATH", None)
        flag = ["--handoff-independent"] if independent else []
        return subprocess.run([python, "-m", "agent_runtime.cli", "--config", str(self.config), *flag, tool, "--", *args], cwd=cwd or self.nested, input=input, text=True, encoding="utf-8", capture_output=True, env=env)

    def test_subprocess_transparent_unicode_cwd_stdio_exit(self):
        result = self.invoke("grok", "--prompt", 'a "quoted" 日本語', input="stdin 日本語")
        self.assertEqual(result.returncode, 23, result.stderr)
        payload = json.loads(result.stdout)
        self.assertEqual(payload["cwd"], str(self.nested))
        self.assertEqual(payload["argv"], ["--cwd", str(self.nested), "--prompt", 'a "quoted" 日本語'])
        self.assertEqual(payload["stdin"], "stdin 日本語")
        self.assertIn("vendor-stderr", result.stderr)
        self.assertEqual(json.loads(result.stderr.splitlines()[0])['lifecycle'][2], str(self.nested))

    def test_native_override_selects_nested_and_preserves_argv(self):
        result = self.invoke("grok", "--cwd", str(self.work))
        self.assertEqual(result.returncode, 23, result.stderr)
        # The lifecycle receives the effective cwd for ownership; the native
        # argv and invocation cwd stay intact for the vendor to interpret.
        self.assertEqual(json.loads(result.stdout)["cwd"], str(self.nested))
        self.assertEqual(json.loads(result.stdout)["argv"], ["--cwd", str(self.work)])
        self.assertEqual(json.loads(result.stderr.splitlines()[0])['lifecycle'][2], str(self.work))

    def test_held_unknown_and_ambiguous_refuse(self):
        self.write_config(state="hold", reason="review")
        held = self.invoke("grok")
        self.assertEqual(held.returncode, 2); self.assertIn("workspace is hold: review", held.stderr)
        self.write_config()
        unknown = self.invoke("grok", cwd=self.root)
        self.assertEqual(unknown.returncode, 2); self.assertIn("not declared", unknown.stderr)
        raw = json.loads(self.config.read_text(encoding="utf-8")); raw["workspaces"].append(dict(raw["workspaces"][0]))
        self.config.write_text(json.dumps(raw), encoding="utf-8")
        ambiguous = self.invoke("grok")
        self.assertEqual(ambiguous.returncode, 2); self.assertIn("ambiguous", ambiguous.stderr)

    def test_pending_and_tool_disallow_refuse(self):
        self.write_config(state="pending", reason="binding review")
        pending = self.invoke("grok")
        self.assertEqual(pending.returncode, 2); self.assertIn("pending", pending.stderr)
        self.write_config(tools=["codex"])
        denied = self.invoke("grok")
        self.assertEqual(denied.returncode, 2); self.assertIn("not enabled", denied.stderr)

    def test_native_values_terminator_duplicates_and_compact_are_bounded(self):
        # Values which happen to spell options and words after -- are vendor
        # data. Repeated true cwd options remain a refusal.
        value = self.invoke("grok", "--prompt", "--cwd")
        self.assertEqual(value.returncode, 23, value.stderr)
        stopped = self.invoke("grok", "--", "--cwd", str(self.root))
        self.assertEqual(stopped.returncode, 23, stopped.stderr)
        duplicate = self.invoke("grok", "--cwd", str(self.work), "--cwd", str(self.nested))
        self.assertEqual(duplicate.returncode, 2); self.assertIn("ambiguous", duplicate.stderr)
        compact = self.invoke("codex", "-Celsewhere")
        self.assertEqual(compact.returncode, 2); self.assertIn("does not exist", compact.stderr)

    def test_codex_resume_is_anchored_without_an_override(self):
        result = self.invoke("codex", "resume", "--last")
        self.assertEqual(result.returncode, 23, result.stderr)
        self.assertEqual(json.loads(result.stdout)["argv"][:4], ["--cd", str(self.nested), "resume", "--last"])

    def test_repair_ignores_broken_workspace_but_not_prompt(self):
        # A configured vendor remains available even though workspace checks
        # would otherwise fail; option-looking prompt content does not qualify.
        raw = json.loads(self.config.read_text())
        raw["workspaces"] = "broken"
        self.config.write_text(json.dumps(raw))
        good = self.invoke("grok", "--help")
        self.assertEqual(good.returncode, 23, good.stderr)
        self.config.write_text("broken", encoding="utf-8")
        bad = self.invoke("grok", "--prompt", "doctor")
        self.assertEqual(bad.returncode, 2); self.assertIn("Expecting value", bad.stderr)

    def test_changed_pin_refuses(self):
        raw = json.loads(self.config.read_text(encoding="utf-8")); raw["tools"]["grok"]["argv"] = [sys.executable]
        self.config.write_text(json.dumps(raw), encoding="utf-8")
        # A non-runtime executable is a valid explicit vendor; source-pinning is
        # separately enforced for lifecycle changes.
        self.life.write_text("changed", encoding="utf-8")
        result = self.invoke("grok")
        self.assertEqual(result.returncode, 2); self.assertIn("source pin", result.stderr)

    def test_handoff_runtime_runs_once_and_failure_stops_launch(self):
        receiver = self.root / "receive.py"; receipt = self.root / "receipt"
        receiver.write_text("from pathlib import Path\nimport sys\nPath(%r).write_text('one'); print('{\\\"status\\\":\\\"updated\\\"}')\n" % str(receipt), encoding="utf-8")
        self.write_config(handoff={"owner":"runtime", "argv":[sys.executable, str(receiver)], "source":str(receiver), "source_sha256":digest(receiver)})
        result = self.invoke("grok")
        self.assertEqual(result.returncode, 23, result.stderr); self.assertEqual(receipt.read_text(), "one")

    def test_handoff_failure_requires_explicit_independent_launch(self):
        receiver = self.root / "fail-receive.py"
        receiver.write_text("raise SystemExit(9)\n", encoding="utf-8")
        self.write_config(handoff={"owner":"runtime", "argv":[sys.executable, str(receiver)], "source":str(receiver), "source_sha256":digest(receiver)})
        blocked = self.invoke("grok")
        self.assertEqual(blocked.returncode, 2); self.assertIn("receiver failed", blocked.stderr)
        independent = self.invoke("grok", independent=True)
        self.assertEqual(independent.returncode, 23, independent.stderr)
        self.assertIn('"independent": true', independent.stderr)

    def test_vendor_missing_and_actual_wrapper_recursion(self):
        raw = json.loads(self.config.read_text())
        raw['tools']['grok']['argv'] = [str(self.root / 'missing')]
        self.config.write_text(json.dumps(raw))
        self.assertIn('unavailable', self.invoke('grok').stderr)
        scripts = Path(sysconfig.get_path('scripts'))
        entry = scripts / ('grok.exe' if os.name == 'nt' else 'grok')
        raw['tools']['grok']['argv'] = [str(entry)]
        self.config.write_text(json.dumps(raw))
        for args in ([], ['--help']):
            result = self.invoke('grok', *args)
            self.assertEqual(result.returncode, 2)
            self.assertIn('recurs', result.stderr)

    def test_vendor_can_launch_a_managed_child(self):
        child_config = self.root / 'child.json'
        child_vendor = self.root / 'child.py'
        child_vendor.write_text("import os; assert 'AGENT_RUNTIME_DEPTH' not in os.environ; print('child')\n")
        raw = json.loads(self.config.read_text())
        raw['tools']['claude']['argv'] = [sys.executable, str(child_vendor)]
        child_config.write_text(json.dumps(raw))
        scripts = Path(sysconfig.get_path('scripts'))
        entry = scripts / ('claude.exe' if os.name == 'nt' else 'claude')
        self.vendor.write_text("import os,subprocess\nassert 'AGENT_RUNTIME_DEPTH' not in os.environ\nenv={**os.environ,'AGENT_RUNTIME_CONFIG':%r}\nraise SystemExit(subprocess.call([%r,'child prompt'],env=env))\n" % (str(child_config), str(entry)))
        result = self.invoke('grok')
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual(result.stdout.strip(), 'child')

    def test_vendor_replacement_is_resolved_on_each_start(self):
        self.vendor.write_text("print('updated')\n", encoding='utf-8')
        result = self.invoke('grok')
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual(result.stdout.strip(), 'updated')

    def test_nested_hold_never_falls_back_to_parent(self):
        raw = json.loads(self.config.read_text())
        raw['workspaces'].append({'root': str(self.nested), 'tools': ['grok'],
                                  'state': 'hold', 'reason': 'child held'})
        self.config.write_text(json.dumps(raw))
        result = self.invoke('grok')
        self.assertEqual(result.returncode, 2)
        self.assertIn('child held', result.stderr)
        result = self.invoke('grok', '--cwd', 'nested', cwd=self.work)
        self.assertEqual(result.returncode, 2)
        self.assertIn('child held', result.stderr)

    def test_repair_forms_are_vendor_specific(self):
        self.write_config(state='hold')
        for tool, args in [('codex', ['update']), ('claude', ['auth', 'login']),
                           ('claude', ['doctor']), ('grok', ['inspect', '--json'])]:
            self.assertEqual(self.invoke(tool, *args).returncode, 23)
        for tool, args in [('claude', ['login']), ('codex', ['inspect']),
                           ('grok', ['--single', 'doctor'])]:
            result = self.invoke(tool, *args)
            self.assertEqual(result.returncode, 2)
            self.assertIn('hold', result.stderr)

    def test_installed_standard_entrypoints_use_explicit_config(self):
        scripts = Path(sysconfig.get_path("scripts"))
        env = {**os.environ, "AGENT_RUNTIME_CONFIG": str(self.config), "PYTHONIOENCODING": "utf-8"}
        env.pop("PYTHONPATH", None)
        for tool in ("codex", "claude", "grok"):
            entry = scripts / (tool + (".exe" if os.name == "nt" else ""))
            result = subprocess.run([str(entry), "hello 日本語"], cwd=self.nested,
                                    env=env, text=True, encoding="utf-8", capture_output=True)
            self.assertEqual(result.returncode, 23, result.stderr)
            self.assertEqual(json.loads(result.stdout)["argv"][-1], "hello 日本語")


if __name__ == "__main__":
    unittest.main()
