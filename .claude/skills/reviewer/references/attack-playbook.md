# Attack playbook — how to try to break the change

Apply every category to the diff. For each attack that plausibly applies, either locate the defense (cite file:line in the review) or record a finding. "The caller would never do that" is not a defense.

## 1. Input attacks
- Empty, null/undefined/None, whitespace-only values in every new parameter and field.
- Oversized input: very long strings, huge arrays, large files, deeply nested JSON. What is the limit, and what happens at limit+1?
- Wrong types: string where number expected, object where array expected, negative and zero where positive assumed.
- Hostile strings: SQL fragments (`' OR 1=1 --`), script tags, path traversal (`../../etc/passwd`), format specifiers, emoji/unicode (multi-byte, RTL, zero-width), leading/trailing whitespace.
- Boundary numbers: 0, -1, MAX_INT, MAX_INT+1, floats where ints expected, NaN, Infinity.
- Malformed encodings: invalid UTF-8, truncated JSON, wrong content-type.

## 2. State and timing attacks
- Double-submit: the same action fired twice fast (double-click, retry). Duplicate records? Double charge?
- Replay: re-sending an old request or token.
- Out-of-order: calling step 3 before step 1. Direct URL/endpoint access skipping the flow.
- Concurrent writes: two sessions modifying the same record. Last-write-wins data loss? Race conditions on check-then-act?
- Stale state: acting on data loaded before another change (edit form open for an hour, then submitted).
- Interrupted flow: process/tab killed midway. Is the system left half-written?

## 3. Failure injection
- Each external dependency (DB, API, queue, filesystem) down, slow (timeout), or returning garbage. Does the change fail loudly, retry sanely, or hang/corrupt?
- Partial failure: step 2 of 3 succeeds, step 3 fails. Is there a transaction or compensation, or is data now inconsistent?
- Timeout values: do they exist at all? What happens at exactly the timeout?
- Retries: are they bounded? Are they safe (idempotent) to retry?
- Disk full, permission denied, missing env var/config at startup.

## 4. Auth and access attacks
- Every new endpoint/action hit unauthenticated.
- Hit with another user's resource IDs (IDOR): change the ID in the request — can I read or mutate someone else's data?
- Expired/invalid session or token mid-flow.
- Lower-privilege role invoking higher-privilege action directly.
- Sensitive data in responses, logs, error messages, or URLs.

## 5. Resource attacks
- Unbounded loops or recursion reachable from input.
- Unclosed handles: files, connections, subscriptions, event listeners — trace every open to its close on ALL paths including error paths.
- N+1 queries or per-item network calls inside loops on hot paths.
- Unbounded growth: caches, queues, lists that only ever append.
- Pagination: page 0, negative page, page beyond end, limit=0, limit=100000.

## 6. Data integrity attacks
- Migration: does it run on real-shaped data (nulls, duplicates, legacy rows)? Is it reversible?
- Uniqueness and foreign keys enforced at the database, not just in application code?
- Time: timezone handling, DST transitions, clock skew, dates stored as local vs UTC.
- Money/precision: floats used where decimals required; rounding rules explicit?

## 7. Frontend flow attacks (apply during flow review too)
- Back button and refresh at every step of a multi-step flow.
- Double-click every submit button.
- Slow network (throttle): do loading states exist, can the user fire actions twice while waiting?
- Deep-link directly into mid-flow pages.
- Small viewport, keyboard-only navigation, browser autofill garbage into fields.
- Error responses from the backend: does the UI show something actionable or swallow it?
