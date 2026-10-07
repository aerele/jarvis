// Upload the files one at a time: each result or failure is reported for its own
// file, and a failed file does not stop the next one.
export async function uploadEach(files, upload, onDone, onFailed) {
	for (const file of files) {
		try {
			onDone(await upload(file));
		} catch (e) {
			onFailed(e, file);
		}
	}
}
