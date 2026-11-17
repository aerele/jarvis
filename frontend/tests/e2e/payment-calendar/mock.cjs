// A stateful stand-in for jarvis.payment_calendar.api.* that serves the golden JSON.
// The floor and the account selection decide which golden scenario the next
// request_snapshot returns; every call is logged so a test can assert what an action called.
const { Golden } = require("./golden.cjs");
const { DetailReplies } = require("./detail.cjs");
const { capabilityAccounts } = require("./catalog.cjs");

const PREFIX = "jarvis.payment_calendar.api.";
const COMPANIES = [
  "Northstar Manufacturing Pvt Ltd",
  "Northstar Logistics Pvt Ltd",
  "Northstar Retail Pvt Ltd",
].map((name) => ({ name, default_currency: "INR" }));
const DEFAULT_ACCOUNTS = ["HDFC Current", "Petty Cash"];
const FLOOR_SCENARIOS = {
  140000: "floor_140000",
  147614: "floor_147614",
  150000: "floor_150000",
  200000: "floor_200000",
};
const MONTH_SCENARIOS = { "2026-10": "month_oct", "2026-12": "month_dec" };
const ok = (data) => ({ ok: true, data });
const refusal = (code) => ({
  ok: false,
  state: "unavailable",
  code,
  retryable: false,
});

class CalendarMock {
  // clockStart: the instant the page clock starts (expires_at is set relative to it).
  constructor({
    clockStart,
    golden = new Golden(),
    preferences = DEFAULT_ACCOUNTS,
  } = {}) {
    this.golden = golden;
    this.details = new DetailReplies(golden);
    this.clockStart = clockStart;
    this.floor = { value: "50000.00", revision: 1 };
    this.preferences = {
      company: COMPANIES[0].name,
      accounts: [...preferences],
    };
    this.snapshots = new Map();
    this.jobs = new Map();
    this.log = [];
    this.held = 0;
    this.failNext = new Map();
    this.slowNext = new Map();
    this.statusEdits = [];
    // Saved themes (set_user_theme); the boot code of a reload reads the last one, as the server would.
    this.themeWrites = [];
    this.onUserTheme = null;
  }

  async install(page) {
    await page.route("**/api/method/**", (route) => this.#route(route));
  }

  // The short method names called so far, optionally only those after `mark`.
  calls(mark = 0) {
    return this.log.slice(mark).map((entry) => entry.method);
  }

  count(method, mark = 0) {
    return this.calls(mark).filter((name) => name === method).length;
  }

  mark() {
    return this.log.length;
  }

  // Calls that were received and not answered yet (a delayed reply counts until it is sent).
  pending() {
    return this.held;
  }

  // Make the next call of `method` answer with a refusal code.
  refuseNext(method, code) {
    this.failNext.set(method, code);
  }

  // Hold the next answer of `method` for `ms` (a slow server: used to test late replies).
  delayNext(method, ms) {
    this.slowNext.set(method, ms);
  }

  // Change every status this mock serves from now on (a fixture variant: a negative month, ...).
  editStatus(edit) {
    this.statusEdits.push(edit);
  }

  hasScenario(name) {
    return this.golden.has(name);
  }

  async #route(route) {
    const request = route.request();
    const full = new URL(request.url()).pathname.split("/api/method/")[1] || "";
    const args = JSON.parse(request.postData() || "{}");
    const method = full.split(".").pop();
    if (!full.startsWith(PREFIX))
      return route.fulfill({ json: { message: this.#shell(full, args) } });
    this.log.push({ method, args });
    this.held++;
    try {
      return await this.#reply(route, method, args);
    } finally {
      this.held--;
    }
  }

  async #reply(route, method, args) {
    const refused = this.failNext.get(method);
    this.failNext.delete(method);
    const message = refused ? refusal(refused) : this.#answer(method, args);
    const hold = this.slowNext.get(method);
    this.slowNext.delete(method);
    if (hold) await new Promise((resolve) => setTimeout(resolve, hold));
    return route.fulfill({ json: { message } });
  }

  // Everything outside the calendar API: the empty answers the shell needs.
  #shell(full, args) {
    if (full.endsWith("set_user_theme")) {
      this.themeWrites.push(args.theme);
      this.onUserTheme?.(args.theme);
      return { ok: true };
    }
    if (full.endsWith("is_ready_for_chat")) return { ready: true };
    if (full.includes("count")) return 0;
    if (full.includes("list")) return [];
    return {};
  }

  #answer(method, args) {
    const handler = this[`api_${method}`];
    return handler ? handler.call(this, args) : refusal("not_mocked");
  }

