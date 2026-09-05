# MCP Connectors — implementation plan (feat/mcp-connectors)

Bench-broker architecture. The agent container gets two dumb tools; the bench does the real
MCP call under the resolved user. Token never enters the agent container. Design of record:
memory `jarvis-mcp-connectors-design`. Artifact: the data-flow diagram.

NAMING: this is the PUBLIC jarvis repo. Never write the literal "openclaw" (CI guard
`test_no_openclaw_leak.py`). Say "agent" / "agent container" / "gateway". No em dashes in
user-facing copy. Say "agent", never "AI container", in UI copy.

## Grounded integration points (from code survey)

- Callback entry: `jarvis/api.py` `call_tool()` (whitelisted, HMAC via
  `jarvis/_plugin_auth.py:validate_plugin_request`, keyed on `Jarvis Settings.agent_token`).
- Session -> user: `Jarvis Chat Session.session_key -> user`; dispatched under
  `jarvis/_session.py:impersonate(user)` inside `_dispatch_from_session`. So connector tools
  run as the real user and Frappe row permissions enforce isolation for free.
- Tool registry: `jarvis/tools/registry.py` (`_TOOL_NAMES`, `dispatch()`). Contract file
  `jarvis/tools/tool-names.json` via `jarvis/tools/_tool_contract.py`, byte-copied to the
  plugin repo `contracts/tool-names.json`.
- SSRF guard to PORT: `jarvis/chat/link_fetch.py` (`_is_blocked_ip`, `_validate_host`,
  `_validate_url`, pin-to-vetted-IP + per-redirect revalidation). Fail-closed, no allowlist.
- Encrypted creds: `Password` fieldtype + `doc.get_password("f", raise_exception=False) or ""`.
- Row perms: mirror `jarvis/chat/chat_permissions.py` + register in `hooks.py:611-625`.
- Enqueue: `frappe.enqueue(..., queue="short", timeout=, job_id=, deduplicate=True)`.
- Whitelist guard test: repo-wide sweep `tests/test_whitelist_annotations.py` — just annotate
  EVERY param on new whitelisted functions.
- Separate error log mirror: `Jarvis Client Error` DocType + `jarvis/api_errors.py` upsert.
- Plugin (separate repo, private, "openclaw" allowed there): `src/tool-defs.ts`,
  `src/schemas.ts`, `openclaw.plugin.json`, generic dispatch via `src/frappe-client.ts`.

## Worker-isolation decision (SETTLED with advisor)

The outbound MCP call runs INSIDE the `call_tool` gunicorn handler (not the RQ chat worker —
that is already occupied for the whole turn). The memory's "dedicated worker" line was
aspirational and conflicts with the plugin's synchronous 30s `AbortController`. v1 protection:
- Hard outbound timeout strictly < 30s (connect ~5s, total budget ~20s) so the plugin never
  sees a transport timeout (it classifies those differently than a `{ok:false}` tool error).
- Per-connector circuit breaker in Redis (N fails/window -> open M s -> fast-fail tool error).
- Per-tenant/per-connector concurrency cap in Redis (protects other tenants from a hung call).

## v1 scope (token-only, feature-flagged OFF, one tenant -> soak -> widen)

Presets: GitHub (`api.githubcopilot.com/mcp`, PAT), Jira/Atlassian, Linear, Stripe + Custom URL.
OAuth tier (Slack/Gmail/Notion/...) is v1.1 (Frappe Connected App), NOT in this build.
stdio/local servers OUT. Aggregators (Composio/Zapier) rejected.

## Phases

### P0 — Data model (Sonnet)
- `Jarvis Connector`: key, label, preset, base_url, scope (Shared/Personal), credential(Password),
  enabled, tools_cache(JSON), tools_cached_at, last_test_status, last_test_at.
  Uniqueness: (Personal, owner, key) and (Shared, key). Resolution: personal wins over shared.
- Child `Jarvis Connector Action`: action, allowed, read_only, destructive, description.
- `Jarvis Connector Log` (audit, every call, NOT deduped): connector, user, action, status,
  error_code, message, duration_ms, run_id, args_summary (truncated + redacted), response_bytes.
  Retention via frappe log-clearing hook or daily cleanup task.
- `jarvis/chat/connector_permissions.py` + register in `hooks.py`. Shared: read all tenant users,
  write SM/Admin only. Personal: owner-only. Tests MUST pass on a FRESH DB (local is polluted).

### P1 — Broker + MCP client (ONE Opus agent — deep-reasoning slot)
- FIRST verify (5-min, do not guess): real `initialize` against `api.githubcopilot.com/mcp`
  with a scratch PAT — is the response JSON or SSE-framed? Does the official `mcp` Python SDK
  install cleanly in the bench venv (pydantic/anyio/httpx pins vs frappe/erpnext) AND can the
  SSRF pin-to-IP be enforced through it? If not, hand-roll a minimal JSON-RPC client.
- `jarvis/connectors/mcp_client.py`: `initialize` -> `notifications/initialized` -> `tools/list`
  / `tools/call`; echo `Mcp-Session-Id`; send `MCP-Protocol-Version`; handle BOTH JSON and
  `text/event-stream` POST responses; Bearer auth; hard timeout.
