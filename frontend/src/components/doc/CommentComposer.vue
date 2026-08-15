<template>
	<div class="rounded-lg border p-2" @keydown="onKeydown">
		<TextEditor
			:content="content"
			:editor-class="'prose-sm max-w-none min-h-[4rem]'"
			:extensions="editorExtensions"
			:autofocus="autofocus"
			:bubble-menu="true"
			placeholder="Add a comment… @ to mention"
			@change="(v) => (html = v)"
		/>
		<div class="mt-2 flex items-center justify-end gap-2">
			<Button variant="ghost" label="Discard" @click="$emit('discard')" />
			<Button
				variant="solid"
				:label="submitLabel"
				:loading="loading"
				:disabled="isEmpty"
				@click="submit"
			/>
		</div>
	</div>
</template>

<script>
// Mention plumbing lives at module level, shared by every composer instance
// on the page. We deliberately do NOT use TextEditor's :mentions prop:
// frappe-ui 0.1.278's suggestion renderer is incompatible with
// @tiptap/suggestion 3.x - onStart now fires with empty items BEFORE the
// async items() fetch, the empty SuggestionList (v-if="items.length")
// renders a comment node so VueRenderer.element is null, tippy creation is
// skipped, and onUpdate only repositions an existing popup. Net effect: the
// mention popup never renders, no matter what data :mentions holds. Instead
// we register the official @tiptap/extension-mention (a frappe-ui
// dependency, same "mention" node schema and span.mention[data-type=
// "mention"] HTML shape) with our own renderer built on the modern
// async-items + props.mount() lifecycle.
import { Mention } from "@tiptap/extension-mention";
import { listShareableUsers } from "@/api";

// One shareable-users fetch per page load, shared across composers. items()
// awaits it, so a "@" typed before the fetch resolves still gets the list.
let _usersPromise = null;
function shareableUsers() {
	if (!_usersPromise) {
		_usersPromise = listShareableUsers().then((rows) =>
			(rows || []).map((u) => ({ id: u.name, label: u.full_name || u.name }))
		);
	}
	return _usersPromise.catch(() => {
		_usersPromise = null; // mentions are best-effort; retry on the next query
		return [];
	});
}

async function mentionItems({ query }) {
	const users = await shareableUsers();
	const q = (query || "").toLowerCase();
	return users.filter((u) => u.label.toLowerCase().startsWith(q)).slice(0, 10);
}

// Plain-DOM suggestion popup (classes mirror frappe-ui's SuggestionList).
// props.mount() handles portaling to <body>, floating-ui positioning and
// outside-click dismissal; Escape is handled by the suggestion plugin.
function mentionRender() {
	let el = null;
	let unmount = null;
	let selected = 0;
	let current = { items: [], command: () => {} };

	function draw() {
		if (!el) return;
		el.replaceChildren();
		el.style.display = current.items.length ? "" : "none";
		current.items.forEach((item, i) => {
			const btn = document.createElement("button");
			btn.type = "button";
			btn.className =
				"flex w-full items-center whitespace-nowrap rounded-md px-2 py-1.5 text-sm text-ink-gray-9" +
				(i === selected ? " bg-surface-gray-2" : "");
			btn.textContent = item.label;
			btn.addEventListener("mousedown", (e) => e.preventDefault()); // keep editor focus
			btn.addEventListener("click", () => current.command(item));
			btn.addEventListener("mouseover", () => {
				if (selected !== i) {
					selected = i;
					draw();
				}
			});
			el.appendChild(btn);
		});
	}

	return {
		onStart(props) {
			current = props;
			selected = 0;
			el = document.createElement("div");
			el.className =
				"max-h-[300px] min-w-40 overflow-y-auto rounded-lg bg-surface-white p-1 shadow-lg";
			el.style.zIndex = "9999";
			el.dataset.mentionPopup = "";
			draw();
			unmount = props.mount(el);
		},
		onUpdate(props) {
			current = props;
			if (selected >= props.items.length) selected = 0;
			draw();
		},
		onKeyDown({ event }) {
			if (!el || !current.items.length) return false;
			if (event.key === "ArrowDown") {
				selected = (selected + 1) % current.items.length;
				draw();
				return true;
			}
			if (event.key === "ArrowUp") {
				selected = (selected + current.items.length - 1) % current.items.length;
				draw();
				return true;
			}
			if (event.key === "Enter") {
				current.command(current.items[selected]);
				return true;
			}
			return false;
		},
		onExit() {
			if (unmount) unmount();
			unmount = null;
			el = null;
		},
	};
}

const CommentMention = Mention.configure({
	// class "mention" = frappe-ui chip styling + round-trips its
	// span.mention[data-type="mention"] parse rule
	HTMLAttributes: { class: "mention" },
	suggestion: { char: "@", items: mentionItems, render: mentionRender },
});
</script>

<script setup>
// CommentComposer - async-loaded comment editor (DESIGN-V3 §6.1, D33): the
// frappe-ui TextEditor (tiptap + prosemirror) stays isolated in THIS chunk so
// list/detail pages don't pay for it until the user actually comments; editor
// styles are imported here only. Ctrl/⌘+Enter submits. Mention rendering
// only - no mention notifications this wave (stated non-goal).
import "frappe-ui/editor-style.css";
import { ref, computed, onMounted } from "vue";
import { TextEditor, Button } from "frappe-ui";

const props = defineProps({
	content: { type: String, default: "" }, // initial HTML (edit mode)
	submitLabel: { type: String, default: "Comment" },
	loading: { type: Boolean, default: false },
	autofocus: { type: Boolean, default: false },
});

const emit = defineEmits(["submit", "discard"]);

const html = ref(props.content || "");

const editorExtensions = [CommentMention];

onMounted(() => {
	shareableUsers(); // warm the cache before the first "@"
});

const isEmpty = computed(() => {
	const raw = html.value || "";
	return !raw.replace(/<[^>]*>/g, "").trim() && !/<img\b/i.test(raw);
});

function submit() {
	if (isEmpty.value || props.loading) return;
	emit("submit", html.value);
}

function onKeydown(e) {
	if (e.key === "Enter" && (e.metaKey || e.ctrlKey)) {
		e.preventDefault();
		submit();
	}
}
</script>
