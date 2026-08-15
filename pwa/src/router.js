import { createRouter, createWebHistory } from "vue-router";
import ChatsView from "./views/ChatsView.vue";

// history base = the route Frappe serves the shell at. The catch-all rule in
// hooks.py ("/jarvis-mobile/<path:app_path>") hands every deep link back to the
// same page, so a refresh on /jarvis-mobile/c/<id> resolves here rather than 404ing.
//
// The destinations are the native app's, and only those: New chat, Chats,
// Business, File Box, plus the account and settings pages the drawer's foot
// opens. Write approvals are answered in the chat that raised them.
const routes = [
	{ path: "/", name: "Chats", component: ChatsView },
	{ path: "/login", name: "Login", component: () => import("./views/LoginView.vue") },
	{ path: "/c/new", name: "NewChat", component: () => import("./views/NewChatView.vue") },
	{ path: "/c/:id", name: "Chat", component: () => import("./views/ChatView.vue"), props: true },
	{
		path: "/notifications",
		name: "Notifications",
		component: () => import("./views/NotificationsView.vue"),
	},
	{ path: "/business", name: "Business", component: () => import("./views/BusinessView.vue") },
	{ path: "/files", name: "FileBox", component: () => import("./views/FileBoxView.vue") },
	{ path: "/settings", name: "Settings", component: () => import("./views/SettingsView.vue") },
	{ path: "/account", name: "Account", component: () => import("./views/AccountView.vue") },
	{ path: "/:pathMatch(.*)*", redirect: "/" },
];

/** The signed-in user, per the cookie Frappe sets on login. "Guest" means nobody. */
export function sessionUser() {
	const cookies = new URLSearchParams(document.cookie.split("; ").join("&"));
	const user = cookies.get("user_id");
	return !user || user === "Guest" ? null : decodeURIComponent(user);
}

const router = createRouter({
	history: createWebHistory("/jarvis-mobile"),
	routes,
});

// Sign-in happens INSIDE the app. The shell renders for guests (see
// www/jarvis_mobile.py) precisely so this guard can show the app's own login
// screen: a standalone PWA that navigates outside its scope — which /login on
// the Desk is — gets handed to the browser, and the user finds themselves in a
// Chrome tab instead of the app they just tapped.
router.beforeEach((to) => {
	const signedIn = !!sessionUser();
	if (!signedIn && to.name !== "Login") return { name: "Login" };
	if (signedIn && to.name === "Login") return { name: "Chats" };
	return true;
});

export default router;
