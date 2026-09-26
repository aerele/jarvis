"""``Jarvis Pending Action``: durable, sealed executable cards (chat Confirm cards and
held File Box writes). Rows are disposable once the human has acted.

- ``park`` persists a sealed ``Pending`` row;
- ``execute`` claims then runs it at most once, as the sealed ``exec_user``;
- ``settle`` / ``settle_batch`` deliver the outcome once;
- ``discard`` / ``cancel_for_conversation`` / ``on_conversation_trash`` retire rows;
- ``reconcile`` (``*/5``) and ``purge`` (daily) keep the table honest.

Extension points for later callers: ``_park.SUPERSEDE_HOOK``,
``_settle.CONTINUATIONS``, ``_reconcile.HELD_RESUME`` (dotted paths), and park's
``legacy_pending`` / ``display_row`` parameters."""

from jarvis.chat.pending_actions._execute import ArmHook, ExecuteCrashed, authorize, execute
from jarvis.chat.pending_actions._lifecycle import (
	cancel_all_chat_pending,
	cancel_for_conversation,
	cancel_unverifiable,
	discard,
	on_conversation_trash,
	withdraw_all_held,
)
from jarvis.chat.pending_actions._ops import operator_fail, operator_settle
from jarvis.chat.pending_actions._park import ConfirmationPendingError, park, snapshot_targets
from jarvis.chat.pending_actions._reconcile import purge, reconcile
from jarvis.chat.pending_actions._settle import outcome_for, settle, settle_batch
from jarvis.chat.pending_actions._store import _terminal_update, _transition, claim_settled

__all__ = [
	"ArmHook",
	"ConfirmationPendingError",
	"ExecuteCrashed",
	"_terminal_update",
	"_transition",
	"authorize",
	"cancel_all_chat_pending",
	"cancel_for_conversation",
	"cancel_unverifiable",
	"claim_settled",
	"discard",
	"execute",
	"on_conversation_trash",
	"operator_fail",
	"operator_settle",
	"outcome_for",
	"park",
	"purge",
	"reconcile",
	"settle",
	"settle_batch",
	"snapshot_targets",
	"withdraw_all_held",
]
