"""The verification helper detects wrong launches and never reports a pass without one."""
import json
import os
from pathlib import Path
import shutil
import subprocess
import sys
import tempfile
import unittest

ROOT = Path(__file__).resolve().parents[1]
HELPER = ROOT / "skills/verify-agent-runtime/scripts/verify.py"


def lifecycle():
    entry = Path(sys.executable).absolute().parent / ("workspace-lifecycle" + (".exe" if os.name == "nt" else ""))
    found = str(entry) if entry.is_file() else shutil.which("workspace-lifecycle")
    if not found:
        raise AssertionError("an installed workspace-lifecycle entry is required")
    return str(Path(found).resolve())


class VerificationSkillTests(unittest.TestCase):
    def setUp(self):
        temp = tempfile.TemporaryDirectory()
        self.addCleanup(temp.cleanup)
        self.base = Path(temp.name)
        self.evidence = self.base / "evidence"

    def verify(self, repo, *extra, entry=None):
        env = {k: v for k, v in os.environ.items() if k != "PYTHONPATH"}
        proc = subprocess.run([sys.executable, str(HELPER), "--repo", str(repo),
                               "--lifecycle", entry or lifecycle(), "--evidence-root", str(self.evidence), *extra],
                              capture_output=True, text=True, encoding="utf-8", env=env)
        path = Path(json.loads(proc.stdout)["result"])
        return proc.returncode, json.loads(path.read_text(encoding="utf-8")), path.parent

    def assertCleaned(self, result, evidence):
        self.assertTrue(result["cleanup"]["ok"])
        self.assertFalse(Path(result["cleanup"]["scratch"]).exists())
        self.assertTrue(result["evidence_retained"])
        for item in result["artifacts"]:
            self.assertTrue((evidence / item).exists(), item)

    def test_checkout_passes_and_keeps_evidence(self):
        code, result, evidence = self.verify(ROOT)
        self.assertEqual((code, result["status"]), (0, "pass"), result.get("error"))
        self.assertTrue(all(item["ok"] for item in result["assertions"]))
        self.assertCleaned(result, evidence)

    def test_wrong_vendor_argv_with_expected_exit_fails(self):
        copy = self.base / "faulty"
        shutil.copytree(ROOT, copy, ignore=shutil.ignore_patterns(".git", "build", "dist", "*.egg-info", "__pycache__"))
        cli = copy / "src/agent_runtime/cli.py"
        text = cli.read_text(encoding="utf-8")
        self.assertIn("*vendor, *native]", text)
        cli.write_text(text.replace("*vendor, *native]", "*vendor, *native[:-1]]"), encoding="utf-8")
        code, result, evidence = self.verify(copy)
        self.assertEqual((code, result["status"], result["target"]["revision"]), (1, "fail", "unknown"))
        failed = [item for item in result["assertions"] if not item["ok"]]
        self.assertEqual([item["name"] for item in failed], ["launch vendor argv"])
        self.assertEqual(result["commands"][-1]["exit_code"], 23)
        self.assertCleaned(result, evidence)

    def test_missing_prerequisite_blocks_and_doctor_only_is_not_run(self):
        code, result, _ = self.verify(ROOT, entry=str(self.base / "missing-lifecycle"))
        self.assertEqual((code, result["status"], result["feature_status"]), (2, "blocked", "not-run"))
        self.assertEqual(result["commands"], [])
        code, result, _ = self.verify(ROOT, "--doctor-only")
        self.assertEqual((code, result["status"], result["feature_status"]), (3, "not-run", "not-run"))
        self.assertIsNone(result["cleanup"]["scratch"])


if __name__ == "__main__":
    unittest.main()
