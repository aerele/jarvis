// What an export card shows for one job state: pill, body, footer and buttons.
// Pure functions, so the component stays a template. Copy and layout follow the approved mock.
import { __ } from "./i18n.js";
import { formatBytes } from "./exportJobs.js";

const MAX_ATTEMPTS = 2;

function toDate(value) {
	if (!value) return null;
	const date = new Date(String(value).replace(" ", "T"));
	return Number.isNaN(date.getTime()) ? null : date;
}

const timeOf = (date) => date.toLocaleTimeString([], { hour: "numeric", minute: "2-digit" });

// "2:31 PM" today, "tomorrow 2:31 PM", otherwise "12 Oct, 2:31 PM".
export function whenText(value, now = Date.now()) {
	const date = toDate(value);
	if (!date) return "";
	const days = Math.round(
		(new Date(date).setHours(0, 0, 0, 0) - new Date(now).setHours(0, 0, 0, 0)) / 86400000,
	);
	if (days === 0) return timeOf(date);
	if (days === 1) return __("tomorrow {0}", [timeOf(date)]);
	return `${date.toLocaleDateString([], { day: "numeric", month: "short" })}, ${timeOf(date)}`;
}

export function clockText(seconds) {
	const total = Math.max(0, Math.round(seconds));
	return `${Math.floor(total / 60)}:${String(total % 60).padStart(2, "0")}`;
}

function secondsBetween(from, to) {
	const start = toDate(from);
	const end = toDate(to);
	return start && end ? (end - start) / 1000 : null;
}

function formatLine(state) {
	if (!state.format) return "";
	const name = state.format === "xlsx" ? __("Excel") : state.format.toUpperCase();
	const columns = Number(state.column_count) || 0;
	if (!columns) return name;
	return `${name}, ${columns === 1 ? __("1 column") : __("{0} columns", [columns])}`;
}

const rowsStat = (count, label) => ({
	strong: Number(count || 0).toLocaleString(),
	text: ` ${label}`,
});

const UNKNOWN = { kind: "unknown" };

// What the running card can say about progress, from the server's counted total (0/null =
// unknown) and the rows written so far. `unknown` keeps today's activity bar and row count: no
// count, a stale count (more rows than counted), or nothing written yet (an empty "0%" track
// would look like a queued one). The percent stops at 99 until the file is really ready, and
// is never 0 once rows exist. `finalizing` (zip + publish, nothing to measure) is never claimed
// while a cancel is pending (the server's flag, or the card's own request still in flight),
// because a file is not coming.
export function progressOf(state, cancelling = false) {
	if (state?.status !== "running") return UNKNOWN;
	if (state.phase === "finalizing" && !state.cancel_requested && !cancelling)
		return { kind: "finalizing" };
	const rows = Number(state.rows_written) || 0;
	const total = Number(state.total_rows) || 0;
	if (rows <= 0 || rows > total) return UNKNOWN;
	const percent = Math.max(1, Math.min(99, Math.round((rows * 100) / total)));
	return { kind: "value", percent, rows, total };
}

// The first stat of a running card.
function progressStat(state, progress) {
	if (progress.kind === "value")
		return {
			strong: progress.rows.toLocaleString(),
			text: ` ${__("of {0} rows", [progress.total.toLocaleString()])} · ${__("{0}%", [
				progress.percent,
			])}`,
		};
	return rowsStat(state.rows_written, __("rows written"));
}

const finalizingLabel = (state) =>
	state.format === "xlsx" ? __("Finalizing spreadsheet\u2026") : __("Finalizing file\u2026");

function runningProgress(state, cancelling) {
	const progress = progressOf(state, cancelling);
	if (progress.kind === "finalizing")
		return {
			track: "indet",
			stats: [
				{ strong: finalizingLabel(state) },
				rowsStat(state.rows_written, __("rows written")),
			],
		};
	if (progress.kind === "value")
		return {
			track: "value",
			percent: progress.percent,
			progressText: __("{0} of {1} rows, {2}%", [
				progress.rows.toLocaleString(),
				progress.total.toLocaleString(),
				progress.percent,
			]),
			stats: [progressStat(state, progress)],
		};
	return { track: "indet", stats: [progressStat(state, progress)] };
}

const attempt = (n) => __("Attempt {0} of {1}", [n, MAX_ATTEMPTS]);

// The attempt count comes from `retries`: a maintenance pause raises before `attempts` moves.
const retried = (state) => Number(state.retries) >= 1;

export function downloadUrl(state) {
	const url = state?.file_url;
	return state?.status === "ready" &&
		typeof url === "string" &&
		url.startsWith("/private/files/")
		? url
		: null;
}

const tone = (text, kind = "") => ({ text, tone: kind });

