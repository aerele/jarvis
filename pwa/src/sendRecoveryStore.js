import { reactive } from "vue";
import { createSendRecovery } from "./lib/sendRecovery";

// Tab memory only. No business data in persistent browser storage. A full page
// logout/reload clears it. Key each draft/request to its originating chat.
export const recoveryState = reactive({ requests: [], drafts: {} });
export const sendRecovery = createSendRecovery(recoveryState);
