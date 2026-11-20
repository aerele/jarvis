// The draft edit form's link search: one shared menu for the panel's fields and grid cells,
// with the generation counter that drops a stale answer or a blur timer from an older search.
// The desktop panel and the phone sheet both call this, so the two cannot drift. The search
// call is injected (`searchLink`), as is the refresh of the rules' hidden values after a pick
// (`fillRuleValues`); `draftPanel` is the ref of the draft model the form edits.
import { onBeforeUnmount, ref, watch } from "vue";
import {
	draftLinkSearch,
	draftLinkContext,
	DraftLinkFilterError,
	DraftLinkTargetError,
} from "./draftLinkFilters";

export function useDraftLink({ draftPanel, searchLink, fillRuleValues }) {
	// one shared link-search menu for panel inputs, keyed "f:<fieldname>" or "t:<ti>:<row __key>:<col>"
	const draftLink = ref({ key: "", items: [], open: false, anchor: null });
	// The shared popup belongs to one search, not merely one field key. A blur
	// timer or response from an older search must not mutate the current popup.
	let draftLinkGeneration = 0;
	onBeforeUnmount(() => {
		draftLinkGeneration++;
	});
	const draftLinkError = ref("");
	// Company / row dependency changes invalidate suggestions even if a request is
	// still pending or its field remains focused. Existing field values stay intact.
	watch(
		() => draftLinkContext(draftPanel.value, draftLink.value.key),
		(context) => {
			if (draftLink.value.filterContext && context !== draftLink.value.filterContext)
				draftLink.value = { ...draftLink.value, open: false, items: [] };
		},
		{ flush: "sync" }
	);

	// Link search shared by panel fields + grid cells.
	async function onDraftLink(key, target, doctype, ev) {
		const generation = ++draftLinkGeneration;
		const anchor = ev && ev.target;
		draftLink.value = { key, items: [], open: true, anchor };
		const search = draftLink.value;
		draftLinkError.value = "";
		if (!doctype) {
			// A Dynamic Link whose controlling field is still empty: say which field to set.
			// Any other link without a DocType stays silent, as before.
			try {
				draftLinkSearch(draftPanel.value, key);
			} catch (e) {
				if (e instanceof DraftLinkTargetError) {
					draftLink.value = { ...draftLink.value, open: false, items: [] };
					draftLinkError.value = e.message;
				}
			}
			return;
		}
		try {
			const args = draftLinkSearch(draftPanel.value, key);
			const filterContext = JSON.stringify(args);
			draftLink.value.filterContext = filterContext;
			const r = await searchLink(
				doctype,
				target(),
				8,
				args.referenceDoctype,
				args.linkFieldname,
				args.filters
			);
			if (
				generation !== draftLinkGeneration ||
				draftLink.value !== search ||
				draftLinkContext(draftPanel.value, key) !== filterContext
			)
				return;
			draftLink.value = {
				key,
				items: (r || [])
					.map((x) => ({ value: x.value, label: x.description || "" }))
					.slice(0, 8),
				open: true,
				anchor,
				filterContext,
			};
		} catch (e) {
			if (generation === draftLinkGeneration && draftLink.value === search) {
				draftLink.value = { ...draftLink.value, open: false, items: [] };
				draftLinkError.value =
					e instanceof DraftLinkFilterError
						? e.message
						: "Could not load this field's choices. Try again or open the document in ERPNext.";
			}
		}
	}
	function pickDraftLink(setter, item) {
		draftLinkGeneration++;
		if (
			draftLink.value.filterContext !==
			draftLinkContext(draftPanel.value, draftLink.value.key)
		) {
			draftLink.value = { ...draftLink.value, open: false, items: [] };
			return;
		}
		setter(item.value);
		draftLink.value = { key: "", items: [], open: false, anchor: null };
		// A row's new link (an item) can change what its rules read: fetch it, as Desk does.
		const panel = draftPanel.value;
		if (panel) fillRuleValues(panel.doctype, panel.tables, () => draftPanel.value === panel);
	}
	function closeDraftLink(ev) {
		const generation = draftLinkGeneration;
		const anchor = ev?.target || draftLink.value.anchor;
		setTimeout(() => {
			if (generation !== draftLinkGeneration || document.activeElement === anchor) return;
			draftLinkGeneration++; // a pending response may not reopen a blurred field
			draftLink.value = { ...draftLink.value, open: false };
		}, 160);
	}

	// The panel closed or its chat changed: nothing of the old search may survive.
	function resetDraftLink() {
		draftLinkGeneration++;
		draftLinkError.value = "";
		draftLink.value = { key: "", items: [], open: false, anchor: null };
	}

	return {
		draftLink,
		draftLinkError,
		linkError: draftLinkError,
		onDraftLink,
		pickDraftLink,
		closeDraftLink,
		resetDraftLink,
	};
}
