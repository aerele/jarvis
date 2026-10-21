// Global floating Jarvis widget — a draggable, edge-snapping FAB that opens
// the side chat panel in place, present on every Desk page EXCEPT the full
// chat page, the onboarding flow (where the agent isn't ready yet), Frappe's
// setup wizard (a fresh install whose site setup isn't finished, where
// Frappe blocks the desk on the wizard), and any page where ERPNext's own
// setup is otherwise incomplete (no Company yet). The visibility rule lives
// in widget_visibility.mjs. Sites may also opt to omit the widget entirely
// for users without Jarvis access; the default retains the no-access redirect.
//
// The panel is a child of the FAB component, so hiding this host hides both.
// On a narrow viewport the FAB navigates to the chat SPA instead of opening a
// panel that would cover the screen (see config.mjs PANEL_MIN_VIEWPORT_PX).
//
// Mounted ONCE into a <body>-level host so it survives SPA navigation: its
// dragged position persists (via localStorage) as you move between
// invoices/reports. Route changes only toggle the host's visibility.

import { createApp } from "vue";
import Widget from "./jarvis_chat/widget/Widget.vue";
import {
	shouldHideWidget,
	shouldHideWidgetForAccess,
} from "./jarvis_chat/widget/widget_visibility.mjs";

(function () {
	if (window.__jarvisWidgetBooted) return;

	let host = null;

	function ensureMounted() {
		if (host) return;
		host = document.createElement("div");
		host.id = "jarvis-widget-host";
		document.body.appendChild(host);
		createApp(Widget).mount(host);
	}

	function sync() {
		ensureMounted();
		const route = (window.frappe && frappe.get_route && frappe.get_route()) || [];
		const siteSetupComplete = window.frappe?.boot?.jarvis_site_setup_complete;
		host.style.display = shouldHideWidget(route, siteSetupComplete) ? "none" : "";
	}

	function start() {
		if (!window.frappe) return;
		window.__jarvisWidgetBooted = true;
		if (
			shouldHideWidgetForAccess(
				frappe.boot?.jarvis_has_access,
				frappe.boot?.jarvis_hide_shortcut_without_access
			)
		)
			return;
		sync();
		if (frappe.router && frappe.router.on) frappe.router.on("change", sync);
	}

	// Desk may not be fully booted when this include runs.
	if (window.frappe && window.frappe.router) {
		start();
	} else if (window.$) {
		$(start);
	} else {
		document.addEventListener("DOMContentLoaded", start);
	}
})();
