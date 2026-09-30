"""Present evaluator-authored findings without inventing missing audit evidence."""


def finding_text(finding: dict) -> tuple[str, str]:
	def text(field):
		value = finding.get(field)
		return value.strip() if isinstance(value, str) else ""

	detail = text("note") or text("detail") or text("detail_md")
	title = text("title")
	if not title and detail:
		title = detail.splitlines()[0]
	if not title:
		reference = " ".join(filter(None, (text("ref_doctype"), text("ref_name"))))
		title = f"Review {reference}" if reference else "Finding needs review"
	return " ".join(title.split())[:140], detail
