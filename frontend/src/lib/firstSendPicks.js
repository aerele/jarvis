// The model and thinking picks a send must carry on the wire itself.
//
// A pick is normally saved the moment it is made (set_conversation_model /
// set_conversation_thinking). A chat started from the home screen has no
// conversation yet, so there is nowhere to save it: send_message creates the
// conversation, and the pick has to ride on that first send or the turn runs on
// the default while the pill shows the pick.
//
// An existing conversation never re-sends its pick: send_message would
// re-validate a pin loaded from the server on every message.
//
// Pure and import-free so the SPA and its tests share one definition.
export function firstSendPicks(sentFrom, model, thinking) {
	if (sentFrom) return { model: undefined, thinking: undefined };
	return { model: model || undefined, thinking: thinking || undefined };
}
