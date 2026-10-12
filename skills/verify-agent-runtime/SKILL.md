---
name: verify-agent-runtime
description: Verify that an agent-runtime checkout, installed as a package, launches a vendor transparently through its standard `codex` entry and a selected workspace-lifecycle, with disposable data and retained evidence.
license: MIT
---

# Verify agent-runtime

Install the selected checkout into a new venv and drive its standard `codex`
console entry the way a user does: `AGENT_RUNTIME_CONFIG` names a version-1
configuration, and `codex ...` runs inside the declared workspace. The vendor
is a synthetic recorder, and the lifecycle is the real executable you select.
Read [the feature map](features/README.md) for what this proves and what it
leaves not-run. The recipe passed on Windows 11 with Python 3.14 and
workspace-lifecycle 0.4.8 and 0.5.1. That result is not evidence for a later run.

Requirements: Python 3.10+ with `venv` and `pip`, Git, network access for
pip to fetch the setuptools build backend, and an installed
`workspace-lifecycle` executable that provides `resolve-run`. No vendor CLI,
agent authentication or private configuration is used.

From the selected `agent-runtime` checkout:

```sh
python skills/verify-agent-runtime/scripts/verify.py --repo . \
  --lifecycle /absolute/path/to/workspace-lifecycle \
  --evidence-root /absolute/evidence/parent --agent "<driving agent and model>"
```

`python` is the interpreter you choose to build the venv; on Windows give
its absolute path and write the command on one line. Pass the lifecycle
explicitly; do not infer it from `HOME` or `PATH`. When `dirty` is true,
the revision does not identify what ran; report `inputs_sha256` with it. For a
distributed copy of this skill, run its own `scripts/verify.py` and still pass
the checkout with `--repo`. `--doctor-only` records the target identity and
exits without driving. `--help` is the authority for options.

## What the helper does

1. **Doctor.** Records the checkout revision and dirty state (`unknown` outside
   Git), SHA-256 of `pyproject.toml`, every `src/**/*.py` and the helper, and
   the lifecycle path, hash and `--version`. A missing input is `blocked`.
2. **Launch.** Copies the checkout into a new scratch directory, creates a venv
   and runs `pip install --no-deps` from that copy. The checkout itself gets no
   build output. It asserts the installed `cli.py` bytes and `--version` match
   the checkout.
3. **Drive.** Creates a Git workspace `workspace/nested 日本語`, a recorder
   vendor and a configuration that pins the lifecycle hash. It then runs the
   installed `codex` twice with fixed expectations from
   [transparent launch](features/transparent-launch.md).
4. **Evidence.** Writes `result.json` before the first operation and after
   every command and assertion. It copies `runtime.json`, `vendor.py` and
   the recorder's `vendor-calls.jsonl` beside it. The judgement compares the
   recorder's observed cwd/argv/stdin and the exit code with literal values.
   A matching exit code alone never passes.
5. **Cleanup.** Removes only its own scratch directory (venv, source copy and
   workspace), including after a failure. It then checks that every listed
   artifact still exists.

The helper prints `{"status", "result"}`. Exit codes 0, 1, 2 and 3 mean
`pass`, `fail`, `blocked` and `not-run`. Before you report, read
`result.json`. Check `status`, each assertion, `cleanup.ok`,
`evidence_retained` and `not_run`. A failed cleanup or missing artifact is
`fail` even when every assertion held. Each session that needs proof runs the
helper itself; an earlier `result.json` is not current evidence.

Run `python tests/test_verification_skill.py` when changing the helper. It
needs an installed `workspace-lifecycle` entry beside the interpreter or on
`PATH`. It checks the detector: the checkout passes, and a disposable copy
that drops the last vendor argument but keeps the vendor's exit code fails.
A missing lifecycle is blocked, and Doctor-only is not-run. These tests do
not replace a feature run.
