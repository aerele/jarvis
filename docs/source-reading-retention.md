# Source-read retention

`query_file`, `query_report`, `read_file`, and `get_file_pages` return source data
to the authorized live tool call. New bench tool receipts retain bounded source
references and coverage metadata. They do not retain rows, samples, text, SQL,
page images or base64 data, or source-bearing error messages. Activity events use
the same minimized receipt. Other tools and intentional assistant replies keep
their existing behavior.

Small UTF-8 JSON, Markdown and TXT attachments (up to 20,000 characters) remain
fenced inline prompt input. Larger documents use the source readers. Inline
attachment text is user input, not a source-tool receipt, and can remain in the
runtime's prompt history. The source-tool storage hooks do not redact that input.

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
Protected reads require gateway runtime 2026.9.3 or later, the Jarvis plugin's
explicit conversation-access hook permission, and runtime trajectory capture
switched off before gateway startup. Native trajectory snapshots
bypass the transcript hooks. The plugin refuses reads while capture is enabled
or its setting is absent. It also refuses reads on CLI or unknown runners.
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

## Claude plan (subscription) tenants

Status: on Claude plan tenants (the `claude-cli` runtime), the source readers work as on every
other model. The Claude CLI does not save chat sessions, so Jarvis keeps the same retention
behaviour as above.

### How it works

- The fleet agent starts every Claude CLI run with `--no-session-persistence`. The CLI keeps the
  chat in the memory of its live process and does not write it to disk.
- A Claude Code `SessionStart` hook restores the chat when a new CLI process starts (after an
  idle exit or a container restart). It reads the user and assistant text of that chat from the
  gateway's own transcript, which already holds source receipts in place of source data.
- The hook leaves a content-free placeholder log per session (text `[history held by Jarvis]`),
  so the gateway keeps resuming the session. The placeholder carries the Claude account id; a
  restore happens only for the same account.
- Large tool results that the CLI writes to a side file go to a memory-only folder.
- The CLI's own file, shell and agent tools are denied. Claude reads data only through the
  Jarvis tools.
- On the first boot with this release, existing CLI session logs are replaced by placeholders.

### What you may claim

"On the Claude plan, the Claude CLI does not store chat sessions or file and report contents on
disk. File and report contents returned by Jarvis's source readers are stored only as source
references, as on every other model."

### Do not claim

- That inline attachments (small JSON, Markdown and TXT files) are minimized. They are user input
  and remain in the gateway's prompt history, as on every other model.
- Protection from operators or host root.
- That a restored chat holds earlier tool results. After a CLI process restarts, Claude gets the
  earlier messages as text; it must query the source again for data.
- Coverage of files the model writes or sends anywhere.

### Known limits

- Manual compaction (`/compact`) on a Claude plan chat fails fast ("Claude CLI did not confirm
  that native compaction ran"). The chat continues; the restored history is bounded instead
  (at most six parts of 9,000 characters, newest first).
- The placeholder format follows the pinned Claude CLI version. A CLI version change must re-run
  the restore checks.

### Release order

The fleet release with `--no-session-persistence` and the restore hook must be live on every
host with a Claude plan tenant before the plugin release that admits source reads on
`claude-cli`. A plugin of that release on an older fleet would let the CLI save source data to
disk.

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
