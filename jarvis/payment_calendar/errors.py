import frappe


class CalendarSourceUnavailable(frappe.ValidationError):
	def __init__(self, code="source_contract_unavailable"):
		self.code = code
		super().__init__("The accounting source cannot provide a complete Payment Calendar snapshot.")
