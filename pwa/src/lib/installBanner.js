import { ref } from "vue";

// InstallBanner's visibility, lifted out of its own private `show` computed
// (Slice 3b) so App.vue can gate the release-nudge banner on it: only one
// top-of-app strip shows at a time - the install offer and the soft update
// nudge must never stack in the same slot. InstallBanner keeps owning the real
// dismissed/isIos/insecure state that decides `show`; this ref is just that
// derived boolean, kept in sync by a watchEffect over there.
export const installBannerVisible = ref(false);
