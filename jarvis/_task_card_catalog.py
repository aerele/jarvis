"""Curated "What can I ask Jarvis?" task cards (static, like _preset_catalog.py).

Each card names exactly ONE tool; its SAFETY BADGE is derived at serve time from
``jarvis.api._gating_badge`` and never stored here. Tests forbid multi-tool
cards, cards that resolve to the audited-but-ungated ``writes_directly`` tier,
and wiki cards (whose availability depends on a runtime kill switch). Extend
within the same three groups following the identical shape - every new card is
gated by ``jarvis.tests.test_capability_catalog``.
"""

TASK_CARDS: list[dict] = [
	# Look things up -> reads_only
	{
		"group": "Look things up",
		"title": "Check what a customer owes",
		"prompt": "How much does this customer owe us right now?",
		"tools": ["get_customer_outstanding"],
	},
	{
		"group": "Look things up",
		"title": "See a customer's account at a glance",
		"prompt": "Show the account summary for this customer",
		"tools": ["get_party_dashboard_info"],
	},
	{
		"group": "Look things up",
		"title": "Pull a report",
		"prompt": "Show me this month's profit and loss",
		"tools": ["run_report"],
	},
	{
		"group": "Look things up",
		"title": "Find a record",
		"prompt": "Find the primary contact for this customer",
		"tools": ["get_list"],
	},
	{
		"group": "Look things up",
		"title": "Check stock on hand",
		"prompt": "What's the current stock balance for this item?",
		"tools": ["get_stock_balance"],
	},
	{
		"group": "Look things up",
		"title": "Check a leave balance",
		"prompt": "How much leave does this employee have left this year?",
		"tools": ["get_leave_balance_on"],
	},
	# Create & update -> asks_to_approve
	{
		"group": "Create & update",
		"title": "Raise a sales order",
		"prompt": "Raise a sales order for this customer: 10 x Widget A",
		"tools": ["create_doc"],
	},
	{
		"group": "Create & update",
		"title": "Update a record",
		"prompt": "Change the delivery date on this sales order to next Friday",
		"tools": ["update_doc"],
	},
	{
		"group": "Create & update",
		"title": "Draft a document for review",
		"prompt": "Draft a quotation for this customer for me to review",
		"tools": ["create_doc"],
	},
	# Send & submit -> always_asks
	{
		"group": "Send & submit",
		"title": "Send a document by email",
		"prompt": "Email this month's statement to the customer's accounts contact",
		"tools": ["send_email"],
	},
	{
		"group": "Send & submit",
		"title": "Submit a document",
		"prompt": "Submit this draft sales invoice",
		"tools": ["submit_doc"],
	},
	{
		"group": "Send & submit",
		"title": "Move a document through its workflow",
		"prompt": "Approve this leave application",
		"tools": ["apply_workflow_action"],
	},
	{
		"group": "Send & submit",
		"title": "Cancel a document",
		"prompt": "Cancel this sales invoice",
		"tools": ["cancel_doc"],
	},
]
