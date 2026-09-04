# Billing-object contract fixture (Plan 01)

`billing_snapshot.json` is the shared billing-object contract for the customer
onboarding flow.

## Source of truth

The **admin** copy is authoritative — admin owns the normalizer
(`billing.signup._normalize_billing` / `_billing_summary`) that stores and
re-reads this object. This file is a **byte-for-byte identical** vendored twin of
it, so the two repos cannot drift silently:

| | |
|---|---|
| twin repo | `git@github.com:Aerele-RnD/jarvis-admin-v2.git` |
| twin branch | `feat/plan01-billing-persistence` |
| twin path | `jarvis_admin_v2/tests/billing/fixtures/billing_contract/billing_snapshot.json` |
| sha256 | `95b5460b850e45bc1c8c3de198f7ddd74d747daa541a0d3b6f16984d46c2ddc1` |

The sha pin is what enforces byte-identity: `test_billing_contract.py`
recomputes the sha of this file and fails on a mismatch, and the admin twin pins
the identical value. If either side edits its copy, both suites go red — a
divergence surfaces as a test failure instead of shipping. (The `gstin` in the
fixture, `33ABCDE1234F1Z7`, is a fully valid GSTIN — real state code 33 and a
correct checksum digit — so it survives the admin normalizer's validation; the
earlier `...Z5` value was checksum-invalid.)

## What is contractual vs volatile

Stable **codes** and **field names** are the contract — `request_billing`'s field
set, `normalized_summary`'s keys, and the `billing_saved` ack shape are what both
sides pin. Human-facing **display messages** are volatile and are NOT part of the
pinned shape; never string-match against them.

The bench forwards `request_billing` to admin UNMODIFIED (it never reshapes or
flattens it) and consumes the normalized summary + `billing_saved` ack.
`test_billing_contract.py` replays the fixture through the real bench consumer
(`jarvis.admin_client.signup`, with `_post_guest` patched) and asserts the
forwarded object is byte-for-byte the fixture, so an admin-side shape change
fails here rather than being discovered in production.

## Re-syncing

Manual today: the paired-head CI job that would diff the two repos' copies
automatically is deferred with the rest of combined-head CI
(FABLE-JUDGMENT §2.6); the sha pin above is the standing in-repo guard until
then. A contract change lands on the admin side first, then is re-copied here —
never edit this file to make a test pass.

```bash
cp <admin-checkout>/jarvis_admin_v2/tests/billing/fixtures/billing_contract/billing_snapshot.json \
   jarvis/tests/fixtures/billing_contract/billing_snapshot.json
sha256sum jarvis/tests/fixtures/billing_contract/billing_snapshot.json   # update _FIXTURE_SHA in test_billing_contract.py
```

and run `jarvis.tests.test_billing_contract`.

## Coordination

The broader admin-contract corpus (`jarvis/tests/fixtures/admin_contract/`, WS-B)
is not on `develop` at the time this landed; this Plan-01 billing fixture is
deliberately in its own `billing_contract/` subdir so it neither depends on nor
collides with that corpus when WS-B merges.
