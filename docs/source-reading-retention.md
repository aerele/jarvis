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

Status: the Claude plan source reads are off until an operator enables them for
a tenant (the per-tenant guard setting) and the fleet default is switched. The
plugin's Claude gate stays closed (D1 off) until it is enabled. Until then every
source read on a Claude plan tenant is refused, as it has been since 16.7.0.
Other models are not affected by any of this.

### Why they were refused

The Claude CLI writes its own transcript, tool results included, to the
container's persisted disk. Jarvis cannot redact that file. The plugin therefore
refused the four source tools on the Claude plan.

### What changes when the guard is active

The container keeps the CLI's live transcripts in memory (tmpfs). A guard in the
container makes a redacted copy and stores only that copy on disk. In the saved
copy, the file and report contents returned by `read_file`, `query_file`,
`query_report` and `get_file_pages` are replaced by the same source-reference
receipts that other models get (see the first section). The shared receipt shape
is defined by one contract file that the fleet agent, the plugin and this
backend all test against (`jarvis/tests/fixtures/source-contract-v1.json.b64`, base64 of the exact fixture bytes).
A later turn must query the source again under current permissions.

### What you may claim

"On the Claude plan with the guard active, file and report contents returned by
Jarvis's file tools are not written to persistent storage by the Claude CLI.
They are held in the container's memory while a chat's process is alive (the
process ends on the agent runtime's own idle timeout, as today) and for a short time
after it stops, normally about ten seconds, after which the saved transcript
holds only references. Transcripts written before this change are not rewritten
unless an operator runs the purge procedure."

Qualify the time. "About ten seconds" is the normal case. It can be longer in
these cases, always in memory only and never on disk:

- A background process started by the model's Bash inside a chat keeps a lock
  open. The raw copy for that chat stays in memory until that process ends or
  the container restarts. The guard counts this (`replace_blocked`) and alerts
  after 10 minutes. It never forces the replacement.
- A host without working file watching falls back to a timer, which can add up
  to about ten seconds before a copy is made.
- If the guard stops (crash loop, parked guard), the gate refuses source reads
  (within 60 seconds, or at the next call for a memory failure). A stale guard
  can stay trusted for at most 10 minutes. Memory-only data is lost with the
  container, never written.

### Do not claim

- Zero retention.
- Protection from operators, host root, or any code running as the container
  user. This includes the model's own Bash tool. It can read other users' live
  chats on the same tenant, as it can today.
- Coverage of assistant replies, thinking, compaction summaries, inline
  attachments, ordinary ERP reads, transcripts written before this change, or
  off-host backups.
- Files the model writes or sends anywhere.
- Output the model makes by transforming file data (jq, cut, awk, totals,
  re-encodings).
- Protection against a prompt-injected or adversarial model that uses Bash to
  copy data.
- Text the model reads off a scanned page image and types again.
- Short fields (under 39 normalised characters) copied into ERP write tools or
  any other tool input. They are below the detection bound.
- That the last one to two seconds of a chat survive an ungraceful kill. That
  gap is lost history, not a leak.
- That the process ends sooner than the agent runtime's idle timeout, or that an
  operator or agent outage keeps reads on. If the fleet agent is down for 10
  minutes, or its lease lapses after 30 minutes, source reads turn off (fail
  closed, the same as today).

### Known limits (accepted by the owner unless noted)

- About 12.0% of list-shaped results measured on the test corpus (54 of 451)
  become receipts in saved history. Field-subset list pages below the 39
  character bound stay as they are. The owner decision on this share is open.
- A text value with a very large number of delimiters (rare) sends that session
  to receipt-only saved history. The `leaf_values` counter tracks it.
- Auto-memory files are in memory only on these tenants. A container restart
  empties them. While any chat of the tenant is in receipt-only mode, memory
  writes get the fallback receipt text.
- A chat that the agent runtime recovers in a forked session after an ungraceful kill, or
  a chat after `/clear` (any session rotation), keeps receipts only for its
  saved history from that point. Rotations count as fork-suspect events and can
  raise the rollout stop rule. Operators lift a false stop by hand.
- A long indivisible job for one chat can delay saving for other chats of the
  same tenant. After 60 seconds the gate refuses reads for that tenant.
- Under memory pressure in the store, older copies become placeholders (small
  delay on resume). Near-full store refuses `get_file_pages`, then all source
  tools, until it drains. No process is stopped.
- Tenants below the memory floor (1536m, provisional) cannot use these reads.
- The host directory for agent runtime state has mode 0777 (existing, `main.py`). Any
  host process running as uid 1000 can write a tenant's store. Fix is tracked
  separately.
- The static no-follow checker does not cover 42 older functions in `main.py`.
- Alerts use the `cp-health` channel. After a channel dies, up to 90 minutes of
  enforcement can run with no working alert path.
- After a rare loss of worker state, clean notice results can be withheld once.
- Scanned-PDF page reads (`get_file_pages`) use the same profile as other
  models (no profile change).

### Older transcripts

Transcripts written before the guard is active are not rewritten. The release
renames the old projects directory (bytes untouched) and the chat still resumes
with its full history. New content after the flip is redacted. Chats older than
16.7.0 may hold file contents from before the refusal. An operator can run the
optional purge runbook per tenant. Transcripts written since 16.7.0 hold no
source data, because reads were refused.

### Operator requirements

- The fleet agent needs the `CAP_SYS_CHROOT` capability and unrestricted
  `unshare`. The current systemd root agent has both. A hardened unit that
  removes either breaks the guard setup. A fleet agent that does not run as root
  is not supported for this feature.
- Provision the `cp-health` alert secret (the control-plane consumer that
  acknowledges each hourly test delivery) before you enable the guard for any
  tenant. While the channel is unset or degraded, the guard is forced off
  everywhere and enforcement cannot be turned on.
- The guard stays off until the operator sets it for a tenant and the fleet
  default is switched. Turning it off is immediate for the gate (within about 40
  seconds) and needs no restart.
- Roll out in cohorts, with deploys to scratch tenants first and a soak of at
  least 3 days on them before cohort 0 starts.
- Rollback: guard off, then per-tenant return to the legacy layout (one
  recreate, no chat reset), then fleet-agent downgrade only after every Claude
  tenant is back on legacy.
- Disconnecting the Claude login wipes the memory store with the container. On
  these tenants the last step is a recreate instead of a restart.

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