// One entry per status: the parts of the card that depend on it.
const BUILDERS = {
	queued: (s, ctx) => ({
		// A saved report's export waits, queued, for the report's own background run
		// (phase "generating"); it takes a worker only once that copy exists.
		pill: s.phase === "generating" ? tone(__("Generating report"), "run") : tone(__("Queued")),
		track: s.phase === "generating" ? "indet" : "empty",
		notes: [
			s.error
				? tone(s.error, "warn")
				: s.phase === "generating"
					? tone(
							__(
								"The report is being generated in the background; the file follows automatically.",
							),
						)
					: tone(__("Waiting for a free background worker; it starts automatically.")),
		],
		footer: s.asked_at
			? __("Asked at {0}", [whenText(s.asked_at, ctx.now)]) +
				(retried(s) ? ` · ${__("attempt 2 of 2 next")}` : "")
			: "",
		buttons: ["cancel"],
	}),
	running: (s, ctx) => {
		const { stats, ...progress } = runningProgress(s, ctx.cancelling);
		return {
			pill: tone(__("Preparing"), "run"),
			...progress,
			stats: [
				...stats,
				{
					strong: clockText((ctx.now - (toDate(s.started_at) || ctx.now)) / 1000),
					text: ` ${__("so far")}`,
				},
				...(formatLine(s) ? [{ text: formatLine(s) }] : []),
			],
			footer:
				(s.started_at ? __("Started at {0}", [whenText(s.started_at, ctx.now)]) : "") +
				(retried(s) ? ` · ${__("attempt 2 of 2")}` : "") +
				(ctx.cancelling ? ` · ${__("stops within a few seconds")}` : ""),
			buttons: ["cancel"],
		};
	},
	paused: (s, ctx) => {
		const next = toDate(s.next_attempt);
		const after = next && next > ctx.now ? whenText(s.next_attempt, ctx.now) : "";
		const base = __(
			"The site is being updated, so this export stopped to stay out of its way.",
		);
		const rest = after
			? __("It starts again automatically after {0}.", [after])
			: __("It starts again automatically shortly.");
		return {
			pill: tone(__("Paused"), "warn"),
			track: "empty",
			notes: [tone(`${base} ${rest}`, "warn")],
			footer: attempt(Math.max(1, Number(s.retries) || 0)),
			buttons: ["cancel"],
		};
	},
	ready: (s, ctx) => {
		const took = secondsBetween(s.started_at, s.finished_at);
		const made = s.finished_at ? whenText(s.finished_at, ctx.now) : "";
		const notes = [];
		if (made)
			notes.push(
				tone(
					__("Made at {0} from the data at that time. Ask again for fresh numbers.", [
						made,
					]),
				),
			);
		if (s.limited_by_spec)
			notes.push(
				tone(
					__("Holds only the first {0} rows the query asked for.", [s.limited_by_spec]),
					"warn",
				),
			);
		if (s.cells_truncated)
			notes.push(
				tone(__("Some long cells were shortened; the row count is complete."), "warn"),
			);
		return {
			pill: tone(__("Ready"), "ok"),
			stats: [
				rowsStat(s.rows_written, __("rows")),
				...(s.size_bytes ? [{ strong: formatBytes(s.size_bytes), text: "" }] : []),
				...(formatLine(s) ? [{ text: formatLine(s) }] : []),
				...(took !== null ? [{ text: __("took {0}", [clockText(took)]) }] : []),
			],
			notes,
			footer: s.expires_at
				? __("Available until {0}", [whenText(s.expires_at, ctx.now)])
				: __("Kept until you delete this chat"),
			buttons: downloadUrl(s) ? ["download"] : [],
		};
	},
	"too large": (s) => ({
		pill: tone(__("Not exported"), "bad"),
		notes: [tone(s.error || __("This export is larger than one file can hold."), "bad")],
		footer: __("Nothing was saved"),
	}),
	"too slow": (s) => ({
		pill: tone(__("Stopped"), "bad"),
		notes: [tone(s.error || __("The export took too long and was stopped."), "bad")],
		footer: __("Nothing was saved"),
	}),
	failed: (s) => ({
		pill: tone(__("Failed"), "bad"),
		notes: [tone(s.error || __("The export could not finish."), "bad")],
		footer: retried(s) ? attempt(MAX_ATTEMPTS) : __("Nothing was saved"),
	}),
	cancelled: (s, ctx) => ({
		pill: tone(__("Cancelled")),
		notes: [
			tone(
				s.finished_at
					? __("You cancelled this export at {0}. Nothing was saved.", [
							whenText(s.finished_at, ctx.now),
						])
					: __("You cancelled this export. Nothing was saved."),
			),
		],
	}),
	expired: () => ({
		pill: tone(__("Expired")),
		notes: [tone(__("This file is no longer kept. Ask again for a fresh export."))],
	}),
};

// The job row is gone; what is left is the saved summary on the card message.
function missingView(summary, ctx) {
	const made =
		summary.status === "ready" && summary.finished_at
			? whenText(summary.finished_at, ctx.now)
			: "";
	return {
		pill: tone(__("Unavailable")),
		stats: [
			rowsStat(summary.rows_written, __("rows")),
			...(made ? [{ text: __("Made at {0}", [made]) }] : []),
		],
	};
}

// `entry` is exportJobs[id]: undefined while loading, {state: null} when the job is gone.
export function exportCardView(summary, entry, ctx = {}) {
	const context = { now: Date.now(), cancelling: false, ...ctx };
	const state = entry?.state;
	const title = state?.title || summary.title || __("Export");
	const common = { title, track: null, stats: [], notes: [], footer: "", buttons: [] };
	const subtitle = __("Export");
	if (entry === undefined || state === undefined)
		return { ...common, pill: tone(__("Checking")), subtitle };
	if (state === null) return { ...common, ...missingView(summary, context), subtitle };
	const build = BUILDERS[state.status] || (() => ({ pill: tone(__("Checking")) }));
	return {
		...common,
		...build(state, context),
		subtitle: state.source_doctype ? __("Export · {0}", [state.source_doctype]) : subtitle,
	};
}
