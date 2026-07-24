<template>
	<Tooltip v-if="status" :text="tip">
		<Badge variant="subtle" size="lg" :theme="theme" :label="label" />
	</Tooltip>
</template>

<script setup>
// Promotion status chip (Skills-area promotion surfacing) — the requester-side
// "requested → approved / rejected" indicator, ONE visual language shared by the
// skill detail page and the wiki page dialog. Fed by my_skill_promotion /
// my_wiki_promotion (the caller's most-recent request for this item, or {}).
// Renders nothing when there is no request.
import { computed } from "vue";
import { Badge, Tooltip } from "frappe-ui";
import { timeAgo } from "@/utils/datetime";

const props = defineProps({
	// {} or {status, to_scope, target_role, reviewer, reviewer_name, decided_at, decision_note}
	req: { type: Object, default: null },
	// "skill" | "page" — used in the tooltip copy.
	noun: { type: String, default: "skill" },
});

function targetLabel(r) {
	return r.to_scope === "Role" ? `Role: ${r.target_role || "—"}` : "Org";
}

const status = computed(() => (props.req && props.req.status) || "");
const theme = computed(() =>
	status.value === "Approved" ? "green" : status.value === "Rejected" ? "red" : "orange"
);
const label = computed(() => {
	if (status.value === "Approved") return `Promoted to ${targetLabel(props.req)}`;
	if (status.value === "Rejected") return "Promotion rejected";
	return "Promotion requested";
});
const tip = computed(() => {
	const r = props.req || {};
	if (status.value === "Pending")
		return `Requested to promote this ${props.noun} to ${targetLabel(
			r
		)} — awaiting a reviewer.`;
	const who = r.reviewer_name || r.reviewer || "a reviewer";
	const when = r.decided_at ? ` ${timeAgo(r.decided_at)}` : "";
	if (status.value === "Approved")
		return `Approved by ${who}${when}. Now visible to ${targetLabel(r)}.`;
	const reason = (r.decision_note || "").trim();
	return `Rejected by ${who}${when}.` + (reason ? ` Reason: ${reason}` : " No reason given.");
});
</script>
