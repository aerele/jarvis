// Per-preset guidance for AddConnectorDialog's Access token field (MCP
// Connectors P4 follow-up): a non-developer adding a connector has no idea
// which token, or which scopes, the app expects. Each entry is one short
// line naming the token/scopes plus a link straight to the vendor's own
// token-creation page (opens in a new tab, so the dialog and its in-progress
// form stay put). Custom URL has no vendor to link to, so AddConnectorDialog
// renders a generic line for it instead of reading this map.
//
// URLs verified against current vendor docs on 2026-09-04 (WebFetch/WebSearch,
// not memory):
//   GitHub     - https://docs.github.com/en/authentication/keeping-your-account-and-data-secure/managing-your-personal-access-tokens
//                (states the creation page is github.com/settings/personal-access-tokens/new)
//   Atlassian  - https://support.atlassian.com/atlassian-account/docs/manage-api-tokens-for-your-atlassian-account/
//                (states id.atlassian.com/manage-profile/security/api-tokens);
//                org-admin gate confirmed by
//                https://support.atlassian.com/security-and-access-policies/docs/authentication-policy-settings-for-your-organizations/
//                (authentication policies can allow/disallow personal API tokens)
//   Linear     - https://linear.app/docs/security-and-access (Settings > Account >
//                Security & Access; direct URL linear.app/settings/account/security
//                confirmed via merge.dev's walkthrough, which cites that page)
//   Stripe     - https://docs.stripe.com/keys.md (API keys page is
//                dashboard.stripe.com/apikeys; restricted-key creation flow
//                documented on the same page)
export const CONNECTOR_HELP = {
	GitHub: {
		tokenHint:
			"Needs a fine-grained token scoped to the repos you want connected, with Contents (read) and Pull requests (read and write) permissions.",
		tokenDocsUrl: "https://github.com/settings/personal-access-tokens/new",
	},
	Atlassian: {
		tokenHint:
			"Needs an Atlassian API token for your account. Your organization admin may need to enable API tokens for Jira and Confluence first.",
		tokenDocsUrl: "https://id.atlassian.com/manage-profile/security/api-tokens",
	},
	Linear: {
		tokenHint:
			"Needs a personal API key from your Linear workspace's Security & access settings.",
		tokenDocsUrl: "https://linear.app/settings/account/security",
	},
	Stripe: {
		tokenHint:
			"Needs a restricted API key with only the permissions this connector should use, not a full secret key.",
		tokenDocsUrl: "https://dashboard.stripe.com/apikeys",
	},
};

// Custom URL has no vendor page — AddConnectorDialog shows this text directly
// instead of a "how to create this token" link.
export const CUSTOM_URL_TOKEN_HINT = "Paste the bearer token or API key your MCP server expects.";
