// Which surface a shared draft component is drawn on: "desktop" (the default) or "phone". The
// edit form provides it under this key to everything below it, so the teleported link menu (which
// sits on <body>, outside the form's markup and classes) can follow it without a prop at every
// level. A component with no provider is on the desktop. Relative imports only.
export const DRAFT_SURFACE = "jvDraftSurface";
// The component a host draws Date / Datetime / Time fields with, provided under this key. The
// desktop provides its frappe-ui picker (DateControl); with no provider (the phone) the shared
// draft components keep the browser's own date, datetime-local and time inputs.
export const DRAFT_DATE_CONTROL = "jvDraftDateControl";
