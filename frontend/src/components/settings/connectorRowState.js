// Whether the row AddConnectorDialog is working against can be thrown away
// when the dialog closes without Save.
//
// Save is the commit point: a row this dialog created is disposable until then,
// even after a passing Connect test (the user may still Cancel). Two things do
// count as real investment and keep the row: a live sign-in (the token would be
// lost) and an app client id/secret the user typed in. A row opened for Edit was
// never created here, so it is never disposable.
export function isDisposableRow({ createdThisSession, oauthConnected, savedClient }) {
	return !!createdThisSession && !oauthConnected && !savedClient;
}
