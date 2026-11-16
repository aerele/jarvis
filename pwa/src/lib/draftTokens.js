// The colour names the shared draft components (DraftEditForm, DraftPreview, DraftCell,
// DraftLinkMenu, DraftRowBlock, DraftRowForm) read, mapped to the phone's own tokens.
//
// The desktop defines these 13 names on the chat root (frontend/src/theme.js). The phone has
// other names for the same roles, so each is the nearest phone token. The 6 other names the
// components read (--border --amber --amber-bg --red --red-bg --green) already exist on the
// phone with the phone's meaning and are not redefined.
//
// Why the names are scoped to a class and not defined on :root: two shared components the phone
// already draws read some of them with a fallback of their own (ExportCard.vue, DocumentBlocks.vue),
// and so does the holiday scene art (jarvis/public/js/shared/holiday/scenes/pongal-art.mjs reads
// --text-3). A global definition would repaint them.
//
// --text-3 is --ink6, not --ink5: the shared form draws hints and labels in it at small sizes, and
// --ink5 is about 4.2:1 on the card, under WCAG AA. --ink6 is 6.7:1 on white and 5.6:1 on the dark
// card, and --text-2 (--ink7) stays the stronger of the two in light and in dark.
//
// Two places read this map: pwa/src/index.css (`.jv-draft-tokens`, for everything inside a draft
// sheet or card) and the teleported link menu, which sits on <body> outside that class and takes
// `draftPalette()` as its `palette` prop. draftTokens.test.js keeps the two equal.
//
// The values are other tokens (`var(--card)`), so light and dark follow the phone's own dark
// blocks with nothing to switch here.
//
// --amber-bd and --red-bd have no phone token. The desktop's are opaque tints that equal its
// amber / red at about 22% over its surface (fitted by draftTokens.test.js), so these are the
// phone's amber / red at 22% over the phone's card colour.
export const DRAFT_ALPHA = 22;

export const DRAFT_TOKENS = {
	"--surface": "var(--card)",
	"--surface-1": "var(--card2)",
	"--surface-2": "var(--card3)",
	"--text": "var(--ink9)",
	"--text-2": "var(--ink7)",
	"--text-3": "var(--ink6)",
	"--border-2": "var(--border2)",
	"--cta": "var(--inv-bg)",
	"--cta-fg": "var(--inv-ink)",
	"--cta-bg": "var(--card3)",
	"--cta-bd": "var(--border2)",
	"--amber-bd": `color-mix(in srgb, var(--amber) ${DRAFT_ALPHA}%, var(--card))`,
	"--red-bd": `color-mix(in srgb, var(--red) ${DRAFT_ALPHA}%, var(--card))`,
};

// The `palette` prop of DraftLinkMenu (inline style on an element teleported to <body>). A new
// object each call, so a caller can never change the map.
export const draftPalette = () => ({ ...DRAFT_TOKENS });
