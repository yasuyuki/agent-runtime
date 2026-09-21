"""Windows console control events cross both runtime launch paths."""
import json
import os
from pathlib import Path
import subprocess
import sys
import unittest

import test_lifecycle_integration as fixtures


@unittest.skipUnless(os.name == "nt", "Windows console acceptance")
class WindowsConsoleTests(unittest.TestCase):
    def setUp(self):
        # Reuse the installed-package Git/lifecycle fixture without inheriting
        # its test methods. This test still invokes only installed public CLIs.
        fixtures.InstalledLifecycleIntegrationTests.setUp(self)
        self.ready = self.base / "signal-ready"
        self.vendor.write_text(
            """import json,os,pathlib,signal,sys,time
ready=pathlib.Path(sys.argv[1])
def received(number, frame):
    ready.with_suffix('.received').write_text(str(number), encoding='utf-8')
    raise SystemExit(37)
signal.signal(signal.SIGBREAK, received)
signal.signal(signal.SIGINT, received)
ready.write_text(json.dumps({'pid':os.getpid(),'tty':[os.isatty(fd) for fd in (0,1,2)]}), encoding='utf-8')
deadline=time.monotonic()+20
while time.monotonic()<deadline: time.sleep(0.05)
raise SystemExit(98)
""", encoding="utf-8")

    lifecycle_spec = fixtures.InstalledLifecycleIntegrationTests.lifecycle_spec

    def write_signal_config(self, root):
        self.config.write_text(json.dumps({"version": 1,
            "tools": {"grok": {"argv": [sys.executable, str(self.vendor), str(self.ready)]}},
            "workspaces": [{"root": str(root), "state": "active", "tools": ["grok"],
                            "lifecycle": self.lifecycle_spec()}]}), encoding="utf-8")

    def drive_event(self, cwd, event):
        self.ready.unlink(missing_ok=True)
        self.ready.with_suffix(".received").unlink(missing_ok=True)
        driver = r'''
import ctypes,json,os,pathlib,subprocess,sys,time
runtime,config,cwd,ready,event=sys.argv[1:]
event=int(event)
# CI shells may inherit the ignore-Ctrl+C attribute. A normal interactive
# caller processes it; set that inherited attribute before creating the private
# console. A targetable Break group is separate from broadcast Ctrl+C.
kernel=ctypes.WinDLL('kernel32',use_last_error=True)
kernel.SetConsoleCtrlHandler.argtypes=(ctypes.c_void_p,ctypes.c_int)
kernel.SetConsoleCtrlHandler.restype=ctypes.c_int
if not kernel.SetConsoleCtrlHandler(None,False):
    raise ctypes.WinError(ctypes.get_last_error())
creationflags=subprocess.CREATE_NEW_CONSOLE
if event:
    creationflags |= subprocess.CREATE_NEW_PROCESS_GROUP
child=subprocess.Popen([runtime,'--config',config,'grok'],cwd=cwd,
    creationflags=creationflags)
ready=pathlib.Path(ready)
deadline=time.monotonic()+20
while not ready.exists() and child.poll() is None and time.monotonic()<deadline:
    time.sleep(0.05)
if not ready.exists():
    out,err=child.communicate(timeout=5)
    print(json.dumps({'driver_error':'vendor did not become ready','code':child.returncode,
                      'stdout':out,'stderr':err}))
    raise SystemExit(3)
kernel.FreeConsole.restype=ctypes.c_int
kernel.AttachConsole.argtypes=(ctypes.c_uint32,); kernel.AttachConsole.restype=ctypes.c_int
kernel.SetConsoleCtrlHandler.argtypes=(ctypes.c_void_p,ctypes.c_int); kernel.SetConsoleCtrlHandler.restype=ctypes.c_int
kernel.GenerateConsoleCtrlEvent.argtypes=(ctypes.c_uint32,ctypes.c_uint32); kernel.GenerateConsoleCtrlEvent.restype=ctypes.c_int
kernel.FreeConsole()
if not kernel.AttachConsole(child.pid):
    raise ctypes.WinError(ctypes.get_last_error())
handler_type=ctypes.WINFUNCTYPE(ctypes.c_int,ctypes.c_uint32)
handler=handler_type(lambda event: 1)
if not kernel.SetConsoleCtrlHandler(ctypes.cast(handler,ctypes.c_void_p),True):
    raise ctypes.WinError(ctypes.get_last_error())
if not kernel.GenerateConsoleCtrlEvent(event,child.pid if event else 0):
    raise ctypes.WinError(ctypes.get_last_error())
try:
    out,err=child.communicate(timeout=20)
except subprocess.TimeoutExpired:
    child.kill(); out,err=child.communicate()
    print(json.dumps({'driver_error':'runtime did not exit after console event','code':child.returncode,
                      'stdout':out,'stderr':err}))
    raise SystemExit(4)
print(json.dumps({'code':child.returncode,'stdout':out,'stderr':err}))
'''
        environment = {k: v for k, v in self.environment.items() if k != "PYTHONPATH"}
        result = subprocess.run([sys.executable, "-c", driver, self.runtime,
                                 str(self.config), str(cwd), str(self.ready), str(event)],
                                text=True, capture_output=True, env=environment, timeout=30)
        self.assertEqual(result.returncode, 0, result.stderr or result.stdout)
        self.assertEqual(json.loads(self.ready.read_text())["tty"], [True, True, True])
        return json.loads(result.stdout)

    def test_unmanaged_console_events_reach_vendor(self):
        unmanaged = self.base / "unmanaged"
        subprocess.run(["git", "init", "--initial-branch=trunk", str(unmanaged)],
                       check=True, text=True, capture_output=True)
        self.write_signal_config(unmanaged)
        for event in (1, 0):  # CTRL_BREAK, then broadcast CTRL_C in the private console.
            with self.subTest(event=event):
                outcome = self.drive_event(unmanaged, event)
                self.assertEqual(outcome["code"], 37, outcome)
                self.assertTrue(self.ready.with_suffix(".received").is_file())
        common = Path(subprocess.run(
            ["git", "-C", str(unmanaged), "rev-parse", "--path-format=absolute", "--git-common-dir"],
            check=True, text=True, capture_output=True).stdout.strip())
        self.assertFalse((common / "workspace-lifecycle").exists())

    def test_managed_console_events_are_forwarded_by_lifecycle(self):
        self.write_signal_config(self.topic)
        for event in (1, 0):
            with self.subTest(event=event):
                outcome = self.drive_event(self.topic, event)
                self.assertEqual(outcome["code"], 37, outcome)
                self.assertTrue(self.ready.with_suffix(".received").is_file())
                status = subprocess.run([self.lifecycle, "--repo", str(self.topic), "lease-status",
                                         "--task", "runtime"], text=True, capture_output=True,
                                        env=self.environment)
                self.assertEqual(status.returncode, 0, status.stderr)
                self.assertFalse(json.loads(status.stdout)["lease"]["busy"])


if __name__ == "__main__":
    unittest.main()
