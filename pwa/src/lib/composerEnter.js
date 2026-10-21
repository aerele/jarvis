// Whether a keydown in the composer should send the message.
//
// Enter sends on a physical keyboard; on a phone the on-screen Return key should
// insert a newline, so only intercept when there is no soft keyboard.
//
// An Enter that CONFIRMS an IME candidate (Tamil, Hindi, Japanese, ...) belongs
// to the composition, not to us. keyCode 229 is the pre-`isComposing` browsers'
// version of the same signal, and Safari fires compositionend before the
// confirming keydown (isComposing already false, keyCode still 229). Same
// guard as the desktop composer.
const MOBILE_UA = /Mobi|Android/i;

export function isImeKey(e) {
	return Boolean(e.isComposing) || e.keyCode === 229;
}

export function enterSends(e, userAgent) {
	if (isImeKey(e)) return false;
	return e.key === "Enter" && !e.shiftKey && !MOBILE_UA.test(userAgent || "");
}
