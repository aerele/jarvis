# Billing-object contract fixture (Plan 01)

`billing_snapshot.json` is the shared billing-object contract for the customer
onboarding flow. It is **byte-for-byte identical** to its twin in the admin repo:

| | |
|---|---|
| twin repo | `git@github.com:Aerele-RnD/jarvis-admin-v2.git` |
| twin path | `jarvis_admin_v2/tests/billing/fixtures/billing_contract/billing_snapshot.json` |
| sha256 | `612aa11a918ea4f789e2b44d51d4b46d35551e3ec06cdebf9a4cb0848b38f6e9` |

Both sides load the same file and assert against it, so the wire shape cannot
drift between the bench (which forwards the object and consumes the normalized
summary + `billing_saved` ack) and admin (which owns the normalizer that stores
and re-reads it). `test_billing_contract.py` recomputes the sha above and fails
on a mismatch.

NOTE (coordination): the broader admin-contract corpus
(`jarvis/tests/fixtures/admin_contract/`, WS-B) is not on `develop` at the time
this landed; this Plan-01 billing fixture is deliberately in its own
`billing_contract/` subdir so it neither depends on nor collides with that corpus
when WS-B merges. The paired-head CI job that diffs the two repos' copies is
deferred with the rest of combined-head CI (FABLE-JUDGMENT §2.6); the sha assertion
here is the standing in-repo guard until then.
