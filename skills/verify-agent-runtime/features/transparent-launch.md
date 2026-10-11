# Transparent launch

A user in a declared active workspace runs a standard name (here `codex`).
The vendor sees the same arguments, stdin, working directory and exit code it
would see if started directly. The only change is the native cwd option that
the runtime prepends. The run passes through the pinned lifecycle's
`resolve-run` and leaves no lifecycle state in an unmanaged repository.

Contract sources:

- `README.md`, the paragraphs on version-1 configuration, the pinned lifecycle
  and native cwd options.
- `src/agent_runtime/cli.py`, `_native_cwd` and the `resolve-run` launch in
  `_run`.
- `tests/test_runtime.py`, the `test_subprocess_transparent_unicode_cwd_stdio_exit`
  and `test_native_override_selects_nested_and_preserves_argv` tests.
- `tests/test_lifecycle_integration.py`, where unmanaged repositories create
  no state.

## Prerequisites

Python 3.10+ with `venv` and `pip`, Git, network access for the setuptools
build backend, and an absolute `workspace-lifecycle` executable. The helper
builds everything else in its scratch directory: the venv, the Git workspace
`workspace` with a `nested 日本語` directory, the recorder `vendor.py` and
`runtime.json`. The configuration declares only `codex`. Its argv is the
helper's interpreter plus `vendor.py`, and its lifecycle pin is the selected
executable's SHA-256.

## Operations and fixed expectations

| Step | Command and cwd | Expected observation |
| --- | --- | --- |
| Install | `<venv python> -m pip install --no-deps <copy>` | Installed `agent_runtime/cli.py` bytes equal the checkout's. `agent-runtime --version` equals `pyproject.toml`. |
| Launch | `codex exec "two words" 日本語` in `nested 日本語`, stdin `stdin 日本語\n` | Exit 23 from the vendor. The vendor ran once, in `nested 日本語`, with argv `--cd <nested> exec "two words" 日本語`. It received that stdin. The runtime's stdout is the vendor's JSON and its stderr includes `vendor-stderr`. |
| Native cwd | `codex -C "nested 日本語" exec x` in the workspace root | Exit 23. A second vendor call ran in the root with argv `-C "nested 日本語" exec x` unchanged. |
| Side effect | Read `--git-common-dir` of the workspace | No `workspace-lifecycle` directory exists. |

The recorder appends each call to `vendor-calls.jsonl`, so a duplicate or
missing launch changes the call count. The runtime environment drops
`PYTHONPATH` and `AGENT_RUNTIME_DEPTH` and sets `AGENT_RUNTIME_CONFIG` and
`PYTHONIOENCODING=utf-8`.

## Substituted boundaries and limits

The vendor is a synthetic recorder. Real Codex flag parsing, authentication
and model behavior are untested. The lifecycle is real, but the workspace is
unmanaged, so only its direct-run path is used. Task supervision and
`finish_argv` are untested. Console signals, Ctrl+C and the
POSIX `exec` handoff are not checked separately. A Linux pass needs its own
run.

The workspace root is ASCII on purpose. On Windows, workspace-lifecycle 0.3.1
(the version CI pins) exits 2 with a locale codec decode error (`cp932` here) when the root contains
日本語; 0.4.8 launches. That is a lifecycle limit, not this feature, so a
non-ASCII root is not tested here. The other entries in the [feature map](README.md) stay not-run.
