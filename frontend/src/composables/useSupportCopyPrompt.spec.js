import { describe, it, expect } from "vitest";
import {
	copyPromptState,
	promptSupportCopy,
	settleSupportCopyPrompt,
} from "./useSupportCopyPrompt.js";

/**
 * Covers the module-scope resolver singleton directly (review finding: an
 * unmount before settle used to leave `_resolve` set forever, silently
 * dropping every future prompt to "no" for the rest of the session - see
 * openSupport()'s AppShell-mount fix in ChatView.vue). settle() is the one
 * escape hatch that clears `_resolve`, so these tests exercise it the way
 * SupportCopyPromptDialog's Yes/No/Don't-ask buttons and Escape key do.
 */

describe("useSupportCopyPrompt", () => {
	it("opens with the given preview and resolves with the settled value", async () => {
		const p = promptSupportCopy({ preview: "You: hi\n\nJarvis: hello" });
		expect(copyPromptState.value).toEqual({ preview: "You: hi\n\nJarvis: hello" });
		settleSupportCopyPrompt("yes");
		expect(await p).toBe("yes");
		// Settling clears the dialog's own state too, not just the resolver.
		expect(copyPromptState.value).toBeNull();
	});

	it("defaults preview to an empty string when none is given", () => {
		promptSupportCopy();
		expect(copyPromptState.value).toEqual({ preview: "" });
		settleSupportCopyPrompt("no");
	});

	it("a concurrent second call resolves 'no' immediately without touching the open prompt", async () => {
		const first = promptSupportCopy({ preview: "first" });
		const second = promptSupportCopy({ preview: "second" });
		// The second call must not have replaced the dialog's preview.
		expect(copyPromptState.value).toEqual({ preview: "first" });
		expect(await second).toBe("no");
		settleSupportCopyPrompt("dontask");
		expect(await first).toBe("dontask");
	});

	it("settling with no prompt open is a harmless no-op", () => {
		expect(copyPromptState.value).toBeNull();
		expect(() => settleSupportCopyPrompt("no")).not.toThrow();
		expect(copyPromptState.value).toBeNull();
	});

	it("survives an abandoned prompt: a bare settle (simulating unmount) frees the next prompt", async () => {
		// This is the regression itself: SupportCopyPromptDialog unmounting
		// without the user answering (e.g. a route change) must still call
		// settle() so `_resolve` doesn't wedge every prompt after it. Nothing
		// in this composable does that automatically - it's the mounting
		// component's job (onBeforeUnmount) - so this test documents the
		// contract: whoever tears the dialog down MUST settle first.
		const abandoned = promptSupportCopy({ preview: "abandoned" });
		settleSupportCopyPrompt("no"); // stands in for the dialog's teardown settle
		await abandoned;

		const next = promptSupportCopy({ preview: "next" });
		expect(copyPromptState.value).toEqual({ preview: "next" });
		settleSupportCopyPrompt("yes");
		expect(await next).toBe("yes");
	});
});
