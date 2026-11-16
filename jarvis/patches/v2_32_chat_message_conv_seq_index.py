"""Composite index (conversation, seq) on Jarvis Chat Message for the live-draft read
(``jarvis.chat.live_draft.load_rows``): a chat's newest rows in seq order, from a backward
range scan of that one chat instead of every row of it plus a filesort.

The index is declared in the DocType's ``on_doctype_update``, which a fresh install runs
when it creates the table. An upgraded site's DocType JSON did not change, so migrate never
re-saves it and the hook never fires there: this patch adds the index on those sites.
``add_index`` is idempotent (a no-op when the index exists), so a re-run is safe.
ADD INDEX runs in place but waits on a metadata lock, so a long open transaction on a large
``Jarvis Chat Message`` table can stall chat queries during migrate.
"""

import frappe

from jarvis.jarvis.doctype.jarvis_chat_message.jarvis_chat_message import on_doctype_update


def execute():
	if not frappe.db.table_exists("Jarvis Chat Message"):
		return
	on_doctype_update()
