import { describe, it, expect, beforeEach, afterEach, vi } from "vitest";
import { ref } from "vue";
import { debouncedFilterValues, FILTER_DEBOUNCE_MS } from "./debouncedFilterValues";

beforeEach(() => {
	vi.useFakeTimers();
});
afterEach(() => {
	vi.useRealTimers();
});

describe("debouncedFilterValues", () => {
	it("updates the values ref immediately but delays the applied ref", () => {
		const values = ref({});
		const applied = ref({});
		const { onChange } = debouncedFilterValues(values, applied);

		onChange({ assignee: "a@example.com" });
		expect(values.value).toEqual({ assignee: "a@example.com" });
		expect(applied.value).toEqual({});

		vi.advanceTimersByTime(FILTER_DEBOUNCE_MS);
		expect(applied.value).toEqual({ assignee: "a@example.com" });
	});

	it("collapses a rapid run of picks into one applied update", () => {
		const values = ref({});
		const applied = ref({});
		const onApply = vi.fn();
		const { onChange } = debouncedFilterValues(values, applied, onApply);

		onChange({ assignee: "a" });
		vi.advanceTimersByTime(FILTER_DEBOUNCE_MS - 50);
		onChange({ assignee: "ab" });
		vi.advanceTimersByTime(FILTER_DEBOUNCE_MS - 50);
		onChange({ assignee: "abc" });
		vi.advanceTimersByTime(FILTER_DEBOUNCE_MS);

		expect(applied.value).toEqual({ assignee: "abc" });
		expect(onApply).toHaveBeenCalledTimes(1);
		expect(onApply).toHaveBeenCalledWith({ assignee: "abc" });
	});

	it("cancel() drops a pending apply", () => {
		const values = ref({});
		const applied = ref({ assignee: "old" });
		const { onChange, cancel } = debouncedFilterValues(values, applied);

		onChange({ assignee: "new" });
		cancel();
		vi.advanceTimersByTime(FILTER_DEBOUNCE_MS);

		expect(applied.value).toEqual({ assignee: "old" });
	});
});
