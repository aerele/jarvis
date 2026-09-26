import { beforeEach, describe, expect, it, vi } from "vitest";
import { downloadPdf } from "./dashboardExport";

const pdf = vi.hoisted(() => ({
	create: vi.fn(),
	addPage: vi.fn(),
	addImage: vi.fn(),
	save: vi.fn(),
}));
vi.mock("jspdf", () => ({
	jsPDF: class {
		constructor(options) {
			pdf.create(options);
		}
		addPage = pdf.addPage;
		addImage = pdf.addImage;
		save = pdf.save;
	},
}));

beforeEach(() => vi.clearAllMocks());

describe("dashboard PDF margins", () => {
	it.each([
		[800, 450, "landscape"],
		[450, 650, "portrait"],
		[400, 400, "landscape"],
	])(
		"adds equal margins around a %s × %s slide without shrinking it",
		async (w, h, orientation) => {
			await downloadPdf([{ dataUrl: "slide", w, h }], "Sales overview");
			expect(pdf.create).toHaveBeenCalledWith({
				orientation,
				unit: "px",
				format: [w + 48, h + 48],
				hotfixes: ["px_scaling"],
			});
			expect(pdf.addImage).toHaveBeenCalledWith("slide", "PNG", 24, 24, w, h);
			expect(pdf.save).toHaveBeenCalledWith("sales-overview.pdf");
		}
	);

	it("preserves the size and orientation of every page in a mixed slide deck", async () => {
		await downloadPdf(
			[
				{ dataUrl: "wide", w: 800, h: 450 },
				{ dataUrl: "tall", w: 450, h: 650 },
			],
			"Review"
		);
		expect(pdf.create).toHaveBeenCalledTimes(1);
		expect(pdf.addPage).toHaveBeenCalledTimes(1);
		expect(pdf.addPage).toHaveBeenCalledWith([498, 698], "portrait");
		expect(pdf.addImage).toHaveBeenNthCalledWith(1, "wide", "PNG", 24, 24, 800, 450);
		expect(pdf.addImage).toHaveBeenNthCalledWith(2, "tall", "PNG", 24, 24, 450, 650);
		expect(pdf.save).toHaveBeenCalledTimes(1);
	});

	it("rejects an empty export without creating a PDF", async () => {
		await expect(downloadPdf([], "Empty")).rejects.toThrow("Nothing was captured");
		expect(pdf.create).not.toHaveBeenCalled();
		expect(pdf.save).not.toHaveBeenCalled();
	});
});
