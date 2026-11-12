// The "Choose accounts" catalog: the 60 accounts of the golden balances job (the template's
// sample accounts). get_capabilities serves name, label, currency, bank and reference.
const capabilityAccounts = (golden) =>
  golden.reply("balances_complete").accounts.map((row) => ({
    name: row.account,
    account_name: row.account_name,
    account_currency: "INR",
    ...(row.bank ? { bank: row.bank } : {}),
    ...(row.reference ? { reference: row.reference } : {}),
  }));

module.exports = { capabilityAccounts };
