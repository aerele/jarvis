// The user's own "Suggest follow-up replies" switch (Jarvis User Settings.suggest_replies,
// default on), as seen by this page. The server is the truth: it stops generating them
// and leaves stored ones out of the chat history for a user who turned it off. This ref
// only makes the change show at once in the chat already on screen: the settings switch
// sets it after a successful save, and the chat (desktop and PWA, through @shared)
// hides the chips while it is off.
import { ref } from "vue";

export const suggestRepliesOn = ref(true);
