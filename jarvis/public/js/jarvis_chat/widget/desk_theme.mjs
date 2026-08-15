// Is the Desk currently dark?
//
// Why this exists rather than a CSS selector: Vue's scoped-style compiler here
// drops the descendant in `:global([data-theme="dark"]) .foo`, emitting a bare
// `[data-theme=dark] { ... }`. Custom properties declared that way land on
// <html> and are then OVERRIDDEN by the component's own `.foo { --x: light }`
// rule, so the dark values never win. Resolving the flag in JS and binding a
// class on the same element sidesteps the whole problem.
//
// Frappe's theme switcher sets `data-theme` to the RESOLVED theme and
// `data-theme-mode` to the user's choice ("light" | "dark" | "automatic"), so
// `data-theme` alone is usually enough — but when the mode is automatic and the
// attribute has not been stamped yet, fall back to the OS preference.

export function resolveDark(theme, mode, prefersDark) {
  const t = String(theme || "").toLowerCase();
  if (t === "dark") return true;
  if (t === "light") return false;
  const m = String(mode || "").toLowerCase();
  if (m === "dark") return true;
  if (m === "light") return false;
  if (m === "automatic" || m === "auto" || m === "")
    return Boolean(prefersDark);
  return false;
}

export function isDarkNow() {
  if (typeof document === "undefined") return false;
  const root = document.documentElement;
  let prefers = false;
  try {
    prefers =
      window.matchMedia?.("(prefers-color-scheme: dark)")?.matches || false;
  } catch (e) {
    prefers = false;
  }
  return resolveDark(
    root.getAttribute("data-theme"),
    root.getAttribute("data-theme-mode"),
    prefers
  );
}

// Calls back whenever the Desk theme changes. Returns a dispose function.
export function watchTheme(onChange) {
  if (typeof document === "undefined") return () => {};
  const root = document.documentElement;
  const fire = () => onChange(isDarkNow());
  const obs = new MutationObserver(fire);
  obs.observe(root, {
    attributes: true,
    attributeFilter: ["data-theme", "data-theme-mode"],
  });

  let mq = null;
  try {
    mq = window.matchMedia?.("(prefers-color-scheme: dark)") || null;
    mq?.addEventListener?.("change", fire);
  } catch (e) {
    mq = null;
  }

  return () => {
    obs.disconnect();
    try {
      mq?.removeEventListener?.("change", fire);
    } catch (e) {
      /* nothing to undo */
    }
  };
}
