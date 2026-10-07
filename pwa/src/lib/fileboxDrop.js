// Send each picked file to the File Box, one at a time, so a failure names its own
// file and the other files still go. Returns {added: [conversation id], failed:
// [{name, reason}]}.
export async function dropFiles(files, { upload, drop }) {
	const added = [];
	const failed = [];
	for (const file of files) {
		try {
			const up = await upload(file);
			const r = await drop(up.file_url, up.file_name, up.name);
			if (r?.ok === false) {
				failed.push({
					name: file.name,
					reason: r.reason || "Jarvis couldn't take that file.",
				});
			} else {
				added.push(r?.conversation_id || "");
			}
		} catch (e) {
			failed.push({ name: file.name, reason: e?.message || "Couldn't upload that file." });
		}
	}
	return { added, failed };
}
