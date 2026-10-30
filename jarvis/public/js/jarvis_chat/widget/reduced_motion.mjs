// The user's reduced-motion preference, read in one place for the Desk widget. Text only.

export const REDUCED_QUERY = "(prefers-reduced-motion: reduce)";

/** True when the user asks for less motion. False where the browser cannot say. */
export function prefersReduced(win = globalThis.window) {
  try {
    return win?.matchMedia?.(REDUCED_QUERY).matches === true;
  } catch (error) {
    return false;
  }
}

/** Calls `onChange(reduced)` whenever the preference changes. Returns the function that stops it. */
export function watchReducedMotion(onChange, win = globalThis.window) {
  let list = null;
  try {
    list = win?.matchMedia?.(REDUCED_QUERY) || null;
  } catch (error) {
    list = null;
  }
  if (!list || typeof list.addEventListener !== "function") return () => {};
  const listener = (event) => onChange(event.matches === true);
  list.addEventListener("change", listener);
  return () => list.removeEventListener("change", listener);
}
