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
	// {} or {status, to_scope, target_role, target_roles[], reviewer, reviewer_name, decided_at, decision_note}
	req: { type: Object, default: null },
	// "skill" | "page" — used in the tooltip copy.
	noun: { type: String, default: "skill" },
});

// The full role set a Role request targets; my_skill_promotion returns
// `target_roles`, falling back to the single `target_role` for legacy rows.
function roleList(r) {
	if (r.target_roles && r.target_roles.length) return r.target_roles;
	return r.target_role ? [r.target_role] : [];
}
function targetLabel(r) {
	if (r.to_scope !== "Role") return "Org";
	const roles = roleList(r);
	if (!roles.length) return "Role: -";
	if (roles.length === 1) return `Role: ${roles[0]}`;
	// Keep the chip compact: first two roles, then an overflow count.
	const shown = roles.slice(0, 2).join(", ");
	return roles.length > 2 ? `Roles: ${shown} +${roles.length - 2}` : `Roles: ${shown}`;
}

const status = computed(() => (props.req && props.req.status) || "");
const theme = computed(() =>
	status.value === "Approved" ? "green" : status.value === "Rejected" ? "red" : "orange"
);
const label = computed(() => {
	const r = props.req || {};
	if (status.value === "Approved")
		// A reviewer may approve a SUBSET of the requested roles; `target_roles` is the
		// ASK, not the grant. When the decision note records a trim, don't enumerate the
		// requested roles as if granted — the exact granted set is in the tooltip.
		return (r.decision_note || "").trim()
			? "Promotion approved"
			: `Promoted to ${targetLabel(r)}`;
	if (status.value === "Rejected") return "Promotion rejected";
	return "Promotion requested";
});
const tip = computed(() => {
	const r = props.req || {};
	if (status.value === "Pending")
		return `Requested to promote this ${props.noun} to ${targetLabel(
			r
		)}, awaiting a reviewer.`;
	const who = r.reviewer_name || r.reviewer || "a reviewer";
	const when = r.decided_at ? ` ${timeAgo(r.decided_at)}` : "";
	const note = (r.decision_note || "").trim();
	if (status.value === "Approved")
		// The decision note carries the ACTUAL granted roles when the reviewer trimmed
		// the request (it names the kept set); prefer it over the requested-role label so
		// the audience is never overstated. No note => the full requested set was granted.
		return note
			? `Approved by ${who}${when}. ${note}`
			: `Approved by ${who}${when}. Now visible to ${targetLabel(r)}.`;
	return `Rejected by ${who}${when}.` + (note ? ` Reason: ${note}` : " No reason given.");
});
</script>
