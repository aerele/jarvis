# Chat failure messages

Chat errors use `jarvis/public/js/turn_error_rules.mjs` as the ordered source
of classification rules and customer copy (a JSON literal exported as an ES module
for compatibility with Frappe’s older bundler). Python's `chat/error_taxonomy.py`
and the shared `public/js/turn_errors.mjs` formatter consume it. Desktop chat,
Desk chat, dashboard chat, and mobile chat use the shared formatter.

## Covered failures and next steps

| Failure | Customer action |
| --- | --- |
| All model / failover attempts failed | Administrator reviews each connection’s error |
| Runtime file permissions / read-only filesystem | Administrator checks file ownership and permissions |
| Full disk / disk quota / locked or corrupt database | Administrator checks storage and database |
| Missing runtime files / missing credentials / pairing | Administrator repairs assistant setup |
| Assistant unreachable | Retry, then check the assistant service connection |
| Recovery window expired | Check completed actions before sending again |
| Context limit | New chat with a shorter summary or less attached content |
| Request too large | Smaller files or split the request |
| Insufficient credits / billing / spending cap | Model account owner checks billing |
| Rate limit / cooldown | Wait or use another available model |
| Usage quota | Wait for reset or ask the account owner to review limits |
| Invalid or expired authentication | Administrator reconnects the affected service |
| Expired chat-subscription sign-in (OpenAI, Anthropic, xAI Grok, Kimi) | Administrator reconnects it in AI models; a member is told to ask their administrator |
| Safety rejection | Review content or contact the provider |
| Missing / unsupported / inaccessible model | Select another model or check its configuration |
| Access denied | Administrator reviews access to the model, service, or record |
| Invalid request / unsupported format | Check inputs; escalate persistent failures |
| Missing resource | Check resource existence and configuration |
| Service overload / HTTP 5xx | Wait and retry; check an identified provider's status |
| Timeout | Retry or reduce request size; check an identified provider's status |
| Connection / DNS / TLS failure | Retry, then have an administrator check the connection |
| Worker exception | Retry; send persistent error details to support |
| Empty reply from the model | Retry; if it returns, start a new chat or choose another model |
| Empty reply after tool actions | Check for completed actions, then retry |
| Cancelled | Muted cancellation, without retry advice |
| Unknown | Explain that the error did not establish a clear cause |

Classification reads at most the first 8 KB of the error text and matches
ASCII-only on both engines (Python uses `re.ASCII`), so a reload agrees with
the live event even for non-ASCII text. `message.provider` is stamped only on
replies that finished; on failed rows the provider is usually inferred from the
text, and a text that names two routing hosts links to neither.

More specific rules precede broad HTTP codes: disk quota is storage, not model
quota; `429 insufficient_quota` is billing, not a transient rate limit; a 404
with `model_not_found` is a model configuration failure. A bare 429 cannot
reliably establish whether a provider's allowance is temporary or monetary.

The legacy `gateway` and `provider` codes are retained for compatibility, but
no longer imply a temporary local fault or a busy model. Old live events with
these broad codes are refined from the error text. Specific live codes remain
authoritative. An `AgentUnreachableError` wrapper only overrides a generic
error or timeout; a specific rejection such as invalid credentials survives.

Immediate Retry buttons are hidden for known failures requiring input,
configuration, access, or account changes. Raw error details remain available.
After a runtime error, the pump sends a turn again by itself in two cases only,
once per turn: the runtime refused to resume a CLI live session (always on; log
line `pump redispatch_refused_session run_id=<id>`; the turn keeps its recovery
budget), and the plain empty reply (off unless a site turns it on; the turn gets
a fresh recovery budget, see below).

Retry (desktop chat and dashboard chat) runs the failed turn again. Where Turn
rows exist (the admission and pump paths), it uses the context and the original
attachments of the failed turn; pure legacy writes no Turn rows, so a retry
there has neither.

The server refuses a retry with one of these codes:

