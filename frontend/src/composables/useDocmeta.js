// useDocmeta - one docmeta bundle per doc page (DESIGN-V3 §6.1). The parent
// page creates it and passes the SAME object to DocMetaPanel + CommentsSection.
// Mutations call src/api/docmeta.js then patch `meta` locally (comments
// append/replace; assignees/likes/shares replace from the response) - no full
// reload per action. Mutation errors → toast; the initial-load error is kept
// in `error` for the page's not-found state.
//
// Returned as reactive() (house style, like stores/shell.js) so consumers read
// plain properties: `docmeta.meta.comments`, `docmeta.loading`.
import { reactive, ref, isRef, watch } from "vue";
import { toast } from "frappe-ui";
import { session } from "@/data/session";
import * as apiDocmeta from "@/api/docmeta";
import { errMessage as errMsg } from "@/lib/errors";

export function useDocmeta(doctype, name) {
	// plain string (B4/B5 contract) or a ref (detail pages whose route param
	// can change in-place) both work.
	const nameRef = isRef(name) ? name : ref(name);

	const meta = ref(null); // get_docmeta bundle (§8.1 + §14 F1 shares)
	const loading = ref(false);
	const error = ref("");

	async function reload() {
		if (!nameRef.value) return;
		loading.value = true;
		try {
			meta.value = (await apiDocmeta.getDocmeta(doctype, nameRef.value)) || null;
			error.value = "";
		} catch (e) {
			error.value = errMsg(e);
		} finally {
			loading.value = false;
		}
	}

	// ── comments ────────────────────────────────────────────────────────────────
	async function addComment(html) {
		try {
			const row = await apiDocmeta.addComment(doctype, nameRef.value, html);
			if (meta.value && row) meta.value.comments = [...(meta.value.comments || []), row];
			return row || null;
		} catch (e) {
			toast.error(errMsg(e));
			return null;
		}
	}

	async function updateComment(id, html) {
		try {
			const row = await apiDocmeta.updateComment(id, html);
			if (meta.value && row) {
				meta.value.comments = (meta.value.comments || []).map((c) =>
					c.name === id ? { ...c, ...row } : c
				);
			}
			return true;
		} catch (e) {
			toast.error(errMsg(e));
			return false;
		}
	}

	async function deleteComment(id) {
		try {
			await apiDocmeta.deleteComment(id);
			if (meta.value)
				meta.value.comments = (meta.value.comments || []).filter((c) => c.name !== id);
			return true;
		} catch (e) {
			toast.error(errMsg(e));
			return false;
		}
	}

	// ── like (optimistic flip; server list is authoritative) ────────────────────
	async function toggleLike() {
		if (!meta.value) return;
		const next = meta.value.liked ? 0 : 1;
		meta.value.liked = !!next;
		try {
			const likedBy = (await apiDocmeta.toggleLike(doctype, nameRef.value, next)) || [];
			meta.value.liked_by = likedBy;
			meta.value.liked = likedBy.includes(session.user);
		} catch (e) {
			meta.value.liked = !next;
			toast.error(errMsg(e));
		}
	}

	// ── assignees (response replaces the list) ──────────────────────────────────
	async function assign(user) {
		try {
			const assignees = await apiDocmeta.toggleAssignment(
				doctype,
				nameRef.value,
				user,
				"add"
			);
			if (meta.value && Array.isArray(assignees)) meta.value.assignees = assignees;
			return true;
		} catch (e) {
			toast.error(errMsg(e));
			return false;
		}
	}

	async function unassign(user) {
		try {
			const assignees = await apiDocmeta.toggleAssignment(
				doctype,
				nameRef.value,
				user,
				"remove"
			);
			if (meta.value && Array.isArray(assignees)) meta.value.assignees = assignees;
			return true;
		} catch (e) {
			toast.error(errMsg(e));
			return false;
		}
	}

	// ── shares (§14 F1) ─────────────────────────────────────────────────────────
	async function toggleShare(user, action = "add") {
		try {
			const res = await apiDocmeta.toggleShare(doctype, nameRef.value, user, action);
			if (Array.isArray(res)) {
				if (meta.value) meta.value.shares = res;
			} else {
				await reload(); // defensive: no list in the response → refetch
			}
			return true;
		} catch (e) {
			toast.error(errMsg(e));
			return false;
		}
	}

	// ── attachments ─────────────────────────────────────────────────────────────
	async function deleteAttachment(fileName) {
		try {
			await apiDocmeta.deleteAttachment(doctype, nameRef.value, fileName);
			if (meta.value) {
				meta.value.attachments = (meta.value.attachments || []).filter(
					(f) => f.name !== fileName
				);
			}
			return true;
		} catch (e) {
			toast.error(errMsg(e));
			return false;
		}
	}

	// FileUploader's success payload is the created File doc - patch locally;
	// callers without the payload get a plain reload.
	function afterUpload(fileDoc) {
		if (meta.value && fileDoc && fileDoc.name) {
			meta.value.attachments = [
				...(meta.value.attachments || []),
				{
					name: fileDoc.name,
					file_name: fileDoc.file_name,
					file_url: fileDoc.file_url,
					file_size: fileDoc.file_size,
					is_private: fileDoc.is_private,
					creation: fileDoc.creation,
					owner: fileDoc.owner,
				},
			];
		} else {
			reload();
		}
	}

	watch(nameRef, (v, old) => {
		if (v !== old) {
			meta.value = null;
			reload();
		}
	});
	reload();

	return reactive({
		doctype,
		name: nameRef,
		meta,
		loading,
		error,
		reload,
		addComment,
		updateComment,
		deleteComment,
		toggleLike,
		assign,
		unassign,
		toggleShare,
		deleteAttachment,
		afterUpload,
	});
}
