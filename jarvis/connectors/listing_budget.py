"""Keep the no-argument connector listing inside a size budget.

A tenant with many connectors, each with many actions, would flood the chat
turn. The listing is shrunk in steps, and a connector is never dropped: the
agent must always be able to see every app it has.

  1. action descriptions clipped to 100 characters;
  2. then action names only;
  3. then, per connector, the first 10 action names plus ``more_actions``;
  4. last resort: no action names, only ``more_actions``.

Frappe-free: it only reshapes the dict the tool returns.
"""

from __future__ import annotations

import json

LISTING_MAX_CHARS = 24_000
_STEP_DESCRIPTION_MAX = 100
_STEP_ACTIONS_KEPT = 10
_HINT = "call list_connector_actions with connector=<key> for all actions"


def bound(listing: dict, budget: int = LISTING_MAX_CHARS) -> dict:
	"""``listing`` unchanged when it fits ``budget``, else the first shrunk
	copy that does (or the smallest copy when none does)."""
	if _size(listing) <= budget:
		return listing
	shrunk = listing
	for step in (_clip_descriptions, _names_only, _first_actions, _no_actions):
		shrunk = step(shrunk)
		if _size(shrunk) <= budget:
			break
	return shrunk


def _size(listing: dict) -> int:
	return len(json.dumps(listing))


def _clip_descriptions(listing: dict) -> dict:
	return _map_actions(listing, lambda actions: [_clipped(a) for a in actions])


def _names_only(listing: dict) -> dict:
	return _map_actions(listing, lambda actions: [{"action": a["action"]} for a in actions])


def _first_actions(listing: dict) -> dict:
	return _keep_first(listing, _STEP_ACTIONS_KEPT)


def _no_actions(listing: dict) -> dict:
	return _keep_first(listing, 0)


def _clipped(action: dict) -> dict:
	description = action.get("description")
	if not description:
		return action
	return {**action, "description": description[:_STEP_DESCRIPTION_MAX]}


def _keep_first(listing: dict, keep: int) -> dict:
	"""Cut each connector's actions to the first ``keep``, and say how many
	were left out (counting any cut made by an earlier step). A connector with
	nothing to cut is untouched."""

	def cut(entry: dict) -> dict:
		actions = entry["actions"]
		if len(actions) <= keep:
			return entry
		return {
			**entry,
			"actions": actions[:keep],
			"more_actions": entry.get("more_actions", 0) + len(actions) - keep,
			"hint": _HINT,
		}

	return {**listing, "connectors": [cut(entry) for entry in listing["connectors"]]}


def _map_actions(listing: dict, change) -> dict:
	connectors = [{**entry, "actions": change(entry["actions"])} for entry in listing["connectors"]]
	return {**listing, "connectors": connectors}