- `seed_missing`: the user message of the failed turn no longer exists.
- `owner_mismatch`: that message has a different owner than the conversation.
- `in_progress`: another turn has not finished (the states a send waits for; a
  `finalizing` turn does not count). On the pump the failed reply is bound to
  its own turn: that turn gives the user message to run again, and any other
  unfinished turn blocks. Without a bound turn (Phase-0, a site cut back from
  the pump) only turns created after the failed reply block, and the user
  message right before the reply is run again. This misses one case: a turn
  still queued when the failed turn wrote its reply.
- `not_latest`: the failed reply is not the latest visible user or assistant
  message (the closing message of a macro run does not count).
- `busy` (legacy path): a reply is still streaming in the chat.
- `no_seed`: no user message comes before the failed reply.

On the admission and pump paths the checks run under the conversation row lock,
so two tabs cannot start two retries, except on the legacy fallback during a
cutover; the legacy path checks without the lock.

One documented edge: on the legacy path a dead worker can leave a blank
streaming row after the failed reply. Desktop chat hides that row and shows
Retry; the server refuses with `busy` while the row is fresh
(`_INFLIGHT_FRESH_SECONDS`), then with `not_latest`. There the row is the only
guard against a duplicate run.

### Empty reply: one automatic re-send

When the model gives an empty `stop` reply, the agent runtime sends it to the
model again once inside the turn. The runtime does not do this when the model
stops at its output limit with no text (the failure seen on 2026-10-07). When
the runtime has no reply, it ends the turn with "Agent couldn't generate a
response. Please try again.". On the pump, a site can let the bench send that
turn again one time (`jarvis/chat/empty_reply_recovery.py`): same Turn row and
reply placeholder, gateway key `<run_id>-r1`, a fresh recovery budget. For the
output-limit failure, this re-send is the only automatic retry (one more model
call). The user sees a longer wait, then the answer. If the second attempt also
fails, the error card shows as usual. While the switch is off, nothing changes
and the re-send writes no log line.

The empty reply has one reading, `variant`: `plain` (the sentence and "Please
try again."), `bare` (the sentence alone), `tools` (the form that says tool
actions may be done) or `other` (more text around the sentence). The reading
looks at the first 300 characters only. Only `plain` is sent again, and only
when the whole text is 300 characters or fewer. All of these must also be true:

- Site config `jarvis_empty_reply_resend` is an on value.
- The terminal is an error with no text.
- This is the first attempt: the turn was not sent again before, for any
  reason, and it has a stored dispatch payload.
- The attempt showed no step line (a status line such as "compacting" does not
  count), and the pump did not re-attach the turn after a hop.
- The turn is interactive and its user message is one a person typed in the
  chat (`origin` `human`). Board answers, File Box drops, agent findings,
  delegated sends, macro steps, continuations, system turns and legacy rows with
  no `origin` are never sent again.
- The chat is not armed, has no live macro run and is not a File Box chat.
- The breaker of the relay target is closed (below).
- No tool row came after the reply placeholder, except the memory hook's
  `recall` (written with no call id). No confirmation card is parked.
- No other attempt at the same user message is unfinished, and no earlier one
  ended with the plain empty reply.
- The last finished turn before it in the chat (`done`, `errored` or
  `cancelled`), when it is less than 24 hours old, did not end with an empty
  reply (either form).
- The turn applied no frame (delta or tool event), has no Stop, and its row did
  not change since the pump read it.

The breaker reads the settled Turn rows: when 3 turns of one relay target that
were sent again got a second empty reply (either form) in the last 10 minutes,
the re-send is off there until they are older. In Phase 0 a site has one relay
target, so the breaker is per site. It cannot count a turn twice, a cache clear
does not reset it, and it cannot be reset by hand: to stop the re-sends before
it closes, turn the switch off. When the rows cannot be read, it counts as open
(no re-send).

Stop on a turn that waits in `ready` to be sent again cancels it there, and the
reply shows as stopped. Stop also aborts the `-r1` run of such a turn in the
same chat. Any other turn, and every turn while the switch is off, is stopped as
before. The legacy path (pump off) never sends again.

#### Turning it on and off