- `jarvis/connectors/broker.py`: resolve row for impersonated user, pick credential, SSRF-check
  (ported link_fetch guard + optional admin egress allow/deny), validate args vs cached
  inputSchema, allowed_actions gate, circuit breaker + concurrency cap, audit log write.
- Validate against modelcontextprotocol.io current spec + vendor docs, not memory.

### P2 — Tools (Sonnet, after broker)
- `jarvis/tools/call_connector.py` (consequential, confirm-first like `run_method`),
  `jarvis/tools/list_connector_actions.py` (read-only, per-user, cached per chat session).
  Register in `registry.py`; regenerate `tool-names.json`.
- Verify `role_profiles.py`: must new tools be added to profile tool-sets or profiled users
  never see `call_connector`? Confirm `tool_deny` in the container template doesn't catch them.
- `_delegate_capability`: DEFAULT-DENY `call_connector` unless run's `tools_allow` names it
  (marketplace agent + user's personal PAT = privilege escalation).
- Kill switch: `Jarvis Settings.connectors_enabled` (Check, default 0) + site_config override.
  `call_connector` fast-fails when off; `list_connector_actions` returns empty; SPA hides pane.

### P3 — SPA API (Sonnet)
- `jarvis/chat/connectors_api.py`: `list_connectors`, `add_connector`, `test_connector`
  (real initialize+tools/list at bench through the SSRF guard + timeout; writes tools_cache;
  nothing saves until it passes), `update_connector`, `delete_connector`, `set_allowed_actions`,
  admin `set_custom_url_policy`. Typed params on ALL.

### P4 — SPA UI (Sonnet, ONLY after Kavin approves UI/UX review)
- `ConnectorsPane.vue` (copy `PersonalisationSettings.vue` two-section list) + `AddConnectorDialog.vue`
  (copy `PromotionRequestDialog.vue`; `FormControl type="select"` preset, NOT Autocomplete-in-Dialog;
  password token field; Test button tri-state). Status via `PromotionStatusChip` Badge+Tooltip.
  Wrap in `SettingsPane`. Register in `SettingsDialog.vue` (PANES+NAV) + `AppShell.vue`
  `SETTINGS_DEEP_LINK_KEYS`. api.js thin `call()` wrappers `jarvis.chat.connectors_api.*`.
  Toasts use `errHtml`. frappe-ui only; never copy `LlmPoolEditor` legacy markup.

### P5 — Plugin (Sonnet, separate repo, AFTER bench flag-off lands)
- Add `call_connector` + `list_connector_actions` to `src/tool-defs.ts` + `src/schemas.ts` +
  `openclaw.plugin.json`; byte-copy `contracts/tool-names.json`; vitest coverage floor.
  `call_connector` copies `run_method` (consequential); `list_connector_actions` copies a read tool.

### P6 — Tests + review
- Backend unit tests: broker, SSRF (blocked IP, DNS-rebind, redirect), per-user isolation on
  FRESH DB, allowed_actions gate, kill-switch, arg validation. Guard-test sweep passes.
- Plugin vitest. Security review via advisor (Fable) as final reviewer.
- Local integration deploy on e2e.localhost for Kavin BEFORE any merge; MCP issues log separately.

## Chat guardrail + docs (Kavin 2026-09-04) — TODOs #1, #2
- GUARDRAIL (do as part of feature, testable): the in-chat agent must NOT help users
  configure the underlying MCP (openclaw) server/client and must NOT explain MCP
  server/client internals. It only describes OUR connectors feature and points users to
  Settings > Connectors. Implement as a jarvis-persona instruction (respect AGENTS.md budget);
  plugin tool descriptions already say "connected apps" (no MCP-server/openclaw wording) — keep
  them that way. Add a chat-behavior assertion to the e2e script.
- DOCS (DEFERRED until after MCP testing): user docs on jarvis-docs.aerele.in — what connectors
  are, how to add/test in Settings, Shared vs Mine, allowed-actions, tokens encrypted + never
  sent to the agent. Our client + settings flow only, not MCP internals.

## UI/UX decisions (SETTLED by Kavin 2026-09-04)
1. Tier display: ONE per-user pane showing BOTH sections — "Shared" (read-only, admin-managed)
   + "Mine" (editable). "Add connector" for shared rows gated to admins only.
2. Admin controls live on the Jarvis Settings DESK form (NOT a new SPA surface): fields
   `connectors_enabled` (Check, default 0), `allow_custom_urls` (Check, default 1), and an egress
   allow/deny list (Small Text / textarea). Disabling any user's custom connector = desk list view
   of Jarvis Connector. Keeps the SPA lean.
3. Removed from artifact mockup for v1: "Gmail / Needs auth" (OAuth = v1.1). v1 states =
   Connected / Failed / Disabled only. Allowed-actions = full tools/list, grouped read/write,
   search + "Allow all read-only", write/destructive off by default. Row actions: Test/Edit/Delete.
4. Chat follow-up: render call_connector as "Connector · action", write actions confirm-first.
