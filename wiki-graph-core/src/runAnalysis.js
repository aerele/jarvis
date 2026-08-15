// Analysis client — runs fullAnalysis in a Web Worker (R4: never blocks the
// render thread), degrading to a synchronous call when workers are unavailable
// or fail. Callers just `await runAnalysis(data)`.
import { fullAnalysis } from "./analysis.js";

let _worker = null,
	_seq = 0;
const _pending = new Map();

function _ensureWorker() {
	if (_worker || typeof Worker === "undefined") return _worker;
	try {
		_worker = new Worker(new URL("./analysis.worker.js", import.meta.url), { type: "module" });
		_worker.onmessage = (e) => {
			const { id, ok, result, error } = e.data || {};
			const p = _pending.get(id);
			if (!p) return;
			_pending.delete(id);
			ok ? p.resolve(result) : p.reject(new Error(error));
		};
		_worker.onerror = () => {
			_worker = null;
			// worker chunk failed (stale deploy, CSP) — reject every queued call so
			// callers' .catch fallback fires instead of hanging forever
			for (const p of _pending.values()) p.reject(new Error("worker error"));
			_pending.clear();
		};
	} catch (_) {
		_worker = null;
	}
	return _worker;
}

export function runAnalysis(data) {
	const w = _ensureWorker();
	if (!w) return Promise.resolve(fullAnalysis(data)); // graceful sync fallback
	// data is a Vue reactive proxy (computed over reactive state) — postMessage
	// throws DataCloneError on Proxy, so send a plain-JSON clone instead
	let plain;
	try {
		plain = JSON.parse(JSON.stringify(data));
	} catch (_) {
		plain = data;
	}
	const id = ++_seq;
	return new Promise((resolve, reject) => {
		_pending.set(id, { resolve, reject });
		w.postMessage({ id, data: plain });
	})
		.catch(() => fullAnalysis(data))
		.finally(() => {
			_pending.delete(id);
		}); // any worker error → sync
}
