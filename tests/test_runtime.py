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
        item = {"root": str(self.work), "lifecycle": {"argv": [sys.executable, str(self.life)], "source": str(self.life), "interface": "resolve-run-v1", "pins": [{"path": str(self.life), "sha256": digest(self.life)}]}}
        item.update(workspace_updates)
        self.config.write_text(json.dumps({"version": 1, "tools": {name: {"argv": [sys.executable, str(self.vendor)]} for name in ("grok", "codex", "claude")}, "workspaces": [item]}), encoding="utf-8")

    def argv(self, tool, *args, independent=False, explain=False):
        flags = ["--handoff-independent"] * independent + ["--explain"] * explain
        return [sys.executable, "-m", "agent_runtime.cli", "--config", str(self.config), *flags, tool, "--", *args]

    def env(self):
        env = {**os.environ, "PYTHONIOENCODING": "utf-8"}
        env.pop("PYTHONPATH", None)
        return env

    def invoke(self, tool, *args, cwd=None, input="", independent=False, explain=False):
        return subprocess.run(self.argv(tool, *args, independent=independent, explain=explain),
                              cwd=cwd or self.nested, input=input, text=True, encoding="utf-8",
                              capture_output=True, env=self.env())

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

    def test_undeclared_cwd_launches_vendor_directly_with_notice(self):
        result = self.invoke("grok", "x", cwd=self.root)
        self.assertEqual(result.returncode, 23, result.stderr)
        self.assertEqual(json.loads(result.stdout)["cwd"], str(self.root))
        self.assertEqual(json.loads(result.stdout)["argv"], ["--cwd", str(self.root), "x"])
        notice = json.loads(result.stderr.splitlines()[0])["notice"]
        self.assertEqual((notice["lifecycle"], notice["cwd"]), ("none", str(self.root)))
        self.assertNotIn('"lifecycle": [', result.stderr)
        raw = json.loads(self.config.read_text(encoding="utf-8")); del raw["workspaces"]
        self.config.write_text(json.dumps(raw), encoding="utf-8")
        bare = self.invoke("codex", "exec", "y")
        self.assertEqual(bare.returncode, 23, bare.stderr)
        self.assertEqual(json.loads(bare.stdout)["argv"], ["--cd", str(self.nested), "exec", "y"])

    def test_former_state_and_tool_fields_do_not_gate_launch(self):
        self.write_config(state="hold", reason="review", tools=["codex"])
        result = self.invoke("grok")
        self.assertEqual(result.returncode, 23, result.stderr)
        self.assertEqual(json.loads(result.stderr.splitlines()[0])["lifecycle"][2], str(self.nested))

    def test_workspace_without_lifecycle_launches_unmanaged(self):
        raw = json.loads(self.config.read_text(encoding="utf-8"))
        raw["workspaces"].append({"root": str(self.nested), "state": "pending"})
        self.config.write_text(json.dumps(raw), encoding="utf-8")
        result = self.invoke("grok")
        self.assertEqual(result.returncode, 23, result.stderr)
        self.assertEqual(json.loads(result.stdout)["argv"], ["--cwd", str(self.nested)])
        self.assertEqual(json.loads(result.stderr.splitlines()[0])["notice"]["lifecycle"], "none")
        raw["workspaces"][-1]["lifecycle"] = {"interface": "other"}
        self.config.write_text(json.dumps(raw), encoding="utf-8")
        invalid = self.invoke("grok")
        self.assertEqual(invalid.returncode, 2)
        self.assertEqual(json.loads(invalid.stderr), {"ok": False, "error": "unsupported lifecycle interface",
                                                     "recovery": "bind a resolve-run-v1 lifecycle to " + str(self.nested)})

    def test_duplicate_root_refuses_with_recovery(self):
        raw = json.loads(self.config.read_text(encoding="utf-8")); raw["workspaces"].append(dict(raw["workspaces"][0]))
        self.config.write_text(json.dumps(raw), encoding="utf-8")
        result = self.invoke("grok")
        self.assertEqual(result.returncode, 2)
        refusal = json.loads(result.stderr)
        self.assertEqual(refusal["error"], "configuration binds workspace root more than once: " + str(self.work))
        self.assertIn("remove the duplicate", refusal["recovery"])
        self.assertEqual(result.stdout, "")

    def test_running_session_does_not_block_another_tool(self):
        for cwd in (self.nested, self.root):
            first = subprocess.Popen(self.argv("codex", "exec", "first"), cwd=cwd, stdin=subprocess.PIPE,
                                     stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True,
                                     encoding="utf-8", env=self.env())
            try:
                second = self.invoke("claude", "second", cwd=cwd)
                self.assertIsNone(first.poll())
            finally:
                out, err = first.communicate("done")
            self.assertEqual(second.returncode, 23, second.stderr)
            self.assertEqual(json.loads(second.stdout)["argv"], ["second"])
            self.assertEqual(first.returncode, 23, err)
            self.assertEqual(json.loads(out)["stdin"], "done")

    def test_explain_reports_plan_without_running_anything(self):
        receiver = self.root / "explain-receive.py"; marker = self.root / "explain-ran"
        receiver.write_text("from pathlib import Path\nPath(%r).write_text('ran')\n" % str(marker), encoding="utf-8")
        self.write_config(handoff={"owner": "runtime", "argv": [sys.executable, str(receiver)], "source": str(receiver), "source_sha256": digest(receiver)})
        managed = self.invoke("codex", "exec", "x", explain=True)
        self.assertEqual(managed.returncode, 0, managed.stderr)
        plan = json.loads(managed.stdout)
        vendor = [sys.executable, str(self.vendor)]
        self.assertEqual(plan["workspace"], str(self.work))
        self.assertEqual(plan["lifecycle"], [sys.executable, str(self.life)])
        self.assertEqual(plan["handoff"], {"owner": "runtime", "argv": [sys.executable, str(receiver)]})
        self.assertEqual(plan["command"], [sys.executable, str(self.life), "resolve-run", "--cwd", str(self.nested),
                                           "--launch-cwd", str(self.nested), "--", *vendor,
                                           "--cd", str(self.nested), "exec", "x"])
        self.assertEqual((plan["ok"], plan["notices"], managed.stderr), (True, [], ""))
        self.assertFalse(marker.exists())
        outside = json.loads(self.invoke("codex", explain=True, cwd=self.root).stdout)
        self.assertEqual((outside["workspace"], outside["lifecycle"]), (None, None))
        self.assertEqual(outside["command"], [*vendor, "--cd", str(self.root)])
        self.assertEqual(outside["notices"][0]["lifecycle"], "none")
        self.life.write_text("changed", encoding="utf-8")
        refused = self.invoke("codex", explain=True)
        self.assertEqual(refused.returncode, 2)
        refusal = json.loads(refused.stdout)
        self.assertFalse(refusal["ok"])
        self.assertIn("lifecycle source pin does not match", refusal["error"])
        self.assertIn("updates its pin", refusal["recovery"])

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

    def test_independent_skips_receiver_but_pin_mismatch_blocks(self):
        receiver = self.root / "skip-receive.py"
        marker = self.root / "ran"
        receiver.write_text("from pathlib import Path\nimport sys\nPath(%r).write_text('ran')\nprint('{\\\"status\\\":\\\"updated\\\"}')\n" % str(marker), encoding="utf-8")
        self.write_config(handoff={"owner": "runtime", "argv": [sys.executable, str(receiver)], "source": str(receiver), "source_sha256": digest(receiver)})
        skipped = self.invoke("grok", independent=True)
        self.assertEqual(skipped.returncode, 23, skipped.stderr)
        self.assertFalse(marker.exists())
        self.assertIn('"reason": "explicit-independent"', skipped.stderr)
        receiver.write_text("changed\n", encoding="utf-8")
        blocked = self.invoke("grok", independent=True)
        self.assertEqual(blocked.returncode, 2)
        self.assertIn("source pin", blocked.stderr)
        self.assertFalse(marker.exists())

    def test_handoff_failure_requires_explicit_independent_launch(self):
        receiver = self.root / "fail-receive.py"
        receiver.write_text("raise SystemExit(9)\n", encoding="utf-8")
        self.write_config(handoff={"owner":"runtime", "argv":[sys.executable, str(receiver)], "source":str(receiver), "source_sha256":digest(receiver)})
        blocked = self.invoke("grok")
        self.assertEqual(blocked.returncode, 2); self.assertIn("receiver failed", blocked.stderr)
        self.assertEqual(json.loads(blocked.stderr)["recovery"],
                         "for work that does not need the HANDOFF, rerun as agent-runtime --handoff-independent TOOL")
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

    def test_deepest_binding_selects_its_own_lifecycle(self):
        child = self.root / "child-life.py"
        child.write_text(self.life.read_text(encoding="utf-8").replace("'lifecycle'", "'child'"), encoding="utf-8")
        raw = json.loads(self.config.read_text())
        raw['workspaces'].append({'root': str(self.nested), 'lifecycle': {
            'argv': [sys.executable, str(child)], 'source': str(child), 'interface': 'resolve-run-v1',
            'pins': [{'path': str(child), 'sha256': digest(child)}]}})
        self.config.write_text(json.dumps(raw))
        for args, cwd in (([], self.nested), (['--cwd', 'nested'], self.work)):
            result = self.invoke('grok', *args, cwd=cwd)
            self.assertEqual(result.returncode, 23, result.stderr)
            self.assertEqual(json.loads(result.stderr.splitlines()[0])['child'][2], str(self.nested))

    def test_repair_forms_are_vendor_specific(self):
        raw = json.loads(self.config.read_text())
        raw['workspaces'] = 'broken'
        self.config.write_text(json.dumps(raw))
        for tool, args in [('codex', ['update']), ('claude', ['auth', 'login']),
                           ('claude', ['doctor']), ('grok', ['inspect', '--json'])]:
            self.assertEqual(self.invoke(tool, *args).returncode, 23)
        for tool, args in [('claude', ['login']), ('codex', ['inspect']),
                           ('grok', ['--single', 'doctor'])]:
            result = self.invoke(tool, *args)
            self.assertEqual(result.returncode, 2)
            self.assertIn('workspaces must be a list', result.stderr)

    def test_installed_standard_entrypoints_use_explicit_config(self):
        scripts = Path(sysconfig.get_path("scripts"))
        env = {**os.environ, "AGENT_RUNTIME_CONFIG": str(self.config), "PYTHONIOENCODING": "utf-8"}
        env.pop("PYTHONPATH", None)
        for tool in ("codex", "claude", "grok"):
            entry = scripts / (tool + (".exe" if os.name == "nt" else ""))
            result = subprocess.run([str(entry), "hello 日本語"], cwd=self.nested, input="",
                                    env=env, text=True, encoding="utf-8", capture_output=True)
            self.assertEqual(result.returncode, 23, result.stderr)
            self.assertEqual(json.loads(result.stdout)["argv"][-1], "hello 日本語")


if __name__ == "__main__":
    unittest.main()
