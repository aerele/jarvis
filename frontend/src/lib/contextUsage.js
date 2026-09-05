function finiteNumber(value) {
	const number = Number(value);
	return Number.isFinite(number) && number >= 0 ? number : 0;
}

export function compactTokenCount(value) {
	const count = finiteNumber(value);
	if (count < 1000) return String(Math.round(count));
	if (count < 1_000_000)
		return `${(count / 1000).toFixed(count < 10_000 ? 1 : 0).replace(/\.0$/, "")}k`;
	return `${(count / 1_000_000).toFixed(count < 10_000_000 ? 1 : 0).replace(/\.0$/, "")}m`;
}

/** Presentation contract for the compact composer context pill. */
export function contextUsageView(usage) {
	const used = finiteNumber(usage && usage.used);
	const capacity = finiteNumber(usage && usage.capacity);
	const rawPct = finiteNumber(usage && usage.pct);
	const pct = capacity ? Math.min(999, Math.round(rawPct || (used / capacity) * 100)) : 0;
	if (!capacity) {
		return {
			label: "Context —",
			title: "Context usage is available after the first completed response.",
			tone: "neutral",
		};
	}
	return {
		label: `Context ${pct}%`,
		title: `${compactTokenCount(used)} of ${compactTokenCount(capacity)} tokens used`,
		tone: pct >= 90 ? "critical" : pct >= 75 ? "warning" : "neutral",
	};
}
