// Called only after the owner read authoritatively refuses the current route.
import { isSharedWithMe } from "@/api/chatSharing";
import { getCollaboration } from "@/api/chatCollaboration";
export function collaborationNoticeRoute(event, current) {
	return current && event?.kind === "collaboration:changed" && event.conversation_id === current
		? "/shared/" + encodeURIComponent(current)
		: null;
}
export async function sharedConversationRoute(
	id,
	probe = isSharedWithMe,
	isCurrent = () => true,
	commonProbe = getCollaboration
) {
	try {
		if (await probe(id)) return isCurrent() ? "/shared/" + encodeURIComponent(id) : null;
		if (!isCurrent()) return null;
		const common = await commonProbe(id);
		return common?.conversation?.activated && isCurrent()
			? "/shared/" + encodeURIComponent(id)
			: null;
	} catch {
		return null;
	}
}