- Off is the default. On: `bench --site <site> set-config jarvis_empty_reply_resend 1`
  (`-g` for every site of the bench). Off again: `bench --site <site> set-config
  jarvis_empty_reply_resend 0`. On values: `1`, `true`, `on`, `yes`. Absent, `0`,
  `false`, `no`, `off`, empty or any other value: off.
- A config apply that writes `site_config.json` again must keep
  `jarvis_empty_reply_resend`, or the re-send is off from the next hop.
- No restart. Each pump hop reads site config when it starts, so a change
  applies from the next hop, within about 2 minutes.
- To check: `bench --site <site> execute jarvis.chat.empty_reply_recovery.switch_state`
  prints `on` or `off`, the value a new pump hop reads. While it is on, each
  plain empty reply on the pump writes an `empty_reply decision=` line.

#### Is it worth it

The source of truth is the Turn rows: ok / (ok + failed), `stopped` in neither.

```sql
SELECT state, COUNT(*) FROM `tabJarvis Chat Turn`
WHERE JSON_VALUE(dispatch_payload, '$.redispatch_reason') = 'empty_reply'
  AND state IN ('done', 'finalizing', 'errored', 'cancelled')
  AND creation > NOW() - INTERVAL 7 DAY
GROUP BY state;
```

`done` or `finalizing` = ok, `errored` = failed, `cancelled` = stopped. The
states filter leaves out turns still in flight. No rows: no turn was sent again
in the period (the switch is off, or no plain empty reply passed the rules). The
`empty_reply outcome=` lines give the same split where finalize ran.

#### Known limits

- The runtime sends an empty `stop` reply again once inside the turn, so a
  re-send after that is one or two more model calls on the same prompt. The
  runtime does not retry a reply cut at the output limit with no text (the
  failure seen on 2026-10-07); for that failure this re-send is the only
  automatic retry (one more model call).
- A write still running on the bench, with no relayed tool event and no
  receipt row yet, cannot be seen.
- The second attempt repeats the user message in the agent runtime's
  transcript, as Retry does. The memory hook can write a second `recall` row.
- A Stop that lands at the same moment as the empty reply can end the turn with
  the error card instead of "stopped", as for any error at that moment.
- A turn recovered after a takeover ends with the "interrupted" error, which
  the breaker does not count.
- When the switch goes from on to off, a turn already put back to `ready` is
  still sent, and Stop leaves it to the pump as for any `ready` turn. A turn
  sent again before the change still writes its `empty_reply outcome=` line
  and `redispatch_reason=` on its `pump turn_finalized` line.
- Before this change, and still: a turn that shows a step line and then errors
  with no reply text can wait on the desktop chat for the silent-run resync
  before its error card shows.

### Log lines

These lines go to `logs/jarvis.chat.latency.log` (in the bench `logs/` and the
site `logs/`):

- `retry_refused conversation=<name> message=<name> reason=<code> seed=<name>
  blocker=<run_id>:<state>`: one line for each refusal with a code listed
  above. `blocker` is empty except for `in_progress`; `seed` is empty for
  `busy` and `no_seed`. `owner_mismatch` and `seed_missing` are at WARNING.
  These refusals write no line: site busy, compacting, the entitlement check
  (`validate_can_send`, for example a suspended subscription), an armed macro
  run, "only assistant messages can be retried", "message did not error", and
  an overload reject from `accept_or_queue` or the legacy cutover gate.
- `retry_accepted_write_failed conversation=<name> write=skill_run|last_active_at`
  (WARNING): a write after an accepted retry failed; the retry still runs.
- `retry_inputs_unreadable conversation=<name> message=<name>` (WARNING): the
  stored payload of the failed turn is not a JSON object, so the retry has no
  context and no attachments.
- `empty_reply run_id=<id> conversation=<name> variant=<v> last_total_tokens=<n>
  context_pct=<pct>`: one line per empty reply, also for an attempt the pump
  sent again. `variant` is `plain` (with "Please try again."), `bare` (the
  sentence alone), `tools` (actions may be done) or `other` (more text around
  the sentence). The token values are the chat session's values after its
  previous turn; `context_pct=0` means the capacity is not known.
