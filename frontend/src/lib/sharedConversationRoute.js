// Called only after the owner read authoritatively refuses the current route.
import { isSharedWithMe } from "@/api/chatSharing";
export async function sharedConversationRoute(id, probe = isSharedWithMe, isCurrent = () => true) {
	try {
		return (await probe(id)) && isCurrent() ? "/shared/" + encodeURIComponent(id) : null;
	} catch {
		return null;
	}
}
