# Feature map

Terms follow [CONTEXT.md](../../../CONTEXT.md).

| Feature | User entry point | State in this verifier |
| --- | --- | --- |
| [Transparent launch](transparent-launch.md) | Installed `codex` with `AGENT_RUNTIME_CONFIG`, cwd inside an active workspace | Driven by `scripts/verify.py` |
| Workspace refusal | Same entry from an undeclared, held, pending, ambiguous or tool-disabled workspace | not-run. Issue #1 changes which of these refuse. |
| Pin refusal | Same entry with a lifecycle whose bytes differ from its pin | not-run |
| HANDOFF receipt | Launch with `handoff.owner: runtime`, `external`, or `--handoff-independent` | not-run |
| Maintenance commands | `codex --version`, `claude update`, `grok doctor` and similar forms, which skip workspace selection | not-run |
| Other standard names | `claude`, `grok`, `agent-runtime --config PATH TOOL` | not-run |
| Managed task launch | Launch inside a lifecycle task, whose vendor receives `WORKSPACE_LIFECYCLE_CONTEXT` | not-run. Covered by `tests/test_lifecycle_integration.py`. |
| `agent-handoff-receive` CLI | `agent-handoff-receive --workspace PATH` | not-run |

A row marked not-run has no evidence from this verifier. Add a profile
for one when a change to it needs proof through its own user entry point.
