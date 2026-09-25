// Shared Approve/Reject verb detection (sheet.js's answer-mapping and
// ApprovalsBoard's chip-noise filter both need it).
export const APPROVE_RE = /^approve(d)?\b/i;
export const REJECT_RE = /^reject(ed)?\b/i;

// Whether `options` is an Approve/Reject verb pair: a lone approve-verb
// option and a lone reject-verb one.
export function isApproveRejectPair(options) {
	if (!options || options.length !== 2) return false;
	const low = options.map((o) => String(o).trim().toLowerCase());
	return (
		low.filter((o) => APPROVE_RE.test(o)).length === 1 &&
		low.filter((o) => REJECT_RE.test(o)).length === 1
	);
}
