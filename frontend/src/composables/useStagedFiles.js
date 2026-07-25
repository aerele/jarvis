// The staged-file / object-URL lifecycle shared by the two support submit
// pages (SupportThreadPage's reply composer, SupportNewPage's create
// composer) — extracted after it drifted once between two verbatim copies
// (I4). Files are held as local File objects and uploaded on submit, never
// before: Helpdesk's media.upload needs an existing ticket and has no
// un-attach endpoint, unlike chat's Composer, which uploads eagerly on
// attach.
//
// The two submit paths themselves stay separate (thread stays on the page;
// new-ticket navigates away on success) — only this file lifecycle is
// shared.
import { computed, onUnmounted, ref } from "vue";
import { toast } from "frappe-ui";

const MAX_ATTACH_BYTES = 25 * 1024 * 1024; // matches the server cap (media.py _MAX_BYTES)
function tooLarge(f) {
	return typeof f.size === "number" && f.size > MAX_ATTACH_BYTES;
}
// Reject oversize files with an immediate message instead of shipping ~33MB of
// base64 across two hops before the server's late "file too large". Files with no
// numeric size (synthetic/jsdom) pass. Shared by EVERY attach surface — both
// support pages' Attach button, paste/drop, and the editor slash command — so the
// cap can't be applied to one entry point and forgotten on another.
export function withinSize(list) {
	const arr = Array.from(list || []);
	const over = arr.filter(tooLarge);
	if (over.length) {
		toast.error(
			over.length === 1
				? `"${over[0].name}" is larger than 25 MB and wasn't attached.`
				: `${over.length} files exceed 25 MB and weren't attached.`
		);
	}
	return arr.filter((f) => !tooLarge(f));
}

export function useStagedFiles() {
	const files = ref([]); // local File objects — uploaded on submit, never before

	// Composer takes DISPLAY objects, never File objects. Object URLs are
	// created once per file and revoked on removal/submit/unmount so a long
	// session can't leak them.
	const previews = new Map();
	function previewFor(f) {
		if (!previews.has(f)) {
			previews.set(f, /^image\//.test(f.type) ? URL.createObjectURL(f) : "");
		}
		return previews.get(f);
	}
	function releasePreview(f) {
		const url = previews.get(f);
		if (url) URL.revokeObjectURL(url);
		previews.delete(f);
	}

	const pending = computed(() =>
		files.value.map((f, i) => ({
			key: `${f.name}-${i}`,
			file_name: f.name,
			preview_url: previewFor(f),
			removable: true,
		}))
	);

	function onFiles(added) {
		// Array.from so a raw FileList is spread element-by-element (concat would
		// append the whole list as ONE nameless entry).
		files.value = files.value.concat(Array.from(added || []));
	}
	function removeFile(i) {
		const f = files.value[i];
		if (f) releasePreview(f);
		files.value = files.value.filter((_, n) => n !== i);
	}

	// Call at the START of the host's submit function, BEFORE the awaited
	// reply/createTicket — a file the user attaches while that call (or the
	// upload below) is in flight must never land in this batch, so it has to
	// be excluded from the very first await onward, not just from the upload.
	function snapshotStaged() {
		return files.value.slice();
	}

	// Call once the upload of a `snapshotStaged()` batch settles, with the array
	// of File REFERENCES that actually succeeded — not a count. uploadTo
	// iterates per-file with its own try/catch, so it always knows exactly which
	// ones landed; Helpdesk's media.upload has no un-attach endpoint, so
	// re-uploading a File that already succeeded on a retry would create a
	// PERMANENT duplicate attachment. Remove-and-revoke exactly the succeeded
	// files by reference, leaving any failures still staged for the next
	// Send/Create. A file attached WHILE the send was in flight — never in
	// `uploaded` — survives untouched either way, and its preview is never
	// revoked out from under it.
	function settleUpload(uploaded) {
		const succeeded = new Set(uploaded || []);
		files.value = files.value.filter((f) => !succeeded.has(f));
		(uploaded || []).forEach(releasePreview);
	}

	// Composer state is per-TICKET, not per-page: switching to a different
	// ticket must not carry a draft or staged files over to it (see
	// SupportThreadPage's open()) — otherwise text/files meant for the ticket
	// just left can end up posted to the new one on the next Send.
	function reset() {
		files.value.forEach(releasePreview);
		files.value = [];
	}

	onUnmounted(() => files.value.forEach(releasePreview));

	return { files, pending, onFiles, removeFile, snapshotStaged, settleUpload, reset };
}
