import { describe, it, expect } from "vitest";
import { runModelInfo } from "./agentRunModel";

describe("runModelInfo", () => {
	it("is null for a legacy run (neither field recorded)", () => {
		expect(runModelInfo({ model_used: "", model_rendered: "" })).toBeNull();
		expect(runModelInfo({})).toBeNull();
		expect(runModelInfo(null)).toBeNull();
	});

	it("labels a normal run with the verified model, no note", () => {
		expect(
			runModelInfo({ model_used: "openai/gpt-5", model_rendered: "openai/gpt-5" })
		).toEqual({
			label: "openai/gpt-5",
			note: "",
			warn: false,
		});
	});

	it("flags a below-minimum run", () => {
		expect(
			runModelInfo({
				model_used: "openai/gpt-4",
				model_rendered: "openai/gpt-5",
				model_below_min: 1,
			})
		).toEqual({
			label: "openai/gpt-4",
			note: "Ran on a lower model after a provider failure",
			warn: true,
		});
	});

	it("reads an unverified run off model_rendered with its own note, not a warning", () => {
		expect(runModelInfo({ model_used: "", model_rendered: "openai/gpt-5" })).toEqual({
			label: "openai/gpt-5",
			note: "Model not verified",
			warn: false,
		});
	});

	it("below_min takes precedence over the unverified branch when both could apply", () => {
		expect(
			runModelInfo({ model_used: "openai/gpt-4", model_rendered: "", model_below_min: 1 })
		).toEqual({
			label: "openai/gpt-4",
			note: "Ran on a lower model after a provider failure",
			warn: true,
		});
	});
});