- `empty_reply decision=resent|skip reason=<r> run_id=<id> conversation=<name>
  target=<relay target>`: one line per empty reply on the pump while the
  re-send is on (none while it is off). `resent` has `reason=plain`. Skip
  reasons: `variant`, `used` (this turn was already sent again), `steps` (the
  attempt showed a step line, or the turn was re-attached), `class`, `origin`
  (not a message a person typed), `armed`, `file_box`, `breaker`,
  `tool:<name>` (the name cleaned to one token), `card`, `retried` (another
  attempt at the same user message is unfinished or got the plain empty reply),
  `chat` (the turn before it in the chat ended with an empty reply),
  `no_payload` (no stored dispatch payload), `cas_lost` (a frame was applied,
  Stop was set or the row changed since the pump read it) and `error` (WARNING,
  with the traceback; the turn settles as before).
- `empty_reply outcome=ok|failed|stopped code=<code> run_id=<id>
  conversation=<name> target=<relay target>`: when finalize runs for a turn
  sent again for an empty reply. `code` is the error code the user saw
  (`failed` only). A replayed finalize can write it again; the Turn rows are
  the record.
- `empty_reply breaker=open target=<relay target> count=<n>` (WARNING): the
  breaker refused a re-send. It also writes the Error Log
  `jarvis.empty_reply.breaker_open` (reference: the relay target), at most one
  per relay target per 10 minutes.
- `empty_reply breaker=unreadable target=<relay target>` (WARNING): the Turn
  rows could not be read; the breaker counts as open.
- `empty_reply breaker_alert_failed target=<relay target>` (WARNING, with the
  traceback): the Error Log could not be written; the breaker still refused.
- `pump turn_finalized run_id=<id> errored=<0|1>`: one line per finalized turn.
  For a turn sent again for an empty reply it ends with
  `redispatch_reason=empty_reply`. The refused-session re-send adds no reason;
  it logs `pump redispatch_refused_session`.

## Macro runs waiting for a report

A macro step that needs a Prepared Report (one that takes longer than a tool call
can wait) asks for a copy made at or after the run started. The run then parks in
status `waiting_report` without holding a worker or a turn. The Runs tab, the
macro's page and the Macros list show "Waiting for report: <names>", and the names
come from the run row, so they survive a reload.

How it moves on:

- A `Prepared Report` update wakes the parked runs that await it (one pending wake
  per run, job id `macro-report-wake-<run>`, so a burst of updates is one job). A
  wake that finds the run lock held leaves the run to the next cron.
- The `*/5` cron `jarvis.chat.macro_report_wait.resume_report_waits` is the
  guarantee; the wake is only speed. When every awaited report is Completed it
  sends one continuation turn to the same chat, and the same step goes on. When one
  failed, was cancelled or was deleted, the chat is disarmed and the model gets one
  turn with no write or email tool to say why, then the run ends `failed`.
- A report still Queued or Started is waited for until its `Report.timeout` (25
  minutes when unset) plus 60 minutes, and never past 6 hours. After that the run
  ends `failed` ("did not finish in time"). The hourly stale-run sweep closes a run
  still parked 7 hours after it parked.
- A site that is overloaded, or whose Relay Pump is draining, leaves the run parked;
  nothing is lost.
- The owner is told (Notification Log) of every ending of a run that waited,
  including their own Stop.

What to check:

- `Report.timeout`. Frappe kills a generation after the report's timeout, 25
  minutes when unset. A report that needs longer must have `Report.timeout` raised
  first, or every run waiting for it ends "did not finish in time".
- The scheduler. The cron and the update hook both depend on it: while the scheduler
  is paused or the `short` queue is down, parked runs wait and nothing is lost; they
  go on when it resumes (a run past its deadline ends then).
- Kill switch: `bench --site <site> set-config jarvis_macro_report_wait 0` (also off
  on a bench without the turn machine). New steps then never park and a report is
  served as before. The switch is not looked at by the wake-up or the cron: a run
  already parked is still continued when its report is ready (its step is then read
  without the freshness bound). To end such runs instead, run `end_all_report_waits`
  (below).

