// Turns what a check returned into { pass, detail }. Kept apart from checks.cjs (which needs
// Playwright) so a unit test can load it.
const firstLine = (text) => String(text).split("\n")[0].slice(0, 300);

const hasPass = (value) => typeof value.pass === "boolean";

// A boolean-like value, or an object { pass: boolean, detail? }. Any other object is a
// mistake in the check (it would always look "true"), so it fails with a message that says so.
async function evaluate(read) {
  try {
    const value = await read();
    if (typeof value !== "object" || value === null) {
      return { pass: !!value, detail: "" };
    }
    if (hasPass(value)) return value;
    return {
      pass: false,
      detail: `(check returned an object without a boolean "pass": keys ${
        Object.keys(value).join(", ") || "none"
      })`,
    };
  } catch (error) {
    return { pass: false, detail: firstLine(error.message) };
  }
}

module.exports = { evaluate };
