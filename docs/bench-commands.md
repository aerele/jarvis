# jarvis — bench commands

Custom `bench` CLI commands this app registers. All target one site: `bench --site <site> <command>`.

> **Keep this in sync.** Every command in `jarvis/commands/__init__.py`'s `commands` list MUST be
> documented here — `jarvis.tests.test_bench_commands_documented` fails CI otherwise.

## Custom CLI commands

### `reset-onboarding`
DEV: flush a tenant site's Jarvis setup so the onboarding wizard runs fresh — connection + LLM
credentials, and (unless `--keep-data`) all workspace content (chats, skills, macros, triggers, learning,
wiki, dashboards). Admin-side records are NOT touched — use the control plane's `purge_customer` for that.

| Option | Meaning |
|---|---|
| `--force` | Skip the confirmation prompt. |
| `--keep-data` | Keep workspace content; only clear the connection + LLM setup. |

```bash
bench --site jarvis_2.local reset-onboarding             # prompts to confirm; wipes content
bench --site jarvis_2.local reset-onboarding --keep-data # only clear connection + LLM setup
bench --site jarvis_2.local reset-onboarding --force     # no prompt
```

## Related (control plane)
Onboarding a tenant also involves the control plane (`jarvis_admin_v2`) — its `purge_customer` erases the
admin-side customer, and it owns the billing/fleet bench commands. See
`jarvis_admin_v2/docs/bench-commands.md`.
