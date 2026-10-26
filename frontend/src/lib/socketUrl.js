// Where the page's socket.io server lives, by Desk's rule (frappe socketio_client
// get_host): production serves it on the page's own origin (nginx routes
// /socket.io), and only `bench start` runs it on a port of its own. Unlike Desk, a
// portless page always stays on its origin: a dev bench behind an https proxy has
// no socketio port the browser can reach. The origin keeps the page's scheme: an
// http-only bench dialling wss://host:443 never connects.
//
// `boot` is the page's boot values (window.<key>). The Vite dev page has none, so
// there a port still means a dev server, as before.
//
// The PWA imports this file too (@shared), so keep it free of dependencies.
export function socketUrl(location, boot, fallbackPort) {
	return `${socketOrigin(location, boot, fallbackPort)}/${boot.site_name || location.hostname}`;
}

function socketOrigin(location, boot, fallbackPort) {
	const devServer = boot.dev_server ?? true;
	if (!location.port || !devServer) return location.origin;
	// bench start's socketio is plain http, whatever scheme the page came over.
	return `http://${location.hostname}:${boot.socketio_port || fallbackPort}`;
}
