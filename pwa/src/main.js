import { createApp } from "vue";
import { setConfig, frappeRequest, resourcesPlugin } from "frappe-ui";

import App from "./App.vue";
import router, { sessionUser } from "./router";
import { initSocket } from "./socket";
// Side-effect import, and it must stay ABOVE the mount: it registers the
// beforeinstallprompt listener at module load. Chrome can fire that event in the
// same tick the app mounts, so capturing it from a component's onMounted loses
// the race on a warm refresh and the install offer silently disappears.
import "./install";
import { applyTheme } from "./lib/theme";
import { applyBrandChrome } from "./branding";
import "./index.css";

setConfig("resourceFetcher", frappeRequest);

// Before mount: applying the saved theme after the first paint would flash the
// wrong palette, which on an OLED phone is very visible.
applyTheme();
// Whitelabel: patch tab title/favicon + repoint the manifest before mount.
applyBrandChrome();

const app = createApp(App);
app.use(resourcesPlugin);
app.use(router);
// A guest has no user room to join: the socket would only sit there retrying
// behind the login screen. Consumers already treat $socket as optional.
app.provide("$socket", sessionUser() ? initSocket() : null);
app.mount("#app");

// The worker is served from the site root (jarvis/pwa.py), NOT from
// /assets/jarvis/pwa/ where the bundle lives. A worker can only claim a scope
// at or below the path it is served from, and the app lives at /jarvis-mobile —
// so a worker shipped alongside the bundle could never control the app. Serving
// it at the root lets us claim a narrower scope explicitly: exactly the app,
// and never the Desk at /app.
if ("serviceWorker" in navigator) {
	window.addEventListener("load", () => {
		navigator.serviceWorker
			.register("/jarvis-mobile.sw.js", { scope: "/jarvis-mobile" })
			.catch((err) => console.error("Jarvis PWA: service worker failed to register", err));
	});
}
