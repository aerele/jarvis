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
This change does not automatically resend messages or alter recovery behavior.

Retry (desktop chat and dashboard chat) runs the failed turn again with the
context and the original attachments of that turn. The server refuses a retry
when a later turn has not finished, when the failed reply is not the latest
user or assistant message (the closing message of a macro run does not count),
or when the user message has a different owner than the conversation. On the
admission and pump paths these checks run under the conversation row lock, so
two tabs cannot start two retries; the legacy path checks without the lock.

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
operation-specific error handling (`errMessage`, `ActionError`, etc.). It does
not change authorization, billing, or error-reporting policy.

Both languages run `jarvis/tests/fixtures/turn_errors.json`. Frontend regression
tests also cover provider routing, legacy-code refinement, retry guidance,
unknown inputs, and safe status URLs. Run:

```sh
node --test frontend/src/lib/errors.test.js
python3 -m unittest jarvis.tests.test_error_taxonomy
```
