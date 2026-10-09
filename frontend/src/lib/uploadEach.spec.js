import { describe, expect, it, vi } from "vitest";

import { uploadEach } from "./uploadEach";

describe("uploadEach", () => {
	it("uploads each file in order and reports each one", async () => {
		const upload = vi.fn(async (f) => ({ name: `F-${f.name}` }));
		const done = vi.fn();
		const failed = vi.fn();
		await uploadEach([{ name: "a" }, { name: "b" }], upload, done, failed);
		expect(upload.mock.calls.map((c) => c[0].name)).toEqual(["a", "b"]);
		expect(done.mock.calls).toEqual([[{ name: "F-a" }], [{ name: "F-b" }]]);
		expect(failed).not.toHaveBeenCalled();
	});

	it("a failed file is reported and the next file still uploads", async () => {
		const err = new Error("too big");
		const upload = vi.fn(async (f) => {
			if (f.name === "a") throw err;
			return { name: `F-${f.name}` };
		});
		const done = vi.fn();
		const failed = vi.fn();
		await uploadEach([{ name: "a" }, { name: "b" }], upload, done, failed);
		expect(failed).toHaveBeenCalledWith(err, { name: "a" });
		expect(done).toHaveBeenCalledWith({ name: "F-b" });
	});
});
