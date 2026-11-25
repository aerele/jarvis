// Side-effect module: the PWA's X-Jarvis-Client shim (the code is shared with the desktop SPA
// through @shared). It must be the FIRST import of main.js, so no module can fire an /api/
// request before the patch is in.
import { install } from "@shared/lib/clientHeader";

install("pwa");
