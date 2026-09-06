// Token guidance for AddConnectorDialog's Access token field (MCP Connectors
// P4 follow-up) now comes from the connector catalog itself
// (jarvis/connectors/catalog.py's `hint` and `help_url`), not a per-preset map
// kept here. Custom URL is the one path with no catalog entry to read - it
// has no vendor page either, so this is the only guidance left in this file.
export const CUSTOM_URL_TOKEN_HINT =
	"Paste an access token or API key from the app you are connecting.";
