# Agent runtime

Connects standard agent CLI names to explicitly configured vendor executables
and hands each launch to a pinned workspace lifecycle.

## Language

**Standard name**:
A user-facing command name (`codex`, `claude`, `grok`) that the runtime owns
instead of the vendor.
_Avoid_: alias, shim, wrapper

**Vendor**:
The configured executable that implements a standard name, given as an absolute argv.
_Avoid_: backend, real CLI

**Workspace**:
A configured root with a state, enabled standard names and a lifecycle. The
deepest unique root containing the effective cwd selects it.
_Avoid_: project, worktree, task

**Lifecycle**:
The external, hash-pinned executable whose `resolve-run` interface receives each launch.
_Avoid_: launcher, supervisor

**Launch cwd**:
The directory the user invoked the standard name from.

**Effective cwd**:
The directory a native cwd option selects, resolved against the launch cwd.
Without such an option it equals the launch cwd. It selects the workspace.
_Avoid_: working directory (without qualifier)

**Maintenance command**:
A fixed vendor argv form (such as `--version` or `update`) that runs without
workspace selection.