Two helpers, run from the bench:

```bash
# read only: each waiting run, the status of every report it awaits, since when, deadline
bench --site <site> execute jarvis.chat.macro_report_wait.report_waits

# ends every waiting run failed with a clear reason, tells the owner, disarms the chat
bench --site <site> execute jarvis.chat.macro_report_wait.end_all_report_waits
```

`end_all_report_waits` answers `{"ended": [...], "skipped": [...]}`. A skipped run was
being moved by another worker or raised; run it again. Use it after switching the kill
switch off, and before rolling the release back (an older release does not know
`waiting_report`, so nothing would ever continue those runs).

Error Log titles (fixed, at most one per run or chat an hour; the run is in the body):
`Jarvis: macro report wait expired`, `Jarvis: macro report resume compensated`,
`Jarvis: macro report resume not delivered`, `jarvis macro report resume failed: <run>`,
`jarvis macro report wait end failed: <run>` (`end_all_report_waits` could not end a run),
`jarvis macro report wake failed` (the wake-up could not be queued; the cron still
continues the run, within five minutes), `jarvis macro report wait guard failed`,
`jarvis macro report wait read failed` and `jarvis macro report wait row skipped`
(these last three fail open: the step is not blocked, and the guard is not applied).

## Customer wording

Headlines state the failure; the next line gives the customer an action.
Technical details remain collapsed. Messages do not claim that the cause is
temporary, a company-wide outage, or the customer's fault without evidence.
When a provider is known, timeout and connection headlines name it while
retaining the guidance for that failure type. A status link supplements that
guidance instead of replacing it.

## Provider status links

Links indicate where to check reported incidents, **not confirmation of an
outage**. Destinations are allowlisted and open in a separate tab with
`noopener noreferrer`. No URL supplied in an error is trusted.

Official pages checked on 2026-09-08:

