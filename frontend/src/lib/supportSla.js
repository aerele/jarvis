// Pure SLA-badge logic for the support "Ticket details" panel. Inspired by
// Helpdesk's customer-portal TicketCustomerSidebar, but computed PER STAGE from
// each stage's own deadline fields (not the whole-SLA `agreement_status`, which is
// ambiguous per stage) — so a first-response that was met still reads green even
// when the resolution SLA later fails. Dependency-free + unit-testable: inputs are
// epoch-ms numbers (the panel converts naive SITE-tz strings via toLocalMs first),
// `nowMs` is injectable, and `resolutionTime` is seconds (the HD duration field).
//
// The one place `agreement_status` IS load-bearing: "Paused" (Helpdesk's
// awaiting-customer / on-hold state). It freezes the clock with a stale past
// deadline, so the raw deadline math would read "Failed" — we surface a neutral
// "On hold" instead of blaming ourselves for the customer's delay. A stage with no
// SLA target at all returns null (panel renders "—"), never a false "Failed".

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

// First-response SLA -> { label, theme } or null.
export function firstResponseBadge(meta, nowMs = Date.now()) {
	const {
		firstRespondedOn = null,
		responseBy = null,
		creation = null,
		agreementStatus = null,
	} = meta || {};
	if (firstRespondedOn != null) {
		// Responded: met if AT or before the target (or there was no target). `<=`
		// matches Helpdesk, which fails only on a strict deadline < actual, so a
		// response landing exactly on the deadline counts as fulfilled.
		if (responseBy == null || firstRespondedOn <= responseBy) {
			const dur =
				creation != null
					? ` in ${formatDuration((firstRespondedOn - creation) / 1000)}`
					: "";
			return { label: `Fulfilled${dur}`, theme: "green" };
		}
		return { label: "Failed", theme: "red" };
	}
	// Not yet responded.
	if (agreementStatus === "Paused") return { label: "On hold", theme: "gray" };
	if (responseBy == null) return null; // no first-response SLA -> nothing to show
	return nowMs < responseBy
		? { label: `Due in ${formatDuration((responseBy - nowMs) / 1000)}`, theme: "orange" }
		: { label: "Failed", theme: "red" };
}

// Resolution SLA -> { label, theme } or null. `resolutionTime` is SECONDS.
export function resolutionBadge(meta, nowMs = Date.now()) {
	const {
		resolutionDate = null,
		resolutionBy = null,
		resolutionTime = null,
		agreementStatus = null,
	} = meta || {};
	if (resolutionDate != null) {
		// Resolved: met if AT or before the target (or there was no target) — `<=`
		// to match Helpdesk's on-the-dot-is-fulfilled boundary (see firstResponseBadge).
		if (resolutionBy == null || resolutionDate <= resolutionBy) {
			const dur = resolutionTime != null ? ` in ${formatDuration(resolutionTime)}` : "";
			return { label: `Fulfilled${dur}`, theme: "green" };
		}
		return { label: "Failed", theme: "red" };
	}
	// Not yet resolved.
	if (agreementStatus === "Paused") return { label: "On hold", theme: "gray" };
	if (resolutionBy == null) return null; // no resolution SLA -> nothing to show
	return nowMs < resolutionBy
		? { label: `Due in ${formatDuration((resolutionBy - nowMs) / 1000)}`, theme: "orange" }
		: { label: "Failed", theme: "red" };
}
