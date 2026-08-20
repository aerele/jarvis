# Formula-injection defence for CSV/spreadsheet exports. A cell whose text
# begins with one of these executes as a formula when the file is opened in
# Excel / LibreOffice Calc / Google Sheets - a phishing / data-exfiltration /
# (via DDE) command-execution vector. The OWASP trigger set:
_TRIGGERS = ("=", "+", "-", "@", "\t", "\r")


def escape_formula(value):
	"""Neutralise a formula-injection cell by prefixing a leading trigger char
	with an apostrophe, so spreadsheets render it as literal text. Checks both
	the raw first char (so tab/CR triggers stay covered) and the
	whitespace-stripped first char (so a payload like " =HYPERLINK(...)" - e.g.
	produced by unwrapping HTML to text - can't smuggle a formula past a leading
	space). Applied to EVERY cell AND header of a CSV/XLSX export. Non-strings
	pass through unchanged (numbers/dates are not an injection vector)."""
	if isinstance(value, str) and (value[:1] in _TRIGGERS or value.lstrip()[:1] in _TRIGGERS):
		return "'" + value
	return value
