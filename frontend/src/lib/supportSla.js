// Pure SLA-badge logic for the support "Ticket details" panel, ported from
// Helpdesk's customer-portal TicketCustomerSidebar.vue (firstResponseData /
// resolutionData). Kept dependency-free and unit-testable: inputs are epoch-ms
// numbers (the panel converts the ticket's naive SITE-timezone strings via
// @/utils/datetime's tz-aware toLocalMs first), `nowMs` is injectable, and
// `resolutionTime` is seconds (the HD Ticket field, already a duration).

// seconds -> a short human duration ("2d 3h" / "3h 4m" / "5m" / "12s").
export function formatDuration(seconds) {
	const s = Math.max(0, Math.round(Number(seconds) || 0));
	const d = Math.floor(s / 86400);
	const h = Math.floor((s % 86400) / 3600);
	const m = Math.floor((s % 3600) / 60);
	if (d) return `${d}d ${h}h`;
	if (h) return `${h}h ${m}m`;
	if (m) return `${m}m`;
	return `${s}s`;
}

// First-response SLA -> { label, theme } or null when there is no SLA to show.
// Mirrors Helpdesk's Due -> Fulfilled -> Failed ladder; the trailing null guard is
// a deliberate deviation so a ticket with NO first-response SLA target reads as
// "nothing to show" (panel renders "—") instead of Helpdesk's false "Failed".
export function firstResponseBadge(meta, nowMs = Date.now()) {
	const { firstRespondedOn = null, responseBy = null, creation = null } = meta || {};
	if (firstRespondedOn == null && responseBy != null && nowMs < responseBy) {
		return { label: `Due in ${formatDuration((responseBy - nowMs) / 1000)}`, theme: "orange" };
	}
	if (firstRespondedOn != null && responseBy != null && firstRespondedOn < responseBy) {
		const dur =
			creation != null ? ` in ${formatDuration((firstRespondedOn - creation) / 1000)}` : "";
		return { label: `Fulfilled${dur}`, theme: "green" };
	}
	if (responseBy == null && firstRespondedOn == null) return null;
	return { label: "Failed", theme: "red" };
}

// Resolution SLA -> { label, theme } or null. Same ladder; `resolutionTime` is the
// HD Ticket duration field in SECONDS (used directly, fixing the Helpdesk bug that
// wrapped it in dayjs()).
export function resolutionBadge(meta, nowMs = Date.now()) {
	const {
		resolutionDate = null,
		resolutionBy = null,
		agreementStatus = null,
		resolutionTime = null,
	} = meta || {};
	if (resolutionDate == null && resolutionBy != null && nowMs < resolutionBy) {
		return {
			label: `Due in ${formatDuration((resolutionBy - nowMs) / 1000)}`,
			theme: "orange",
		};
	}
	if (agreementStatus === "Fulfilled") {
		const dur = resolutionTime != null ? ` in ${formatDuration(resolutionTime)}` : "";
		return { label: `Fulfilled${dur}`, theme: "green" };
	}
	if (resolutionBy == null && resolutionDate == null && agreementStatus == null) return null;
	return { label: "Failed", theme: "red" };
}
