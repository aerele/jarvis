// debouncedFilterValues - shared by the dashboard viewer (DashboardView) and
// the builder (DashboardsPage): a filter bar's picks update its bound
// "values" ref immediately (so the control never lags the user's typing),
// while a separate "applied" ref - the one actually fed to DashboardCanvas -
// only updates 300ms after the last pick. Without this, every pick remounts
// the sandboxed iframe and re-runs the dashboard's sources.
//
// Framework-agnostic on purpose (plain refs in, plain function out) so it is
// trivial to unit-test with fake timers; each caller wires its own
// onBeforeUnmount(() => debounce.cancel()).
export const FILTER_DEBOUNCE_MS = 300;

/**
 * @param {import('vue').Ref<Object>} valuesRef bound to the filter bar's modelValue, updated immediately
 * @param {import('vue').Ref<Object>} appliedRef fed to the canvas, updated once the debounce settles
 * @param {(values: Object) => void} [onApply] called right after `appliedRef` updates, e.g. to clear stale errors or sync the URL
 * @returns {{onChange: (values: Object) => void, cancel: () => void}}
 */
export function debouncedFilterValues(valuesRef, appliedRef, onApply) {
	let timer = null;
	function onChange(values) {
		valuesRef.value = values;
		clearTimeout(timer);
		timer = setTimeout(() => {
			appliedRef.value = { ...values };
			if (onApply) onApply(values);
		}, FILTER_DEBOUNCE_MS);
	}
	function cancel() {
		clearTimeout(timer);
		timer = null;
	}
	return { onChange, cancel };
}
