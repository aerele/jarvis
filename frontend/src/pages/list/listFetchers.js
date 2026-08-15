// The EXACT `fetchFn` each migrated list page mounts — the full
// `wrapper(adapter(p))` transport — defined ONCE here so the surface-transport
// spec (S6 / D1) can drive the SAME function object the page passes to
// useListPage, not a reconstruction of it.
//
// The blocker this closes: the spec used to rebuild the chain inline
// (`wrapper(adapt(p))`), so a page that dropped `filters_v2` in its own mapping
// — `adapter({ ...p, filters_v2: undefined })`, the exact "claims-filtering-but-
// doesn't" defect — left the whole suite green. With the page and the spec both
// bound to the fetcher below, neutering `filters_v2` here (the only place a page's
// transport now lives) turns the spec red.

import * as api from "@/api";
import { listDashboardsPage } from "@/api/dashboards";
import { listTriggersPage } from "@/api/triggers";
import { listWikiPagesPage } from "@/api/wiki";

import { searchSplitArgs, buildWikiListArgs } from "./listAdapters";

export const skillsListFetch = (p) => api.listCustomSkillsPage(searchSplitArgs(p));
export const macrosListFetch = (p) => api.listMacrosPage(searchSplitArgs(p));
export const dashboardsListFetch = (p) => listDashboardsPage(searchSplitArgs(p));
export const triggersListFetch = (p) => listTriggersPage(searchSplitArgs(p));
export const wikiListFetch = (p) => listWikiPagesPage(buildWikiListArgs(p));
