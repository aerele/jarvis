"""Bench CLI commands for the jarvis app (auto-discovered via ``commands``)."""

import click
import frappe
from frappe.commands import get_site, pass_context


@click.command("reset-onboarding")
@click.option("--force", is_flag=True, help="Skip the confirmation prompt.")
@click.option(
	"--keep-data",
	is_flag=True,
	help="Keep workspace content (chats, skills, macros, ...); only clear the connection + LLM setup.",
)
@pass_context
def reset_onboarding(context, force, keep_data):
	"""Completely flush this site's Jarvis setup so the onboarding wizard can
	run fresh (DEV): connection + LLM credentials, and — unless --keep-data —
	all workspace content (chats, skills, macros, triggers, learning, wiki,
	dashboards). Admin-side records are NOT touched - use the admin's Purge
	customer for that."""
	site = get_site(context)
	frappe.init(site)
	frappe.connect()
	try:
		if not force:
			what = (
				"the local connection + LLM credentials (workspace content is kept)"
				if keep_data
				else "the local connection + LLM credentials AND ALL workspace content "
				"(chats, skills, macros, triggers, learning, wiki, dashboards)"
			)
			click.confirm(
				f"Reset Jarvis onboarding on {site}? This clears {what}. Admin-side records are not touched.",
				abort=True,
			)
		from jarvis.dev import reset_onboarding as _reset

		data = _reset(wipe_data=not keep_data).get("data", {})
		cleared = data.get("cleared_fields", [])
		wiped = data.get("wiped_doctypes", [])
		msg = f"Onboarding reset on {site} - cleared {len(cleared)} field(s)"
		if wiped:
			msg += f", wiped {len(wiped)} content doctype(s)"
		click.echo(msg + ".")
	finally:
		frappe.destroy()


commands = [reset_onboarding]
