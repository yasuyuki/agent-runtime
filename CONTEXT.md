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
A configured root bound to a lifecycle. The deepest root containing the
effective cwd selects it. It informs supervision and never grants or denies a
launch.
_Avoid_: project, worktree, task, allowlist

**Unmanaged launch**:
A launch whose effective cwd is outside every workspace. The vendor runs
directly, with a notice that no lifecycle supervises it.

**Refusal**:
A launch the runtime will not start because it would break a trust or
evidence boundary. It names the error and, when one exists, the recovery.

**Launch plan**:
The resolved vendor, workspace, lifecycle, HANDOFF route and final command for
one launch, reported by `--explain` without running anything.

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
