"""Jarvis App Learning Run — one "learn from a custom app" execution.

Created by ``jarvis.chat.app_learning_api.schedule_app_learning`` (one row per
app, admin-consented); driven end to end by ``jarvis.learning.app_analysis``:
the */10 cron tick starts due Queued runs (Zipping -> source snapshot zip ->
batch plan), the turn-end chaining hook advances the Analyzing conversation
batch by batch, and the Ingesting worker lands the final consolidation into
wiki pages + org custom skills. Rows are server-written only (no create/write
perms); the SPA reads them through the manage-gated API.
"""

from frappe.model.document import Document

from jarvis.permissions import NotRenamable


class JarvisAppLearningRun(NotRenamable, Document):
	pass
