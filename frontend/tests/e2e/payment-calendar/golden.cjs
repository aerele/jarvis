// Reads the golden JSON written by PR 1's Python test (never edited here).
// Status files are { scenario, status }; reply files are { scenario, request, data }.
const fs = require("node:fs");
const path = require("node:path");

const DIR = path.join(__dirname, "../../payment-calendar/golden");

class Golden {
  constructor(dir = DIR) {
    this.statuses = {};
    this.replies = {};
    for (const file of fs
      .readdirSync(dir)
      .filter((name) => name.endsWith(".json"))) {
      const json = JSON.parse(fs.readFileSync(path.join(dir, file), "utf8"));
      if (Object.hasOwn(json, "status"))
        this.statuses[json.scenario] = json.status;
      else
        this.replies[json.scenario] = {
          request: json.request,
          data: json.data,
        };
    }
  }

  has(scenario) {
    return Object.hasOwn(this.statuses, scenario);
  }

  // A deep copy, so a test mock can rewrite ids and dates without touching the source.
  status(scenario) {
    if (!this.has(scenario))
      throw new Error(`Unknown golden scenario: ${scenario}`);
    return structuredClone(this.statuses[scenario]);
  }

  replyList() {
    return Object.values(this.replies).map((item) => structuredClone(item));
  }

  reply(scenario) {
    return structuredClone(this.replies[scenario]?.data);
  }
}

module.exports = { Golden };
