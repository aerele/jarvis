"""Jarvis Agent Run Step DocType controller.

One append-only row per observable step of a ``Jarvis Agent Run``: the launch
dispatch, every bench tool call the delegate made through the plugin endpoint,
and the findings writeback. Together they are the run's live timeline - what the
agent is doing right now, and what it did when the run is over.

Like ``Jarvis Agent Activity``, every reference field is a Data SNAPSHOT and
never a Link, so the uninstall cascade (which hard-deletes the installation, its
runs and its findings) can never trip LinkExistsError here. Rows are
server-generated, so ``Jarvis User`` gets ``if_owner`` READ only - the customer
sees their own runs' steps and can neither forge nor edit one.

Steps carry SHAPES, not data: DocType names, report names, counts. A step never
records row contents, so the timeline is safe to render to anyone who may read
the run.
"""

from frappe.model.document import Document


class JarvisAgentRunStep(Document):
	pass
