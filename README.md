# Agent runtime

`agent-runtime` is an independently installable Python 3.10+ package that
connects the standard `codex`, `claude`, and `grok` names to explicitly
configured vendor executables. It does not import agent-rules, private
configuration, placement, inventory, or workspace state implementations.

Install it in an isolated environment:

```console
python -m pip install .
agent-runtime --help
```

Set `AGENT_RUNTIME_CONFIG` to an explicit version-1 JSON configuration, or
pass `--config PATH` to `agent-runtime` before the tool name. A configuration declares the
absolute vendor argv for each tool and, optionally, workspaces. A workspace binds
a pinned lifecycle to a directory tree. It is not a list of places or tools the
user may launch. The deepest workspace root containing the effective cwd, by
canonical real path, selects the lifecycle. Outside every root the vendor runs
directly, and one JSON `notice` on stderr says that no lifecycle supervises it.
The runtime never refuses a second session of the same or another tool.
Measured workspace-lifecycle 0.3.1 and 0.4.8 still refuse a second session in the
same managed task and any launch from an unbound checkout of a managed
repository. Those refusals belong to the lifecycle and are tracked in
[issue #1](https://github.com/yasuyuki/agent-runtime/issues/1).

Each workspace supplies a fixed, SHA-256 pinned lifecycle CLI. The runtime
calls its public `resolve-run` interface and never reads lifecycle state or Git
metadata. The lifecycle decides whether the directory is a managed task and
may itself refuse, for example a task whose retirement is pending. A HANDOFF receiver, when required,
is likewise a fixed, source-hash bound CLI; its owner is explicitly `runtime` or an attested external GUI owner.
Native cwd options remain in the vendor argv while the selected cwd is checked
before launch. Tool-specific maintenance commands can read the configured tool
binding while skipping malformed workspace declarations; they never select an
ambient `PATH` vendor.

The small schema keeps host-specific values in the configuration owner. This
illustrates its shape; every shown path is a placeholder and every pin is the
SHA-256 of the invoked source file.

```json
{
  "version": 1,
  "tools": {"codex": {"argv": ["/absolute/vendor/codex"]}},
  "workspaces": [{
    "root": "/absolute/workspace",
    "lifecycle": {
      "argv": ["/absolute/bin/workspace-lifecycle"],
      "source": "/absolute/bin/workspace-lifecycle",
      "interface": "resolve-run-v1",
      "pins": [{"path": "/absolute/bin/workspace-lifecycle", "sha256": "..."}]
    }
  }]
}
```

Version 0.2.0 required `state: "active"` and a `tools` list on every
workspace and refused everything else. Those fields are now ignored.

A workspace without a `lifecycle` binds none, so launches under it are
unmanaged too. The runtime refuses a launch only for an invalid configuration
or when running it would break a trust or evidence boundary. Examples are a
missing vendor binding, a vendor that recurses into the runtime, a native cwd
that does not exist, two native cwd options, a duplicate workspace root, an
unsupported lifecycle interface, a changed lifecycle or receiver pin, and an
unsuccessful HANDOFF receipt. A refusal is one JSON object on stderr with
`error` and, when the user or configuration owner can resolve it, `recovery`.

## Agent management

Agents manage work through the lifecycle, not through launch refusals.
`agent-runtime --explain TOOL [-- ARGS]` runs nothing. It prints the resolved
launch as JSON on stdout: vendor, effective cwd, workspace root, the verified
lifecycle argv, HANDOFF route, final command and notices. It exits 2 with the
same refusal object when the launch would be refused. An agent appends
lifecycle subcommands to that argv, such as `--repo ROOT status`,
`lease-status`, `lease-release`, `hold`, `release-hold` and `finish`.
Their contracts belong to workspace-lifecycle. Inside a managed task the
vendor also receives `WORKSPACE_LIFECYCLE_CONTEXT` with its `finish_argv`.

The command help is the authoritative reference for launcher arguments. The
runtime does not generate configuration, scan environments, adopt workspaces,
look up versions or models, or finish lifecycle work. A lifecycle finish needs
the separately reviewed plan and result evidence required by its owner.

## Adoption and rollback boundary

Version 0.3.0 is source-only, not a confirmed PyPI publication. Build the wheel
from the accepted source revision and install it alongside the separately pinned
[workspace-lifecycle 0.3.1](https://github.com/yasuyuki/workspace-lifecycle) wheel in an isolated environment. Pins identify the invoked
entry and any adopted package files requiring drift detection; the environment
owner controls their installation and source revision. Normal launch never
repairs changed pins or grants trust.

For HANDOFF, use the installed receiver module with `owner: "runtime"`, `argv`,
`source` and `source_sha256`; the runtime passes the selected workspace root.
A GUI-owned route uses `owner: "external", attested: true` only after its adoption
proves receipt before CLI launch. This is a route-level declaration, not a receipt
created by the runtime. Do not use that route outside its GUI precondition.
For independent work after receipt failure, explicitly launch through
`agent-runtime --handoff-independent`; the warning remains on stderr.

Switch a consumer only after checking its package/config/entry identity and
synthetic acceptance. Keep its previous entry, config, hooks and state together
until that separate live acceptance. Revert using that preserved set only after
preserving and reconciling work created after the switch; do not overwrite new
results. This source change does not switch Windows or Linux consumers.

The runtime selects a lifecycle for the launch; it does not confine the
session afterward. Native changes of directory or creation of other worktrees remain vendor behavior. Lifecycle
context exposes its fixed `finish_argv`; a reviewed plan and result reference
remain mandatory. Child exit alone does not finish a task. Neither this adapter
nor lifecycle intercepts arbitrary direct Git operations or guarantees cleanup
after an uncatchable forced termination.

## HANDOFF receiver and source ownership

This repository is the sole current editing and distribution owner of the
runtime and its HANDOFF receiver. Initial source was extracted from
[yasuyuki/agent-rules](https://github.com/yasuyuki/agent-rules) revision
`8f9e930bb77fb27d2db2a958aafa971661108013`,
`packages/agent-runtime/` and `bin/handoff_receive.py`.
Historical consumers may retain their fixed old revisions. Version 0.2.0 adds
the receiver as a supported package surface; receipt behavior is unchanged.

`agent-handoff-receive --workspace /absolute/workspace` and
`python -m agent_runtime.handoff --workspace /absolute/workspace` return
one JSON result, with exit 0 for not-configured, updated or unchanged, and
exit 1 for unsuccessful receipt. The library entry is
`agent_runtime.handoff.receive(workspace)`. The receiver uses Git and the
workspace's explicit binding; it never executes received Markdown.

For the runtime's fixed-source CLI interface, set HANDOFF `argv` to the
installed Python executable and the absolute installed `agent_runtime/handoff.py`
file, and set `source` and `source_sha256` to that same file and its digest.
This pins the invoked implementation, rather than only a console-script shim.
Private bindings and shared documents stay with their environment owner.
Local sections, conflicts, stale/diverged delivery and interrupted publication
retain the existing receiver contract. An unsuccessful receipt never becomes
a claim of freshness; the explicit independent-work exception remains visible.

CI builds a wheel and tests its installed package outside the source checkout
on Linux and Windows with Python 3.10 and 3.12. Lifecycle integration uses the
fixed public 0.3.1 source artifact, without copying its implementation.

## Verification

[verify-agent-runtime](skills/verify-agent-runtime/SKILL.md) installs a
checkout and proves a transparent `codex` launch through a selected lifecycle,
with retained evidence. Its [feature map](skills/verify-agent-runtime/features/README.md)
lists the entry points it leaves not-run. Terms follow [CONTEXT.md](CONTEXT.md).
