import { __ } from "@/lib/i18n";

export function sharedResultMessage(result) {
	const detail = [result?.error?.message, result?.reason].find(
		(value) => typeof value === "string" && value.trim()
	);
	if (["partial", "unknown"].includes(result?.outcome))
		return (
			__("The action may have partly run. Check its outcome before retrying.") +
			(detail ? " " + detail : "")
		);
	return detail || __("The action was not completed. Check its outcome below.");
}
