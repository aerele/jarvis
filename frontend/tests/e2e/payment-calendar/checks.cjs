// One named check = one soft assertion inside a test step, so a failing check is reported by
// name and the flow keeps going. A check polls (real time) because the page updates after
// requests resolve; `pass` may be a boolean or { pass, detail } (detail goes into the report).
const { test, expect } = require("@playwright/test");

const { evaluate } = require("./evaluate.cjs");

const POLL_MS = 5000;

// read: () => boolean | { pass, detail } | Promise of either. Polls until it passes.
async function check(name, read, { timeout = POLL_MS } = {}) {
  await test.step(name, async () => {
    const deadline = Date.now() + timeout;
    let result = await evaluate(read);
    while (!result.pass && Date.now() < deadline) {
      await new Promise((resolve) => setTimeout(resolve, 150));
      result = await evaluate(read);
    }
    expect.soft(result.pass, `${name} ${result.detail}`.trim()).toBe(true);
  });
}

// Result builders: the detail shows expected and actual in the failure report.
const equal = (actual, expected) => ({
  pass: actual === expected,
  detail: `(expected ${JSON.stringify(expected)}, got ${JSON.stringify(
    actual
  )})`,
});
const includes = (text, parts) => {
  const missing = []
    .concat(parts)
    .filter((part) => !String(text).includes(part));
  return {
    pass: !missing.length,
    detail: `(missing ${JSON.stringify(missing)} in ${JSON.stringify(
      String(text).slice(0, 200)
    )})`,
  };
};

const textOf = async (locator) =>
  ((await locator.first().textContent()) || "").trim();

module.exports = { check, equal, includes, textOf };
