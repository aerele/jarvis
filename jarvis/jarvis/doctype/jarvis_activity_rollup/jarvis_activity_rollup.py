from frappe.model.document import Document


class JarvisActivityRollup(Document):
	"""Single holding ``rolled_through``, the rollup watermark. Written only by
	``jarvis.activity_rollup`` through ``frappe.db.set_single_value``."""
