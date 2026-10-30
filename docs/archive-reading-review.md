# Archive reading review evidence

The intended user is an accountant. They upload six months of bank or tax data, ask for exact counts or totals, inspect an Accounts Receivable prepared report, and use the findings to request later Customer changes. An incomplete total can cause a wrong decision. This change must preserve source scope and current access before any answer or update.

## Code review

Codex implemented the change. Claude reviewed the actual code with read-only source tools. The first review found worksheet coverage and shared attachment identity defects. A later review found an old-client retry defect. All concrete findings were fixed and rechecked. The final scoped review found no remaining concrete defect in its scope. The first review used one unintended line-count command; later reviews explicitly denied command and edit tools. No reviewer edits or business writes occurred.

| Area | Evidence and result |
| --- | --- |
| Correctness | Real SQLite queries reach the last of 6,000/7,200 data rows. Source coverage is separate from display limits. Worksheet subsets cannot claim full archive coverage. |
| Security and data | Exact File IDs, current File access on each call, report owner/filter/company checks, terminal native report handoff failures, SQL authorizer, escaped source data, and revoked access tests. No extracted File or business document is created by reading. |
| Performance | Shared byte, XML, row, cell, table, CPU, memory, and wall limits. A 60 MiB benign internal-XML fixture passes in plain/ZIP/GZ workers. Its observed child-process peak RSS is about 87 MiB; this is not a capacity promise. |
| Edge cases | Real ZIP64 and signed/unsigned descriptors, renamed workbook part, corrupt CRC/length/EOF, false small ZIP sizes with matching prefix CRC, unsafe paths, links, nested formats, changed source versions, duplicate File URLs, and global budgets. |
| API and maintenance | Existing file tools and SQL reader are reused. Explicit inventory/query/cursor contracts prevent a preview from replacing scoped reads. Bundle capabilities have paired pinned digests. |
| Testing | Backend pure/worker checks, dedicated test-site API/database checks, plugin registration/contract/coverage checks, Admin unit and database transaction checks, frontend components and builds. |
| Recovery | Failed members return no partial aggregate. Child deadlines kill and reap the process. Old and mixed attachment request shapes preserve shared-send idempotence. Source/contract failures retain their scope. |
| Operation | Archive key defaults off. Deployment and rollback use exact source snapshots and hashes. Native report handoff remains independent of the archive key. Existing AgentRun capability snapshots remain unchanged. |

## Accountant journey

| Step | Checked behavior | Remaining limit |
| --- | --- | --- |
| Upload | Ordinary and shared upload responses keep exact File IDs. A deduplicated URL alone is refused. Shared canvas and dispatch retain the chosen ID. | Full browser upload through the model gateway is not yet verified. |
| Find data | Inventory lists supported and unsupported members without claiming a read. Query inspection returns tables and provenance. | The user must split inputs above supported limits. |
| Count and total | One query can combine selected members after complete loading. Decimal functions preserve exact money values. | The model must identify invoice rows, currencies, headers, and totals correctly. |
| Read a prepared report | Completed runs return metadata. Native artifacts hand off to the exact report reader. A missing/corrupt read does not regenerate the report. | Native Frappe 15 execution remains unverified. |
| Follow progress | The report card remains visible after metadata-only readiness. A real completed query closes that exact run. | The deployed local base lacks an older report failure-reason enhancement; its same diagnostic test fails before and after this change. |
| Request updates | Existing preview, approval, and action limits remain in place. | No Customer update was performed in this work. |
| Recover | Damaged, stale, unsupported, or denied sources return an explicit refusal. Existing shared requests can replay without a duplicate seed. | There is no shell or parser fallback after a refusal. |

## Visual and interaction check

The actual `ReportScope` and `ReportRunCards` components were rendered in a fixture page at 1366×900 and 390×844. The page used representative long company names, multiple currencies, ready/preparing/failed cards, and a hostile source label. Screenshots were inspected. Names and dates stay associated. The ready action remains visible and has a clear focus state. Narrow layouts wrap without horizontal page overflow. The hostile label stays text. Keyboard Tab and Enter activate the ready card's scoped event.

This was a component-page check. The fixture used the real Frappe date helper exports but did not load the complete chat screen, authenticate a browser, submit an ERP update, or send a prompt through a model gateway. The component checks do not establish full chat readiness.

## Release limits

Keep ZIP/GZ reading off and the PRs in draft until native Frappe 15, representative Microsoft Excel/LibreOffice/Google exports, and full model-driven ordinary/shared chat journeys pass. Local Frappe 16 API and worker evidence is available. These gaps do not block review of the implementation, but they block a production-ready claim and archive activation under this plan.
