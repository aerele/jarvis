import { test } from "node:test";
import assert from "node:assert/strict";
import fs from "node:fs";
import path from "node:path";
import { fileURLToPath } from "node:url";

const HERE = path.dirname(fileURLToPath(import.meta.url));
const read = (...parts) => fs.readFileSync(path.join(HERE, ...parts), "utf8");
const pane = read("..", "pages", "dashboards", "DashboardChatPane.vue");
const page = read("..", "pages", "dashboards", "DashboardsPage.vue");
const view = read("..", "pages", "dashboards", "DashboardView.vue");
const approvals = read("..", "pages", "approvals", "ApprovalsBoard.vue");
const dashboardsApi = read("..", "api", "dashboards.js");

test("dashboard surfaces never route their native thread into general chat", () => {
	assert.doesNotMatch(pane, /Open in chat/);
	assert.doesNotMatch(pane, /router\.push\(['"]\/c\//);
	assert.doesNotMatch(view, /Discuss in chat|setChatPrefill/);
	assert.doesNotMatch(page, /label: "Open its chat"|router\.push\("\/c\//);
	assert.match(pane, /label="Hide chat"/);
	assert.match(page, /v-if="!chatOpen"[\s\S]*?label="Show chat"/);
});

test("the builder exposes only dashboard-scoped conversation history", () => {
	assert.match(dashboardsApi, /list_dashboard_conversations/);
	assert.match(pane, /label="Dashboard chats"/);
	assert.match(pane, /listDashboardConversations\(\)/);
	assert.match(page, /selectDashboardConversation/);
	assert.match(page, /query\.conversation/);
	assert.match(page, /id === chatConv\.value[\s\S]*?chatOpen\.value = true/);
});

test("approval hand-offs route dashboard conversations back to the builder", () => {
	assert.match(approvals, /originPage === "dashboards"/);
	assert.match(approvals, /name: "DashboardsPage", query: \{ conversation \}/);
	assert.match(approvals, /Answer in builder/);
});

test("dashboard chat sends model and effort and wears main chat's context ring", () => {
	assert.match(pane, /<ModelEffortPicker/);
	// The pane is narrow and overflow-hidden: the shared picker must use its
	// start-aligned menu and inline (compact) effort flyout, or both clip.
	assert.match(pane, /<ModelEffortPicker[\s\S]*?\balign="start"[\s\S]*?\bcompact\b[\s\S]*?\/>/);
	// Send stays pinned at the row's end; the controls wrap on their own.
	assert.match(pane, /class="flex min-w-0 flex-1 flex-wrap items-center gap-1\.5"/);
	// The SAME ContextRing + CompactDialog main chat uses (#1136), fed by the
	// same get_conversation_context payload: no dashboard-only meter, and the
	// ring sits LEFT of the model pill exactly as in main chat's composer.
	assert.match(pane, /import ContextRing from "@\/components\/chat\/ContextRing\.vue"/);
	assert.match(
		pane,
		/<ContextRing[\s\S]*?:context="contextInfo"[\s\S]*?@compact="openCompactDialog\(\)"/
	);
	assert.match(pane, /<CompactDialog[\s\S]*?@confirm="runCompact"/);
	assert.match(pane, /getConversationContext\(id\)/);
	assert.doesNotMatch(pane, /ContextUsagePill|contextUsage\b/);
	assert.ok(
		pane.indexOf("<ContextRing") < pane.indexOf("<ModelEffortPicker"),
		"ring before model pill"
	);
	assert.match(dashboardsApi, /model_override: modelOverride/);
	assert.match(dashboardsApi, /thinking_override: thinkingOverride/);
});

test("the builder frees the window: auto-collapses the rail and swaps to single-pane on mobile", () => {
	// The page asks the shell to collapse the left rail on enter and releases it
	// on leave (stores/shell.setSpaciousView restores the saved preference).
	assert.match(page, /shell\.setSpaciousView\(true\)/);
	assert.match(page, /onBeforeUnmount\(\(\) => shell\.setSpaciousView\(false\)\)/);
	// Below the phone breakpoint the side-by-side split can't hold two usable
	// columns, so the canvas and chat swap full-width instead of squeezing.
	assert.match(page, /const isMobile = computed\(\(\) => shell\.mobile\)/);
	assert.match(page, /v-show="!isMobile \|\| !chatOpen"/); // canvas hidden while chat is open on mobile
	assert.match(page, /v-show="chatOpen && !isMobile"/); // the drag divider is desktop-only
	assert.match(page, /isMobile \? 'w-full' : 'shrink-0 border-l'/); // chat pane takes the full width on a phone
});
