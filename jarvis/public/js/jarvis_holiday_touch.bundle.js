// The Desk's holiday art: the worn item, its eye glance and the mini chat's particles.
// Lazy by design. This bundle is NOT in hooks.py app_include_js; the widget loads it by
// its own <script> tag, and only when the answer has something to wear (see
// jarvis_chat/widget/holiday_art_loader.mjs and holiday_bundle_guard.test.mjs).
import { installHolidayArt } from "./shared/holiday/art.mjs";

installHolidayArt(window);
