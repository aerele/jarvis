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
		files.value = files.value.concat(added);
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

	// Call once the upload of a `snapshotStaged()` batch settles, with the
	// COUNT of successes. uploadTo reports a count, not which files made it,
	// and Helpdesk's media.upload has no un-attach to undo a partial batch —
	// so guessing which File to drop would be worse than doing nothing. Only
	// clear/revoke when EVERY staged file uploaded; on any shortfall, leave
	// ALL staged files in place so the user can just hit Send/Create again
	// instead of re-picking from disk. Removing by reference (never a blanket
	// `files.value = []`) means a file attached WHILE the send was in flight —
	// never in `staged` — survives either branch untouched, and its preview
	// is never revoked out from under it.
	function settleUpload(staged, uploadedCount) {
		if (uploadedCount === staged.length) {
			files.value = files.value.filter((f) => !staged.includes(f));
			staged.forEach(releasePreview);
		}
	}

	onUnmounted(() => files.value.forEach(releasePreview));

	return { files, pending, onFiles, removeFile, snapshotStaged, settleUpload };
}
