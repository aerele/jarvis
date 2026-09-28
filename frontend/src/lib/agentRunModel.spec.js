import { describe, it, expect } from "vitest";
import { runModelInfo } from "./agentRunModel";

describe("runModelInfo", () => {
	it("is null for a legacy run (neither field recorded)", () => {
		expect(runModelInfo({ model_used: "", model_rendered: "" })).toBeNull();
		expect(runModelInfo({})).toBeNull();
		expect(runModelInfo(null)).toBeNull();
	});

	it("labels a normal run with the verified model, no note - provider/door prefix stripped, full ref kept as title", () => {
		expect(
			runModelInfo({ model_used: "openai/gpt-5", model_rendered: "openai/gpt-5" })
		).toEqual({
			label: "gpt-5",
			title: "openai/gpt-5",
			note: "",
			warn: false,
		});
	});

	// UX-1: only the FIRST '/' is a delimiter - the model id itself may carry
	// '/' (an openrouter-style ref), and that must survive into the label.
	it("strips only the first path segment (provider/door), even for a multi-segment provider or model id", () => {
		expect(runModelInfo({ model_used: "openai_compat/gpt-5.6-terra" }).label).toBe(
			"gpt-5.6-terra"
		);
		expect(runModelInfo({ model_used: "anthropic-2/claude-opus-5" }).label).toBe(
			"claude-opus-5"
		);
		expect(runModelInfo({ model_used: "openrouter/anthropic/claude-x" }).label).toBe(
			"anthropic/claude-x"
		);
	});

	it("flags a below-minimum run with neutral copy (no failure implied)", () => {
		expect(
			runModelInfo({
				model_used: "openai/gpt-4",
				model_rendered: "openai/gpt-5",
				model_below_min: 1,
			})
		).toEqual({
			label: "gpt-4",
			title: "openai/gpt-4",
			note: "Ran on a model below this agent's requirement.",
			warn: true,
		});
	});

	it("reads an unverified run off model_rendered with its own note, not a warning", () => {
		expect(runModelInfo({ model_used: "", model_rendered: "openai/gpt-5" })).toEqual({
			label: "gpt-5",
			title: "openai/gpt-5",
			note: "Model not verified",
			warn: false,
		});
	});

	it("below_min takes precedence over the unverified branch when both could apply", () => {
		expect(
			runModelInfo({ model_used: "openai/gpt-4", model_rendered: "", model_below_min: 1 })
		).toEqual({
			label: "gpt-4",
			title: "openai/gpt-4",
			note: "Ran on a model below this agent's requirement.",
			warn: true,
		});
	});
});
