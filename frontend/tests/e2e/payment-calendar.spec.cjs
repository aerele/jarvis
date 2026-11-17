// Browser suite for the Payment Calendar (#821). Each group below registers its tests; every
// check of the design template is either ported there or dropped on purpose (a group says so).
for (const group of [
  "overview",
  "ledger",
  "accounts",
  "months",
  "views",
  "phone",
  "theme",
  "trough",
  "layout",
  "requests",
  "keys",
  "app",
  "variants",
])
  require(`./payment-calendar/${group}.cjs`).register();
