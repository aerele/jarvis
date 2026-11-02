import frappe
from frappe.model.document import Document

from jarvis.payment_calendar.permissions import require_company, require_floor_write
from jarvis.payment_calendar.settings import validate_floor
from jarvis.permissions import NotRenamable


class JarvisPaymentCalendarSettings(NotRenamable, Document):
	def validate(self):
		require_floor_write(self.company)
		previous = self.get_doc_before_save()
		if previous and previous.company != self.company:
			raise frappe.ValidationError("The settings company cannot be changed.")
		self.currency = require_company(self.company).default_currency
		self.safety_floor = str(validate_floor(self.safety_floor))
		self.revision = int(previous.revision or 0) + 1 if previous else 1

	def on_trash(self):
		require_floor_write(self.company)
