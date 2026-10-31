# Source-read retention

`query_file`, `query_report`, and `read_file` return source data to the authorized
live tool call. New bench tool receipts retain bounded source references and
coverage metadata. They do not retain rows, samples, text, SQL, or source-bearing
error messages. Activity events use the same minimized receipt. Other tools and
intentional assistant replies keep their existing behavior.

The paired plugin registers synchronous `tool_result_persist` and
`before_message_write` hooks. These minimize gateway-owned tool-result records
and source tool-call arguments. The live result is not mutated. A later turn must
query the original source again under current permissions and pin the returned
version. A shared retry of a minimized receipt returns `SourceReadRequired`; it
cannot replay a stored reference as a complete query result or execute the same
call again.

These changes apply to new writes. They do not delete earlier transcripts,
backups, original uploads, report artifacts, or user-requested exported files.
Gateway hooks do not establish retention guarantees for native Claude CLI or
other provider-owned transcripts. Those paths need separate lifecycle checks.
An older runtime without the persistence hook API logs an explicit compatibility
warning. The paired plugin refuses source reads before network dispatch until
the runtime is upgraded. Other tools retain their existing behavior.

Compaction must be checked against the deployed runtime's persisted-history path.
Unit checks of the redactor alone do not prove that a native runner or an existing
historical summary contains no source values. Do not claim universal zero
retention from this hook.

The file and report child processes disable core dumps before reading input.
Their 512 MiB address-space limit is enforced on Linux. macOS does not have this
address-space guarantee; byte, row, SQL-work, and parent wall limits still apply.