  api_get_capabilities({ company }) {
    if (!company) return ok({ companies: COMPANIES });
    return ok({
      accounts: capabilityAccounts(this.golden),
      settings: this.#floorSettings(company),
      can_configure: true,
    });
  }

  api_get_preferences() {
    return ok(this.preferences);
  }

  api_set_preferences({ company, accounts }) {
    this.preferences = { company, accounts: [...accounts] };
    return ok(this.preferences);
  }

  api_configure_floor({ company, value }) {
    this.floor = {
      value: Number(value).toFixed(2),
      revision: this.floor.revision + 1,
    };
    return ok(this.#floorSettings(company));
  }

  api_request_snapshot({ company, accounts, month }) {
    const id = this.#newId();
    this.snapshots.set(id, { company, accounts: [...accounts], month });
    return ok({ snapshot_id: id, state: "queued", queued_at: this.#iso(0) });
  }

  api_get_snapshot_status({ snapshot_id }) {
    const request = this.snapshots.get(snapshot_id);
    return request
      ? ok(this.#status(snapshot_id, request))
      : refusal("not_found_or_expired");
  }

  api_get_day_detail(args) {
    return this.#detail(args);
  }

  api_get_bucket_detail(args) {
    return this.#detail(args);
  }

  api_get_upcoming({ snapshot_id, date }) {
    return this.snapshots.has(snapshot_id)
      ? ok(this.details.upcoming({ date }))
      : refusal("not_found_or_expired");
  }

  api_request_account_balances({ company }) {
    const id = `balances-${this.jobs.size + 1}`;
    this.jobs.set(id, company);
    return ok({ job_id: id, state: "queued" });
  }

  api_get_account_balances_status({ job_id }) {
    if (!this.jobs.has(job_id)) return refusal("not_found_or_expired");
    const data = this.golden.reply("balances_complete");
    return ok({ ...data, job_id, expires_at: this.#iso(3 * 3600) });
  }

  #detail(args) {
    const request = this.snapshots.get(args.snapshot_id);
    if (!request) return refusal("not_found_or_expired");
    const status = this.#status(args.snapshot_id, request);
    const data = this.details.detail(args, {
      anchor: status.scope.anchor,
      month: status.scope.month,
    });
    return data ? ok(data) : refusal("invalid_request");
  }

  // The golden scenario for a request: no accounts, a floor, or another month.
  scenarioFor({ accounts, month }) {
    if (!accounts.length) return "accounts_none";
    const isDefaultPair =
      DEFAULT_ACCOUNTS.every((name) => accounts.includes(name)) &&
      accounts.length === 2;
    if (!isDefaultPair) return this.#otherSelection(accounts);
    if (MONTH_SCENARIOS[month]) return MONTH_SCENARIOS[month];
    return FLOOR_SCENARIOS[Number(this.floor.value)] || "default";
  }

  #otherSelection(accounts) {
    if (
      accounts.length === 1 &&
      accounts[0] === "HDFC Current" &&
      this.golden.has("accounts_chip_removed")
    )
      return "accounts_chip_removed";
    return accounts.length >= 50 && this.golden.has("accounts_50")
      ? "accounts_50"
      : "default";
  }

  #status(id, request) {
    const status = this.golden.status(this.scenarioFor(request));
    status.snapshot_id = id;
    status.scope.accounts = [...request.accounts].sort();
    status.expires_at = this.#iso(3 * 3600);
    status.floor = { ...status.floor, revision: this.floor.revision };
    this.statusEdits.forEach((edit) => edit(status));
    return status;
  }

  #floorSettings(company) {
    return {
      configured: true,
      company,
      currency: "INR",
      safety_floor: this.floor.value,
      revision: this.floor.revision,
      configuration_id: "northstar-floor",
    };
  }

  #newId() {
    return String(this.snapshots.size + 1).padStart(32, "0");
  }

  #iso(offsetSeconds) {
    return new Date(
      this.clockStart.getTime() + offsetSeconds * 1000
    ).toISOString();
  }
}

module.exports = { CalendarMock, COMPANIES, DEFAULT_ACCOUNTS };