- [OpenAI / ChatGPT](https://status.openai.com/)
- [Claude / Anthropic](https://status.claude.com/)
- [Google Gemini](https://aistudio.google.com/status), linked by the
  [Gemini troubleshooting guide](https://ai.google.dev/gemini-api/docs/troubleshooting)
- [OpenRouter](https://status.openrouter.ai/)
- [Mistral AI](https://status.mistral.ai/)
- [Groq](https://groqstatus.com/)
- [Together AI](https://status.together.ai/)
- [DeepSeek](https://status.deepseek.com/)
- [xAI](https://status.x.ai/)
- [Moonshot / Kimi](https://status.moonshot.cn/) (verified 2026-09-09)

Actual persisted provider metadata takes precedence over provider names in
error text. Recognized routing services (e.g. OpenRouter serving Claude) take
precedence over the model author. Unknown, ambiguous, local, and custom
OpenAI-compatible hosts do not inherit a model author's status page. No
verified public status destination was found for Z.AI or its Coding Plan.
Ollama and vLLM are local providers in Jarvis’s catalog; their operators must
check their own servers. Custom OpenAI-compatible endpoints likewise need
the hosting operator’s monitoring. The current
model picker is deliberately not used: it can change after the failure, and
failover may have used another provider.

## Boundaries and validation

The relay currently forwards error prose, not a complete structured provider
error envelope. A message row persists the error text (the pump truncates it
to 1,000 characters); the realtime classification is not persisted on the
message. Shared rules remove classifier drift on reload, but missing or
truncated evidence cannot be reconstructed. Unknown errors and unidentified
providers therefore have explicit fallbacks. New provider formats should add
fixtures before extending the ordered rules.

This taxonomy covers failed **chat turns**. API admission refusals, uploads,
connection setup forms, and action approval cards retain their existing
operation-specific error handling (`errMessage`, `ActionError`, etc.). This
taxonomy does not change authorization, billing, or error-reporting policy.

Both languages run `jarvis/tests/fixtures/turn_errors.json`. Frontend regression
tests also cover provider routing, legacy-code refinement, retry guidance,
unknown inputs, and safe status URLs. Run:

```sh
node --test frontend/src/lib/errors.test.js
python3 -m unittest jarvis.tests.test_error_taxonomy
```

## Error capture lanes

Every way a Jarvis failure reaches a user is buffered on the bench and pushed to
the admin Errors feed by `jarvis.error_push.push_error_rollup` (`*/5` cron).

| Lane | Source | Surface | Severity | Cursor |
| --- | --- | --- | --- | --- |
| Browser (SPA, PWA, Desk) | `Jarvis Client Error` | the reporter's surface | error | `pushed` 0 to 1 |
| Failed jarvis API call (4xx) | `Jarvis Client Error`, by the `after_request` hook | `api` | warning | `pushed` |
| Failed tool (`{ok: false}`) | `Jarvis Client Error` | `tool` | warning | `pushed` |
| Rejected `call_tool` | `Jarvis Client Error` | `call_tool` | error | `pushed` |
| Code-level exception | `Error Log`, jarvis origin only | `server` | error | `jarvis_error_log_watermark` |
| Failed turn | `Jarvis Chat Turn` in state `errored` | `turn` | error | `jarvis_failed_turn_watermark` |

Rules worth knowing:

- Cursors are `<timestamp>|<name>` and only move forward. Each scan stops 120 s
  behind now. A transaction that commits more than 120 s after its row was
  created is skipped.
- A rejected `call_tool` before the caller is known is recorded under the route
  `unauthenticated`; a tool or endpoint name that does not exist is recorded as
  `unknown_tool` / `unknown_method`, so a hostile caller cannot choose how many
  rows are written. HTTP 401 and 429 are not errors.
- A `frappe.log_error(title, message)` row has no traceback. Only its title and
  the jarvis function that called `log_error` are forwarded, never the message
  body. A row written by `error_push`, `api_errors` or `chat/heartbeat`, or whose
  traceback passes through `admin_client.py` or `chat/usage_push.py`, is never
  forwarded.
- A browser `RunError` is dropped only when its `run_id` names an `errored` turn
  (the turn lane sends that one). Any other `RunError` is kept.
- A batch is claimed and committed before the admin call, reverted if it fails,
  and dropped when admin refuses the payload (`InvalidErrorRollup`) or answers it
  with a failure 12 cycles in a row (an hour). An outage, 401/403/429 or a stopped tenant
  is waited out, never dropped.
- A tool that calls `frappe.log_error` and then returns `{ok: false}` shows as
  two rows (a `tool` warning and an exception). That is by design.
- Not captured: generic Frappe endpoints (`savedocs`, `/api/resource`) on jarvis
  doctypes, and HTTP 200 `{ok: false}` replies outside `call_tool`.
- After the deploy every forwarded Error Log fingerprint changes once (frames now
  read `jarvis/...`), so open admin rows will not fold with new ones. This is a
  one-time re-key.
- `done_at` on `Jarvis Chat Turn` now has a search index, so the failed-turn scan
  is an index range read. It needs `bench migrate` (v15 and v16).
- The site_config flag `jarvis_error_capture_off: 1` turns every lane and the
  push off.

### Diagnosing a stalled pipeline

In `bench --site <site> console`:

```python
d = frappe.db.get_default
for k in ("last_ok_at", "last_attempt_at", "consecutive_failures", "last_failure_cause", "inflight"):
    print(k, d("jarvis_error_push_" + k))
print(d("jarvis_error_log_watermark"), d("jarvis_failed_turn_watermark"))
frappe.db.count("Jarvis Client Error", {"pushed": 0})  # waiting in the buffer
```

Once the last success is over an hour old and a push fails, an Error Log row
`jarvis errors: rollup push stalled <N>h (<cause>)` is written at most every 6
hours. A dropped batch writes `jarvis errors: admin refused rollup batch` and a
`jarvis.error_push` log line; a prune of unsent rows and rows a scan could not
read are logged the same way. Capture-hook failures go to the `jarvis.error_capture`
log at warning level.
