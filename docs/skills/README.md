# Optional customer-authored procedures

`landed-cost-voucher.md` is reusable instruction text for a Jarvis Custom Skill,
not a dataset-generation hook or an additional accounting validator.

Save it with the existing `create_custom_skill` flow, using skill name
`landed-cost-voucher`, description "Prepare landed costs from shipment documents",
the Markdown as `instructions`, and User scope. The normal confirmation applies.
Find/read it through `find_skills` / `get_skill`. Promotion or publishing uses
Jarvis's existing reviewer workflow. Importing an app or populating a playground
does not create, promote or push the skill. There are no new tools in this change.

New LCVs use ERPNext's native vendor-invoice amount helper for omitted amounts in
the shared create/preview path. Explicit amounts are preserved. This default does
not change updates to existing vouchers or ERPNext's claim/submission rules.
New vendor rows added to an existing draft still need explicit amounts; this
change does not fetch those amounts during updates.

The native amount helper does not reserve costs. ERPNext 16.50.0 overwrites or
resets a bill's claimed amount on submit/cancel rather than accumulating multiple
voucher claims. This adapter preserves that behavior; it does not make partial
claims across multiple vouchers safe. Check the installed ERP's behavior before
submitting another voucher against a partly claimed invoice.
