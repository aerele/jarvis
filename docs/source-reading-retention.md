# Source-read retention

`query_file`, `query_report`, and `read_file` return source data to the authorized
live tool call. New bench tool receipts retain bounded source references and
coverage metadata. They do not retain rows, samples, text, SQL, or source-bearing
error messages. Activity events use the same minimized receipt. Other tools and
intentional assistant replies keep their existing behavior.

The paired plugin registers synchronous `tool_result_persist` and
`before_message_write` hooks. These minimize gateway-owned tool-result records
and source tool-call arguments. The live result is not mutated. A later turn must
query the original source again under current permissions and pin the returned
version. A shared retry of a minimized receipt returns `SourceReadRequired`; it
cannot replay a stored reference as a complete query result or execute the same
call again.

These changes apply to new writes. They do not delete earlier transcripts,
backups, original uploads, report artifacts, or user-requested exported files.
Gateway hooks do not establish retention guarantees for native Claude CLI or
other provider-owned transcripts. Those paths need separate lifecycle checks.
An older runtime without the persistence hook API logs an explicit compatibility
warning. The paired plugin refuses source reads before network dispatch until
the runtime is upgraded. Other tools retain their existing behavior.

Compaction must be checked against the deployed runtime's persisted-history path.
Unit checks of the redactor alone do not prove that a native runner or an existing
historical summary contains no source values. Do not claim universal zero
retention from this hook.

The file and report child processes disable core dumps before reading input.
Their 512 MiB address-space limit is enforced on Linux. macOS does not have this
address-space guarantee; byte, row, SQL-work, and parent wall limits still apply.

## Paired rollout

Use a maintenance window for the source-reader release group. Pause chat and
agent traffic before deploying the backend. A completed `run_report` now returns
metadata and requires `query_report`; an old plugin cannot finish that handoff.

1. Deploy the backend file-query change (#2245), then the prepared-report change
   (#2247). Run `bench migrate` and restart the backend workers.
2. Deploy the matching plugin file-query change (#136), then the prepared-report
   change (#137). Restart the gateway. Require a runtime that supports the
   synchronous persistence hooks; a function named `api.on` alone is not proof.
3. Compare `jarvis/tools/tool-names.json` with the plugin's
   `contracts/tool-names.json`. Both must contain `query_report` and have the same
   digest. Check a completed report through the live tool path, then check the
   stored receipt and transcript with a non-sensitive canary value.
4. Deploy the agent bundles and Admin provisioning changes only after those tools
   are registered. Refresh role profiles and confirm the effective permissions.
5. Resume traffic only after the paired checks pass. Keep archive reads off until
   their separate backend/plugin release and runtime checks pass.

For rollback, pause traffic and restore a matching backend/plugin pair before
resuming it. A backend-only or plugin-only rollback is not a supported mixed
version state. This procedure does not delete historical source data.
