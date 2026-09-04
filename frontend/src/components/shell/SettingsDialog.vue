<template>
	<!-- Shell-level settings dialog. Owns the scrim, the grouped rail and the
	     active pane; each pane owns its own header (design.md §4.1), which is
	     why there is no title bar or footer here.

	     frappe-ui's Dialog supplies the modality, focus trap, focus return and
	     Escape handling that this component used to hand-roll. It renders
	     through DialogPortal into <body>, so the stacking order that used to
	     come from .jv-settings-overlay's z-index:60 now comes from the
	     .dialog-overlay rule in main.css. The shell ConfirmDialog stays at 200
	     so a confirm still opens on top of settings.

	     disable-outside-click-to-close is bound to confirmOpen, not hardcoded.
	     ConfirmDialog is ALSO teleported to <body> (jarvis#438), so it is a DOM
	     sibling of this dialog's content, not a descendant — reka's
	     dismissable-layer treats any pointerdown on it, including Cancel, as
	     "outside" and closes this dialog too (jarvis#452). Disabling
	     outside-click-to-close only while a confirm is open keeps plain
	     backdrop clicks closing Settings (the #405 e2e-verified behaviour)
	     while a confirm is showing. Note: handling `@interact-outside`
	     directly, as first suggested on #452, does not work — frappe-ui's
	     Dialog.vue consumes that event internally on its own DialogContent and
	     never forwards it to callers of <Dialog>. -->
	<Dialog
		v-model="open"
		:options="{ size: '5xl' }"
		:disable-outside-click-to-close="confirmOpen"
	>
		<template #body>
			<!-- Overriding #body replaces Dialog's default content, which is where
			     frappe-ui renders both its DialogClose (X) button and the
			     DialogTitle that reka wires aria-labelledby to. Both have to be
			     supplied here or the dialog ships with no close affordance and an
			     unresolvable aria-labelledby. DialogTitle is visually hidden
			     because each pane already renders its own visible header. -->
			<DialogTitle as="h1" class="sr-only">Settings</DialogTitle>
			<!-- jv-dark + paletteVars are load-bearing, not decoration. The jv-
			     palette (--surface, --text, --red, ...) is deliberately NOT on
			     :root; it resolves only inside a subtree that binds it (see the
			     brand-token comment in main.css, and ConfirmDialog, which does the
			     same thing for the same reason). Dialog portals this content into
			     <body>, so it cannot inherit the palette from ChatView's root.
			     Every settings pane is now migrated to frappe-ui + semantic tokens;
			     the one thing left on legacy markup is LlmPoolEditor (rendered
			     inside AiModelsPane, deliberately deferred to its own PR -
			     jarvis#406). It would otherwise render with every var(--)
			     unresolved, and not one of the 161 in settings.css carries a
			     fallback. Worst in dark mode: frappe-ui chrome themes correctly
			     around a pane that has lost its backgrounds and borders. -->
			<div
				class="relative flex h-[calc(100vh-8rem)] flex-col sm:flex-row"
				:class="{ 'jv-dark': dark }"
				:style="paletteVars"
			>
				<!-- ===== grouped rail =====
				     Presentation, NOT a security boundary: /api/method is reachable
				     directly, so every endpoint gates itself server-side. -->
				<div
					class="flex shrink-0 gap-1 overflow-x-auto border-b bg-surface-menu-bar p-1 sm:w-56 sm:flex-col sm:gap-0.5 sm:overflow-y-auto sm:overflow-x-visible sm:rounded-l-lg sm:border-b-0 sm:px-3 sm:py-5"
				>
					<template v-for="group in visibleGroups" :key="group.name">
						<div
							class="hidden px-2 pb-1 pt-3 text-xs font-medium text-ink-gray-5 sm:block"
						>
							{{ group.name }}
						</div>
						<button
							v-for="item in group.items"
							:key="item.key"
							class="mx-0.5 flex h-7 shrink-0 items-center gap-2 rounded px-2 text-sm text-ink-gray-8"
							:class="[
								section === item.key
									? 'bg-surface-white shadow-sm'
									: 'hover:bg-surface-gray-2',
								applying ? 'cursor-not-allowed opacity-50' : '',
							]"
							:aria-current="section === item.key ? 'page' : undefined"
							:aria-disabled="applying"
							:disabled="applying"
							@click="go(item.key)"
						>
							<FeatherIcon :name="item.icon" class="size-4 shrink-0" />
							<span class="truncate">{{ item.label }}</span>
						</button>
					</template>
				</div>

				<!-- ===== active pane =====
				     flex-col matters: every pane's SettingsPane frame is `h-full`,
				     which only resolves against a flex-column ancestor with a
				     definite height. AiModelsPane's jv-pane-fill wrapper around
				     LlmPoolEditor needs the same thing for its save bar to sink to
				     the bottom. On a plain block wrapper both would clip silently
				     with no scrollbar. -->
				<div class="flex min-h-0 min-w-0 flex-1 flex-col overflow-hidden">
					<component :is="pane" />
				</div>

				<!-- Close lives at the dialog level, not in SettingsPane, so panes
				     that have not been migrated yet still get one. -->
				<DialogClose as-child>
					<button
						class="absolute right-3 top-3 flex size-7 items-center justify-center rounded text-ink-gray-7 hover:bg-surface-gray-3"
						aria-label="Close settings"
					>
						<FeatherIcon name="x" class="size-4" />
					</button>
				</DialogClose>
			</div>
		</template>
	</Dialog>
</template>

<script setup>
import { computed, defineAsyncComponent } from "vue";
import { Dialog, FeatherIcon } from "frappe-ui";
// Straight from reka-ui, the same primitives frappe-ui's Dialog uses
// internally. Needed because overriding the #body slot drops the ones it
// renders by default.
import { DialogClose, DialogTitle } from "reka-ui";
import { useShellStore } from "@/stores/shell";
// MUST be @/theme's useJarvisTheme, the same singleton the header toggle
// writes to. @/composables/useTheme was a separate instance and is deleted.
import { useJarvisTheme } from "@/theme";
// State only, not confirm() itself: this dialog never opens a confirm, it
// just needs to know when one is open (see the disable-outside-click-to-close
// comment on <Dialog> above).
import { confirmState } from "@/composables/useConfirm";

const store = useShellStore();
const { effectiveDark: dark, paletteVars } = useJarvisTheme();

// Panes are lazy: this dialog is mounted eagerly by AppShell for EVERY user, so
// static imports would pull each pane's dependency tree (charts + usageCharts
// for usage, LlmPoolEditor for AI models) into the initial shell bundle — even
// for users who can never open those sections.
const GeneralPane = defineAsyncComponent(() => import("@/components/settings/GeneralPane.vue"));
const UsagePane = defineAsyncComponent(() => import("@/components/settings/UsagePane.vue"));
const ActivityPane = defineAsyncComponent(() => import("@/components/settings/ActivityPane.vue"));
const ShortcutsPane = defineAsyncComponent(() =>
	import("@/components/settings/ShortcutsPane.vue")
);
const PlanBillingPane = defineAsyncComponent(() =>
	import("@/components/settings/PlanBillingPane.vue")
);
const AiModelsPane = defineAsyncComponent(() => import("@/components/settings/AiModelsPane.vue"));
const UsageAdminPane = defineAsyncComponent(() =>
	import("@/components/settings/UsageAdminPane.vue")
);
const BrandingPane = defineAsyncComponent(() => import("@/components/settings/BrandingPane.vue"));
const ConnectorsPane = defineAsyncComponent(() =>
	import("@/components/settings/ConnectorsPane.vue")
);

// ACCOUNT AND BILLING is the tenant-admin tier (System Manager OR Jarvis Admin,
// matching the widened require_jarvis_admin endpoints). ADMINISTRATION is
// is_jarvis_admin, which is true for System Managers too.
const isSM = !!window.is_system_manager;
const isAdmin = !!window.is_jarvis_admin;
// MCP Connectors kill switch (MCP_CONNECTORS_PLAN.md P4/design §2): boot flag
// off site.jarvis.py's connector_flags(), same shape AddConnectorDialog reads
// via list_connectors for allow_custom_urls.
const connectorsEnabled = !!window.connectors_enabled;

const PANES = {
	general: GeneralPane,
	usage: UsagePane,
	activity: ActivityPane,
	shortcuts: ShortcutsPane,
	connectors: ConnectorsPane,
	plan: PlanBillingPane,
	aimodels: AiModelsPane,
	branding: BrandingPane,
	usageadmin: UsageAdminPane,
};

// Rail labels live here; the header title and description each pane shows are
// the pane's own (design.md §4.1). The two differ on purpose in places — the
// rail says "Shortcuts", the pane header says "Keyboard shortcuts".
const NAV = [
	{
		name: "Workspace",
		gate: () => true,
		items: [
			{ key: "general", label: "General", icon: "settings" },
			{ key: "usage", label: "Usage", icon: "bar-chart-2" },
			{ key: "activity", label: "Activity", icon: "activity" },
			{ key: "shortcuts", label: "Shortcuts", icon: "command" },
			// Item-level gate (on top of the group's, which is always true here):
			// the tab is hidden entirely, not shown-and-refused, when the tenant
			// hasn't turned connectors on.
			{
				key: "connectors",
				label: "Connectors",
				icon: "link-2",
				gate: () => connectorsEnabled,
			},
		],
	},
	{
		name: "Account and billing",
		gate: () => isSM || isAdmin,
		items: [
			{ key: "plan", label: "Billing", icon: "credit-card" },
			{ key: "aimodels", label: "AI models", icon: "cpu" },
			{ key: "branding", label: "Branding", icon: "image" },
		],
	},
	{
		name: "Administration",
		gate: () => isAdmin,
		items: [{ key: "usageadmin", label: "User usage", icon: "users" }],
	},
];

// Items may carry their own gate (currently only "connectors") on top of the
// group's — a group stays visible for its ungated siblings even when one item
// in it is hidden.
const visibleGroups = computed(() =>
	NAV.filter((g) => g.gate())
		.map((g) => ({ ...g, items: g.items.filter((i) => !i.gate || i.gate()) }))
		.filter((g) => g.items.length)
);

const open = computed({
	get: () => store.settingsOpen,
	set: (v) => {
		store.settingsOpen = v;
	},
});

const confirmOpen = computed(() => confirmState.value !== null);

// Legacy section key: "billing" used to open the standalone "Billing and
// metering" pane, now folded into Usage. Old deep links and any stale
// store.openSettings("billing") caller still land on Usage instead of
// falling through to the General default. PAIRED with AppShell's
// SETTINGS_DEEP_LINK_KEYS, which must keep "billing" for the ?settings=
// deep link to reach this alias at all: remove an entry here or there only
// together.
const LEGACY_SECTION_ALIASES = { billing: "usage" };

// A gated section requested by a user without the role (group gate) or the
// item's own gate (connectors' kill switch) falls back to General.
const section = computed(() => {
	let s = store.settingsSection;
	if (LEGACY_SECTION_ALIASES[s]) s = LEGACY_SECTION_ALIASES[s];
	if (!PANES[s]) return "general";
	const group = NAV.find((g) => g.items.some((i) => i.key === s));
	if (group) {
		if (!group.gate()) return "general";
		const item = group.items.find((i) => i.key === s);
		if (item && item.gate && !item.gate()) return "general";
	}
	return s;
});
const pane = computed(() => PANES[section.value]);

// True while the active pane is applying a change (currently only AiModelsPane
// during a model apply). AiModelsPane publishes this into the shell store
// itself (watching LlmPoolEditor's busy state, cleared on settle AND from its
// own onUnmounted so it can never stick true) rather than this dialog reading
// it off a template ref: that way the SAME flag also gates the store's
// openSettings(), which is a second, independent writer of settingsSection
// (GeneralPane's "AI models" buttons, UserMenu, ChatView, AppShell's
// ?settings= deep link all go through it, not through go() below). A lock
// enforced only here would leave every one of those callers free to unmount
// AiModelsPane mid-apply (jarvis#821 review).
const applying = computed(() => !!store.settingsApplying);

function go(key) {
	// Locked while an apply is in flight: switching sections would drop the
	// applying pane's scrim and abandon the apply mid-flight. The dialog's own
	// close (X) button is untouched, so a hung apply never traps the user.
	if (applying.value) return;
	store.settingsSection = key;
}
</script>
