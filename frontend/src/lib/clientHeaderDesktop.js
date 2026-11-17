// Side-effect module: the desktop SPA's X-Jarvis-Client shim. It must be the FIRST import of
// main.js (ES imports evaluate in order), so no module can fire an /api/ request before the
// patch is in. The PWA has the same file with "pwa" (pwa/src/lib/clientHeaderPwa.js).
import { install } from "./clientHeader";

install("desktop");
