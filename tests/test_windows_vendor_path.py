import hashlib
import json
import os
from pathlib import Path
import stat
import subprocess
import sys
import tempfile
import types
import unittest
from unittest import mock

from agent_runtime import cli


@unittest.skipUnless(os.name == "nt", "Windows junction behavior")
class WindowsVendorPathTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory(dir=Path(__file__).resolve().parents[1])
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name).resolve()
        self.installer = self.root / "installer"
        self.standalone = self.installer / "standalone"
        self.standalone.mkdir(parents=True)
        self.release_a = self._release("release-a")
        self.release_b = self._release("release-b")
        self.official = self.root / "official"
        self._junction(self.official, self.installer)
        self.current = self.standalone / "current"
        self._junction(self.current, self.release_a)
        self.vendor = self.official / "standalone" / "current" / "bin" / "codex.exe"
        self.life = self.root / "life.py"
        self.life.write_text("print('life')\n", encoding="utf-8")
        self.config = self.root / "runtime.json"
        self.work = self.root / "work"
        self.work.mkdir()
        self._write_config()

    def _release(self, name):
        release = self.root / name / "bin"
        release.mkdir(parents=True)
        (release / "codex.exe").write_text("vendor\n", encoding="utf-8")
        return release.parent

    def _junction(self, link, target):
        result = subprocess.run(["cmd", "/c", "mklink", "/J", str(link), str(target)],
                                text=True, encoding="utf-8", capture_output=True)
        self.assertEqual(result.returncode, 0, result.stderr or result.stdout)

    def _retarget_current(self, release):
        os.rmdir(self.current)
        self._junction(self.current, release)

    def _write_config(self):
        digest = hashlib.sha256(self.life.read_bytes()).hexdigest()
        self.config.write_text(json.dumps({"version": 1, "tools": {"codex": {
            "argv": [str(self.vendor), "--binding", "vendor"]}}, "workspaces": [{"root": str(self.work), "lifecycle": {
                "argv": [sys.executable, str(self.life)], "source": str(self.life),
                "interface": "resolve-run-v1", "pins": [{"path": str(self.life), "sha256": digest}]}}]}),
            encoding="utf-8")

    def _resolve_448(self, path):
        original = Path.resolve
        vendor = os.path.normcase(os.fspath(path))

        def resolve(value, *args, **kwargs):
            if os.path.normcase(os.fspath(value)) == vendor:
                raise OSError(0, "The path contains a reparse point", None, 448)
            return original(value, *args, **kwargs)

        return mock.patch.object(Path, "resolve", resolve)

    def _plan(self, argv=(), cwd=None):
        previous = Path.cwd()
        os.chdir(cwd or self.root)
        try:
            with self._resolve_448(self.vendor):
                return cli._plan("codex", list(argv), str(self.config))
        finally:
            os.chdir(previous)

    def test_448_vendor_chain_retargets_and_preserves_all_plan_forms(self):
        physical_a = self.release_a / "bin" / "codex.exe"
        unmanaged = self._plan()
        self.assertEqual(unmanaged["vendor"], [str(physical_a), "--binding", "vendor"])
        self.assertEqual(unmanaged["configured_vendor"], [str(self.vendor), "--binding", "vendor"])
        self.assertEqual(unmanaged["command"], [str(physical_a), "--binding", "vendor", "--cd", str(self.root)])
        maintenance = self._plan(["update"], self.work)
        self.assertEqual(maintenance["vendor"], [str(physical_a), "--binding", "vendor"])
        self.assertEqual(maintenance["command"], [str(physical_a), "--binding", "vendor", "update"])
        managed = self._plan(["exec", "x"], self.work)
        self.assertEqual(managed["vendor"], [str(physical_a), "--binding", "vendor"])
        self.assertIn(str(physical_a), managed["command"])
        self._retarget_current(self.release_b)
        physical_b = self.release_b / "bin" / "codex.exe"
        retargeted = self._plan(cwd=self.work)
        self.assertEqual(retargeted["vendor"], [str(physical_b), "--binding", "vendor"])

    def test_normal_vendor_path_keeps_configured_argv(self):
        plan = cli._plan("codex", [], str(self.config))
        self.assertEqual(plan["vendor"], [str(self.vendor), "--binding", "vendor"])
        self.assertNotIn("configured_vendor", plan)

    def test_non_448_resolve_error_is_not_recovered(self):
        original = Path.resolve
        lstat = os.lstat
        inspected = []

        def resolve(path, *args, **kwargs):
            if os.path.normcase(os.fspath(path)) == os.path.normcase(os.fspath(self.vendor)):
                raise OSError(0, "Access is denied", None, 5)
            return original(path, *args, **kwargs)

        def tracked_lstat(path, *args, **kwargs):
            inspected.append(path)
            return lstat(path, *args, **kwargs)

        with mock.patch.object(Path, "resolve", resolve), \
             mock.patch("agent_runtime.cli.os.lstat", tracked_lstat):
            with self.assertRaises(OSError) as raised:
                cli._plan("codex", [], str(self.config))
        self.assertEqual(raised.exception.winerror, 5)
        self.assertEqual(inspected, [])

    def test_missing_vendor_after_448_refuses(self):
        (self.release_a / "bin" / "codex.exe").unlink()
        with self.assertRaisesRegex(cli.RuntimeError_, "unavailable"):
            self._plan()

    def test_cyclic_vendor_link_after_448_refuses(self):
        candidate = self.official
        original = os.readlink

        def readlink(path):
            if os.path.normcase(os.fspath(path)) == os.path.normcase(os.fspath(candidate)):
                return str(candidate)
            return original(path)

        with mock.patch("agent_runtime.cli.os.readlink", readlink):
            with self.assertRaisesRegex(cli.RuntimeError_, "cycle"):
                self._plan()

    def test_unsupported_reparse_point_after_448_refuses(self):
        candidate = self.official
        original = os.lstat

        def lstat(path, *args, **kwargs):
            value = original(path, *args, **kwargs)
            if os.path.normcase(os.fspath(path)) == os.path.normcase(os.fspath(candidate)):
                return types.SimpleNamespace(st_mode=stat.S_IFREG, st_reparse_tag=0xDEADBEEF)
            return value

        with mock.patch("agent_runtime.cli.os.lstat", lstat):
            with self.assertRaisesRegex(cli.RuntimeError_, "unsupported reparse point"):
                self._plan()

    def test_permission_error_while_inspecting_fallback_refuses(self):
        candidate = self.official
        original = os.lstat

        def lstat(path, *args, **kwargs):
            if os.path.normcase(os.fspath(path)) == os.path.normcase(os.fspath(candidate)):
                raise PermissionError("Access is denied")
            return original(path, *args, **kwargs)

        with mock.patch("agent_runtime.cli.os.lstat", lstat):
            with self.assertRaises(PermissionError):
                self._plan()

    def test_unc_link_target_after_448_refuses(self):
        candidate = self.official
        original = os.readlink

        def readlink(path):
            if os.path.normcase(os.fspath(path)) == os.path.normcase(os.fspath(candidate)):
                return r"\\?\UNC\server\share"
            return original(path)

        with mock.patch("agent_runtime.cli.os.readlink", readlink):
            with self.assertRaisesRegex(cli.RuntimeError_, "unsupported link target"):
                self._plan()

    def test_volume_namespace_never_selects_relative_lookalike(self):
        lookalike = self.standalone / "Volume{fixture}" / "bin" / "codex.exe"
        lookalike.parent.mkdir(parents=True)
        lookalike.write_text("wrong vendor\n", encoding="utf-8")
        original = os.readlink

        def readlink(path):
            if Path(path) == self.current:
                return r"\\?\Volume{fixture}"
            return original(path)

        with mock.patch("agent_runtime.cli.os.readlink", readlink):
            with self.assertRaisesRegex(cli.RuntimeError_, "unsupported link target"):
                self._plan()

    def test_physical_vendor_target_still_blocks_runtime_recursion(self):
        linked_runtime = self.root / "runtime-link"
        self._junction(linked_runtime, Path(cli.__file__).resolve().parent)
        vendor = linked_runtime / Path(cli.__file__).name
        raw = json.loads(self.config.read_text(encoding="utf-8"))
        raw["tools"]["codex"]["argv"] = [str(vendor)]
        self.config.write_text(json.dumps(raw), encoding="utf-8")
        original = Path.resolve

        def resolve(path, *args, **kwargs):
            if os.path.normcase(os.fspath(path)) == os.path.normcase(os.fspath(vendor)):
                raise OSError(0, "The path contains a reparse point", None, 448)
            return original(path, *args, **kwargs)

        with mock.patch.object(Path, "resolve", resolve):
            with self.assertRaisesRegex(cli.RuntimeError_, "recurses"):
                cli._plan("codex", [], str(self.config))

    def test_relative_link_target_still_blocks_runtime_recursion(self):
        os.rmdir(self.current)
        package = Path(cli.__file__).resolve().parent
        relative = os.path.relpath(package, self.current.parent)
        try:
            os.symlink(relative, self.current, target_is_directory=True)
        except OSError as exc:
            self.skipTest("symbolic links unavailable: " + str(exc))
        self.vendor = self.official / "standalone" / "current" / Path(cli.__file__).name
        raw = json.loads(self.config.read_text(encoding="utf-8"))
        raw["tools"]["codex"]["argv"] = [str(self.vendor)]
        self.config.write_text(json.dumps(raw), encoding="utf-8")
        with self.assertRaisesRegex(cli.RuntimeError_, "recurses"):
            self._plan()


if __name__ == "__main__":
    unittest.main()
