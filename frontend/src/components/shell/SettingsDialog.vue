<template>
	<!-- Shell-level settings dialog. Owns the scrim, the grouped rail and the
	     active pane; each pane owns its own header (design.md §4.1), which is
	     why there is no title bar or footer here.

	     frappe-ui's Dialog supplies the modality, focus trap, focus return and
	     Escape handling that this component used to hand-roll. It renders
	     through DialogPortal into <body>, so the stacking order that used to
	     come from .jv-settings-overlay's z-index:60 now comes from the
	     .dialog-overlay rule in main.css. The shell ConfirmDialog stays at 200
	     so a confirm still opens on top of settings. -->
	<Dialog v-model="open" :options="{ size: '5xl' }">
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
			     Panes still on legacy markup (AiModelsPane and the LlmPoolEditor
			     under it, BrandingPane) would otherwise render with every var(--)
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
					class="flex shrink-0 gap-1 overflow-x-auto border-b bg-surface-menu-bar p-1 sm:w-56 sm:flex-col sm:gap-0.5 sm:overflow-y-auto sm:overflow-x-visible sm:rounded-l-lg sm:border-b-0"
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
							class="flex h-7 shrink-0 items-center gap-2 rounded px-2 text-sm text-ink-gray-8"
							:class="
								section === item.key
									? 'bg-surface-white shadow-sm'
									: 'hover:bg-surface-gray-2'
							"
							:aria-current="section === item.key ? 'page' : undefined"
							@click="go(item.key)"
						>
							<FeatherIcon :name="item.icon" class="size-4 shrink-0" />
							<span class="truncate">{{ item.label }}</span>
						</button>
					</template>
				</div>

				<!-- ===== active pane =====
				     flex-col matters: panes not yet migrated (AiModelsPane) still
				     render `.jv-settings-body`, whose `flex:1; overflow-y:auto`
				     only becomes a scroll region inside a flex-column parent. On a
				     plain block wrapper their content would clip silently with no
				     scrollbar. -->
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

const store = useShellStore();
const { effectiveDark: dark, paletteVars } = useJarvisTheme();

// Panes are lazy: this dialog is mounted eagerly by AppShell for EVERY user, so
// static imports would pull each pane's dependency tree (charts + usageCharts
// for billing, LlmPoolEditor for AI models) into the initial shell bundle — even
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
const ConnectionPane = defineAsyncComponent(() =>
	import("@/components/settings/ConnectionPane.vue")
);
const BillingMeteringPane = defineAsyncComponent(() =>
	import("@/components/settings/BillingMeteringPane.vue")
);
const UsageAdminPane = defineAsyncComponent(() =>
	import("@/components/settings/UsageAdminPane.vue")
);
const BrandingPane = defineAsyncComponent(() => import("@/components/settings/BrandingPane.vue"));

// ACCOUNT AND BILLING is the tenant-admin tier (System Manager OR Jarvis Admin,
// matching the widened require_jarvis_admin endpoints). ADMINISTRATION is
// is_jarvis_admin, which is true for System Managers too.
const isSM = !!window.is_system_manager;
const isAdmin = !!window.is_jarvis_admin;

const PANES = {
	general: GeneralPane,
	usage: UsagePane,
	activity: ActivityPane,
	shortcuts: ShortcutsPane,
	plan: PlanBillingPane,
	aimodels: AiModelsPane,
	connection: ConnectionPane,
	billing: BillingMeteringPane,
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
		],
	},
	{
		name: "Account and billing",
		gate: () => isSM || isAdmin,
		items: [
			{ key: "plan", label: "Plan and billing", icon: "credit-card" },
			{ key: "aimodels", label: "AI models", icon: "cpu" },
			{ key: "connection", label: "Connection", icon: "wifi" },
			{ key: "billing", label: "Billing and metering", icon: "dollar-sign" },
			{ key: "branding", label: "Branding", icon: "image" },
		],
	},
	{
		name: "Administration",
		gate: () => isAdmin,
		items: [{ key: "usageadmin", label: "User usage", icon: "users" }],
	},
];

const visibleGroups = computed(() => NAV.filter((g) => g.gate()));

const open = computed({
	get: () => store.settingsOpen,
	set: (v) => {
		store.settingsOpen = v;
	},
});

// A gated section requested by a user without the role falls back to General.
const section = computed(() => {
	const s = store.settingsSection;
	if (!PANES[s]) return "general";
	const group = NAV.find((g) => g.items.some((i) => i.key === s));
	if (group && !group.gate()) return "general";
	return s;
});
const pane = computed(() => PANES[section.value]);

function go(key) {
	store.settingsSection = key;
}
</script>
