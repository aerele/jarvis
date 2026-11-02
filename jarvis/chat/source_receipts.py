"""Retain source references, never file/report cells or SQL, in tool receipts."""

SOURCE_TOOLS = frozenset({"query_file", "query_report", "read_file"})
REFERENCE_FIELDS = frozenset(
	{
		"file_id",
		"file_url",
		"filename",
		"prepared_report",
		"version",
		"archive_version",
		"read_contract",
		"read_mode",
		"member",
		"payload_format",
		"sheet",
		"cursor",
	}
)
COUNT_FIELDS = frozenset({"source_rows", "data_rows", "total_rows"})


def _references(value):
	if not isinstance(value, dict):
		return {}
	result = {
		key: item
		for key, item in value.items()
		if key in REFERENCE_FIELDS and isinstance(item, str) and len(item) <= 2048
	}
	for key in ("sheets", "members", "selected_members", "worksheet_scope"):
		items = value.get(key)
		if (
			isinstance(items, list)
			and len(items) <= 32
			and all(isinstance(item, str) and len(item) <= 256 for item in items)
		):
			result[key] = list(items)
	return result


def source_receipt(tool, args, result):
	"""Return detached storage values; the live API result remains unchanged."""
	if tool.removeprefix("jarvis__") not in SOURCE_TOOLS:
		return args, result
	data = result.get("data") if isinstance(result, dict) else None
	stub = {
		"retention": "source-reference-v1",
		"requery_required": True,
		**_references(data),
		"note": "Source values and SQL are not retained. Query the source again with current permissions.",
	}
	if isinstance(data, dict):
		for key in COUNT_FIELDS:
			if type(data.get(key)) is int and data[key] >= 0:
				stub[key] = data[key]
		coverage = data.get("coverage")
		if isinstance(coverage, dict) and type(coverage.get("complete")) is bool:
			stub["coverage"] = {"complete": coverage["complete"]}
		if type(data.get("result_truncated")) is bool:
			stub["result_truncated"] = data["result_truncated"]
	if isinstance(result, dict) and result.get("ok") is True:
		stored = {"ok": True, "data": stub}
	else:
		stored = {
			"ok": False,
			"error": {"code": "SourceReadFailed", "message": "Source read failed. Details are not retained."},
			"data": stub,
		}
	stored_args = _references(args)
	if tool.removeprefix("jarvis__") in {"query_file", "query_report"}:
		stored_args["source_query_executed"] = bool(isinstance(args, dict) and args.get("sql"))
	return stored_args, stored


def is_source_receipt(result):
	return (
		isinstance(result, dict)
		and isinstance(result.get("data"), dict)
		and (result["data"].get("retention") == "source-reference-v1")
	)
