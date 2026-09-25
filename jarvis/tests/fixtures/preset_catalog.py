"""Test fixture: a copy of the admin preset catalog (see model_catalog.py)."""

from __future__ import annotations

PRESET_CATALOG: list[dict] = [
	{
		"key": "openai-resilient",
		"label": "OpenAI — resilient",
		"kind": "single_vendor",
		"blurb": "One OpenAI key. Your first model runs every turn; the others are backups if it fails.",
		"enabled": True,
		"vendors": ["openai"],
		"models": [
			{"provider": "openai", "model": "gpt-5.5", "order": 0},
			{"provider": "openai", "model": "gpt-5.4", "order": 1},
			{"provider": "openai", "model": "gpt-5.4-mini", "order": 2},
		],
	},
	{
		"key": "anthropic-resilient",
		"label": "Anthropic — resilient",
		"kind": "single_vendor",
		"blurb": "One Anthropic key. Your first model runs every turn; the others are backups if it fails.",
		"enabled": True,
		"vendors": ["anthropic"],
		"models": [
			{"provider": "anthropic", "model": "claude-opus-4-8", "order": 0},
			{"provider": "anthropic", "model": "claude-sonnet-4-6", "order": 1},
			{"provider": "anthropic", "model": "claude-haiku-4-5", "order": 2},
		],
	},
	{
		"key": "gemini-resilient",
		"label": "Google (Gemini API) — resilient",
		"kind": "single_vendor",
		"blurb": "One Gemini API key. Your first model runs every turn; the others are backups if it fails.",
		"enabled": True,
		"vendors": ["gemini"],
		"models": [
			{"provider": "gemini", "model": "gemini-2.5-pro", "order": 0},
			{"provider": "gemini", "model": "gemini-3.5-flash", "order": 1},
			{"provider": "gemini", "model": "gemini-3.1-flash-lite", "order": 2},
		],
	},
	{
		"key": "mistral-resilient",
		"label": "Mistral — resilient",
		"kind": "single_vendor",
		"blurb": "One Mistral key. Your first model runs every turn; the others are backups if it fails.",
		"enabled": True,
		"vendors": ["mistral"],
		"models": [
			{"provider": "mistral", "model": "mistral-large-latest", "order": 0},
			{"provider": "mistral", "model": "mistral-medium-latest", "order": 1},
			{"provider": "mistral", "model": "mistral-small-latest", "order": 2},
		],
	},
	{
		"key": "deepseek-resilient",
		"label": "DeepSeek — resilient",
		"kind": "single_vendor",
		"blurb": "One DeepSeek key. Your first model runs every turn; the other is a backup if it fails.",
		"enabled": True,
		"vendors": ["deepseek"],
		"models": [
			{"provider": "deepseek", "model": "deepseek-flash", "order": 0},
			{"provider": "deepseek", "model": "deepseek-v4-pro", "order": 1},
		],
	},
	{
		"key": "cost-saver",
		"label": "Cost-saver",
		"kind": "cross_vendor",
		"blurb": "Cheapest primary with cross-vendor fallbacks. Needs one key per vendor.",
		"enabled": True,
		"vendors": ["gemini", "mistral", "openai"],
		"models": [
			{"provider": "gemini", "model": "gemini-3.1-flash-lite", "order": 0},
			{"provider": "mistral", "model": "mistral-large-latest", "order": 1},
			{"provider": "openai", "model": "gpt-5.4", "order": 2},
		],
	},
	{
		"key": "balanced",
		"label": "Balanced",
		"kind": "cross_vendor",
		"blurb": "Balanced quality/cost with cross-vendor fallbacks. Needs one key per vendor.",
		"enabled": True,
		"vendors": ["anthropic", "gemini"],
		"models": [
			{"provider": "anthropic", "model": "claude-sonnet-4-6", "order": 0},
			{"provider": "gemini", "model": "gemini-3.5-flash", "order": 1},
			{"provider": "anthropic", "model": "claude-opus-4-8", "order": 2},
		],
	},
	{
		"key": "max-reliability",
		"label": "Max-reliability",
		"kind": "cross_vendor",
		"blurb": "Strongest primary with cross-vendor outage resilience. Needs one key per vendor.",
		"enabled": True,
		"vendors": ["anthropic", "openai", "gemini"],
		"models": [
			{"provider": "anthropic", "model": "claude-opus-4-8", "order": 0},
			{"provider": "openai", "model": "gpt-5.5", "order": 1},
			{"provider": "gemini", "model": "gemini-2.5-pro", "order": 2},
		],
	},
]
