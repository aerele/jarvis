

## Merged source follow-up (2026-11-02)

This v15 adapter carries the merged #2258 generic RPC guards, document hooks, linked-journal and submitted debit-note checks, safe classifier failures, and PI-before-LCV workflow lock order. It retains the v15 source-link fields, prior-claim totals, account and currency checks, and per-row link-removal guards. Native vendor-invoice helpers apply only to v16 and are not used here. The net-cost ceiling was already present in the v15 adapter.

The generic mutation and classifier-gap regression tests run without India Compliance. Tests of recorded India Compliance treatment still require that app and native v15 validation before release.
