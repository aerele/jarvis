"""Frozen replies of one day and one bucket: a literal that no shared code can drift with.

`test_payment_calendar_levers` compares every reply with a copy of the rev-4 path that shares party,
total, paging and render code with `detail`. These two literals pin those parts. They were computed with
the BASE commit's (aec82dcc6) own reply code, `api._detail` and the day and bucket filters of `get_day_detail`
and `get_bucket_detail`, run on this golden dataset (the source rows of `payment_calendar_golden`). Only the
intended differences were applied by hand: `source_link` removed, `settled` and `total_remaining` added
(computed with plain arithmetic) and the new default order (remaining descending, undated last, ties by id).
They are edited by hand only when a reply is meant to change.

Day 2026-11-13 of Payable: three invoices of 13,485, 13,485 and 13,487; the base order was the source order, so the
largest invoice moving to the front shows the new order, and the 13,485 tie shows the id rule. The bucket is
`overdue` of Payable, page 2 of 2-row pages with party page 2.
"""

from unittest import TestCase

from jarvis.payment_calendar import detail
from jarvis.tests import payment_calendar_golden as golden
from jarvis.tests.payment_calendar_fixtures import require_calendar_test_site

FROZEN = {
	"bucket": {
		"generated_at": "2026-11-11T05:12:00+00:00",
		"has_more": True,
		"page": 2,
		"page_size": 2,
		"parties": [
			{
				"invoices": 1,
				"party": "Sierra Textiles",
				"party_name": "Sierra Textiles",
				"party_type": "Supplier",
				"remaining": "4000.00",
				"terms": 1,
			},
			{
				"invoices": 1,
				"party": "Bluebird Logistics",
				"party_name": "Bluebird Logistics",
				"party_type": "Supplier",
				"remaining": "3000.00",
				"terms": 1,
			},
		],
		"party_page": 2,
		"party_total": 6,
		"rows": [
			{
				"account": "Creditors - NM",
				"credit": "0.00",
				"discount": "0.00",
				"due_date": "2026-11-07",
				"id": "payable-2026-11-07-0",
				"kind": "obligation",
				"original": "4000.00",
				"paid": "0.00",
				"party": "Sierra Textiles",
				"party_name": "Sierra Textiles",
				"party_type": "Supplier",
				"posting_date": "2026-10-15",
				"remaining": "4000.00",
				"settled": "0.00",
				"settlement": "Unpaid",
				"term": "",
				"voucher_no": "PINV-2026-1070",
				"voucher_type": "Purchase Invoice",
			},
			{
				"account": "Creditors - NM",
				"credit": "0.00",
				"discount": "0.00",
				"due_date": "2026-11-05",
				"id": "payable-2026-11-05-0",
				"kind": "obligation",
				"original": "3000.00",
				"paid": "0.00",
				"party": "Bluebird Logistics",
				"party_name": "Bluebird Logistics",
				"party_type": "Supplier",
				"posting_date": "2026-10-15",
				"remaining": "3000.00",
				"settled": "0.00",
				"settlement": "Unpaid",
				"term": "",
				"voucher_no": "PINV-2026-1050",
				"voucher_type": "Purchase Invoice",
			},
		],
		"total": 6,
		"total_remaining": "22500.00",
	},
	"day": {
		"generated_at": "2026-11-11T05:12:00+00:00",
		"has_more": True,
		"page": 1,
		"page_size": 2,
		"parties": [
			{
				"invoices": 1,
				"party": "Sierra Textiles",
				"party_name": "Sierra Textiles",
				"party_type": "Supplier",
				"remaining": "13487.00",
				"terms": 1,
			},
			{
				"invoices": 1,
				"party": "Apex Electricals",
				"party_name": "Apex Electricals",
				"party_type": "Supplier",
				"remaining": "13485.00",
				"terms": 1,
			},
		],
		"party_page": 1,
		"party_total": 3,
		"rows": [
			{
				"account": "Creditors - NM",
				"credit": "0.00",
				"discount": "0.00",
				"due_date": "2026-11-13",
				"id": "payable-2026-11-13-2",
				"kind": "obligation",
				"original": "13487.00",
				"paid": "0.00",
				"party": "Sierra Textiles",
				"party_name": "Sierra Textiles",
				"party_type": "Supplier",
				"posting_date": "2026-10-15",
				"remaining": "13487.00",
				"settled": "0.00",
				"settlement": "Unpaid",
				"term": "",
				"voucher_no": "PINV-2026-1132",
				"voucher_type": "Purchase Invoice",
			},
			{
				"account": "Creditors - NM",
				"credit": "0.00",
				"discount": "0.00",
				"due_date": "2026-11-13",
				"id": "payable-2026-11-13-0",
				"kind": "obligation",
				"original": "13485.00",
				"paid": "0.00",
				"party": "Bluebird Logistics",
				"party_name": "Bluebird Logistics",
				"party_type": "Supplier",
				"posting_date": "2026-10-15",
				"remaining": "13485.00",
				"settled": "0.00",
				"settlement": "Unpaid",
				"term": "",
				"voucher_no": "PINV-2026-1130",
				"voucher_type": "Purchase Invoice",
			},
		],
		"total": 3,
		"total_remaining": "40457.00",
	},
}


class TestFrozenReplies(TestCase):
	def setUp(self):
		require_calendar_test_site()
		self.scope, self.result = golden.build_result()
		self.status = {"scope": self.scope}

	def test_the_day_reply_is_the_frozen_literal(self):
		reply = detail.day_detail(
			self.status,
			self.result,
			date="2026-11-13",
			side="Payable",
			page=1,
			page_size=2,
			party_page=1,
			sort=None,
			order=None,
		)
		self.assertEqual(reply, FROZEN["day"])

	def test_the_bucket_reply_is_the_frozen_literal(self):
		reply = detail.bucket_detail(
			self.status,
			self.result,
			bucket="overdue",
			side="Payable",
			page=2,
			page_size=2,
			party_page=2,
			sort=None,
			order=None,
		)
		self.assertEqual(reply, FROZEN["bucket"])
