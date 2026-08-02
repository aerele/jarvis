# Admin contract fixtures

Two kinds of file live here and the difference is load-bearing.

**Vendored** (the table below): byte-for-byte copies of the wire contract the
control plane publishes. They are **not ours** — they are here so this
repository's tests replay the *other* side's published truth instead of a local
re-statement of it.

**Bench-authored** (listed at the bottom): cases this bench must survive that the
admin corpus does not yet contain. They carry a `produced_by` field saying so.
They are not checksummed against admin, they are not evidence of what admin
sends, and each one has an admin-side twin ledgered for the next admin round.
Keeping them in the same directory is deliberate — the reader under test is the
same, and the alternative is a second loader nobody runs.

## Source (vendored files only)

| | |
|---|---|
| repo | `git@github.com:Aerele-RnD/jarvis-admin-v2.git` |
| branch | `feat/onboarding-reconcile` |
| commit | `99740f2` ("fix(billing): review round - lock lifetime, parked rows, and the pay button") |
| path | `jarvis_admin_v2/tests/billing/fixtures/*.json` |
| copied | 2026-08-02 |

`README.md` in that directory is the contract's prose (the two wire shapes, the
capability flags, the codes-not-messages rule). It is deliberately NOT copied:
one authoritative copy, on the side that owns the vocabulary. Read it there.

Checksums of what was copied, so drift is visible rather than argued about:

```
061aabf5b44131ae639fd3cc493697f9843ca6b49fb85cfe12b3f2de43f195f5  account_reconnect_required.json
a647f72beebfae8e43870fdbba2d669b862890e76a150ff3916ca00d6478362c  duplicate_409.json
389e75957ddc715c0760b10324617b87546438f144c369a2a6aa11f7256fae76  legacy_v1_duplicate_409.json
2a3ced8920d84712c45976276ae6a041293e068acaf05aed18b105914a346e82  payment_already_active.json
86d39fd6297d46e2c98d23096e27688e004a88f1df93f5a164340d9feb35aea0  payment_check_rate_limited.json
42ae064fb6407c1ded2d705a785c4e285b9359bde7c27cb764bb2d16cfccfc77  payment_declined_mandate.json
75bead5d397ee8661d17d5cfdf4553bf7c848f372a2912482722e6ed4408c7e2  reconcile_authorized_pending_confirm.json
70549805007c7c22584d73b6ba9ac4bbff50d2e17811a0d24d406897418af7a0  reconcile_declined.json
bca211819996fe51afca89aeda5774a6dd787ecd98be33b14eb82b5597f6b7a3  reconcile_paid_active.json
93bf1ae29ff36e4f0abefbe6472573675cc275a0f864b402ed405d6445dac104  reconcile_pending.json
df5596bc4925ef05b13a861f3676b38b7684deb219417ba6806e4a743a8ac971  reconcile_unmatched_payment.json
41dfbd2bac688c7d25a5189a43c28dbcc27a3b20ea104f7f66c41ac5442732bf  resume_intent.json
c7a7237c2a4d95c0acd70be22de4b70a0ef317f49ab0af77099ed791d42e5e68  signup_terminal.json
fd508b91a430f688a42bec006b0741c2ecf9ab30c05b19c37f57b16931ef9045  state_mandate_pending.json
e6353a18b9b9ab9ad2d0cf344517defb5b162cc893078637810f7a25a5376b24  state_mandate_pending_razorpay.json
43dc1fb4574e760455601ee5428ee7fbc6b20d89ea2a5c5c4d415b0d5822db41  state_no_current_intent.json
bdd79f8c19879c4cfe9b5871e10f9334c83678951e247fb90a9eab1cb73cecc6  verification_required.json
```

`test_admin_contract.py` recomputes that table and fails on a mismatch, so a
vendored file edited in place is caught rather than argued about.

## Bench-authored files (NOT vendored, NOT checksummed)

| file | why it exists | admin-side twin |
|---|---|---|
| `duplicate_409_code_only.json` | Every vendored duplicate fixture carries `exc_type`, so the bench's `error.code` branch was never actually exercised — delete it and the suite stayed green. This is the returned-envelope form (`codes.error_response`, no `exc_type`), which is what the contract says a coded rejection is. | **LEDGERED**: admin adds the same case, or states that the guest duplicate will always be a raise. |
| `state_verification_required.json` | The passive poll's answer while the magic link is unclicked — the state a wizard sits in longest and had no fixture for. `verification_required.json` is the *resume* endpoint's version of the same cohort; the capability flags differ. | **LEDGERED**: admin adds the poll-side case beside its resume-side one. |

## Why a copy exists at all

Because the last time each side kept its own statement of this contract, both
repositories stayed green while every real declined-card customer hit a dead
end. Admin reworded its duplicate-signup message on 2026-07-26; the bench's
failed-payment resume string-matched the old sentence; the only surviving copy
of that sentence in this tree was **this suite's own fixture**, so nothing here
could notice. `test_admin_contract.py` replays these files through the real
`admin_client` parser, which is what makes a rewording harmless and a contract
change loud.

## Editing rule

**Never edit a file in this directory to make a test pass.** They describe what
the control plane sends. If a fixture and this bench disagree, either the bench
is wrong or the contract genuinely changed — and a contract change is a change
*over there*, landed first, then re-copied here.

## Re-syncing

Manual today, deliberately: the combined-head CI that would make it automatic is
blocked behind the llm-proxy PAT pin in admin's `pyproject` (admin #119/#122 +
fleet #161). Until that lands:

```bash
cp <admin-checkout>/jarvis_admin_v2/tests/billing/fixtures/*.json \
   jarvis/tests/fixtures/admin_contract/
sha256sum jarvis/tests/fixtures/admin_contract/*.json   # update the table above
```

and run `jarvis.tests.test_admin_contract`. A fixture whose `contract_version`
is higher than `jarvis.onboarding_contract.CONTRACT_VERSION` means a BREAKING
shape change shipped and the facade has to be read again, not re-pinned; the
suite asserts that and fails on it.
