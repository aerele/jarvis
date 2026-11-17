// Catalog rows come from get_capabilities().accounts; bank and reference are optional (D6).
export const accountLabel = (account) => account.account_name || account.name;

// The small line under an account: "{bank} · {reference}", or what the catalog has instead.
export function accountDetail(account) {
	const parts = [account.bank, account.reference].filter(Boolean);
	if (parts.length) return parts.join(" · ");
	return [account.name !== accountLabel(account) ? account.name : "", account.account_currency]
		.filter(Boolean)
		.join(" · ");
}

// Name, bank and reference (so "3281" finds "•••• 3281"), as the template matches them.
export function matchingAccounts(catalog, query) {
	const needle = query.trim().toLowerCase();
	if (!needle) return catalog;
	return catalog.filter((account) =>
		[account.name, account.account_name, account.bank, account.reference]
			.filter(Boolean)
			.join(" ")
			.toLowerCase()
			.includes(needle)
	);
}
