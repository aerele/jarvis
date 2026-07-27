import { createApp } from "vue";
import { setConfig, frappeRequest, resourcesPlugin } from "frappe-ui";

import App from "./App.vue";
import router from "./router";
import { initSocket } from "./socket";
import { session, requireLogin } from "./data/session";
import { applyBrandChrome } from "./branding";
// Tailwind pipeline entry (frappe-ui/style.css + compensations + dark bridge)
// MUST load before main.css so jv-* globals win cascade ties (DESIGN-V3 §2.3).
import "./index.css";
import "./main.css";

// Whitelabel: patch tab title + favicon from boot before anything paints.
applyBrandChrome();

// Bounce to Frappe login if there's no session (cookie) - same auth as Desk.
requireLogin();

setConfig("resourceFetcher", frappeRequest);

const app = createApp(App);
app.use(resourcesPlugin);
app.use(router);

// `?nosocket` skips the realtime connection - handy for headless screenshots,
// where an open websocket otherwise keeps the page from ever going idle.
const socket = window.location.search.includes("nosocket") ? null : initSocket();
app.provide("$socket", socket);
app.provide("$session", session);

app.mount("#app");
