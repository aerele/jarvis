const LIMIT = 2;
const WINDOW_MS = 5 * 60 * 1000;

// Caps automatic recoveries (after a scope change or an expiry) so a calendar that keeps
// changing cannot loop. It is time based on purpose: a recovery that ends "ready" must
// not refill it. Each recovery frees its place WINDOW_MS after it was taken.
export class RecoveryBudget {
	#times = [];

	take(now = Date.now()) {
		this.#times = this.#times.filter((time) => now - time < WINDOW_MS);
		if (this.#times.length >= LIMIT) return false;
		this.#times.push(now);
		return true;
	}

	reset() {
		this.#times = [];
	}
}
