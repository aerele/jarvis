// Test fixture: get_model_catalog_ui's answer for the backend catalog fixture
// (jarvis/tests/fixtures/model_catalog.py). Test-only; the app has no model list.
export const MODEL_CATALOG_UI = {
	catalog_available: true,
	provider_base_urls: {
		OpenAI: "https://api.openai.com/v1",
		Anthropic: "https://api.anthropic.com",
		"Google Gemini": "https://generativelanguage.googleapis.com",
		Mistral: "https://api.mistral.ai/v1",
		Groq: "https://api.groq.com/openai/v1",
		"Together AI": "https://api.together.xyz/v1",
		DeepSeek: "https://api.deepseek.com",
		"Moonshot (Kimi)": "https://api.moonshot.ai/v1",
		OpenRouter: "https://openrouter.ai/api/v1",
		"Ollama (local)": "http://host.docker.internal:11434/v1",
		"xAI Grok": "https://api.x.ai/v1",
		"GLM / Z.ai": "https://api.z.ai/api/paas/v4",
		"GLM / Z.ai (Coding Plan)": "https://api.z.ai/api/coding/paas/v4",
	},
	api_key_models: {
		OpenAI: [
			{
				model_id: "gpt-5.6",
				label: "gpt-5.6",
				is_default: true,
			},
			{
				model_id: "gpt-5.5",
				label: "gpt-5.5",
				is_default: false,
			},
			{
				model_id: "gpt-5.4",
				label: "gpt-5.4",
				is_default: false,
			},
			{
				model_id: "gpt-5.4-mini",
				label: "gpt-5.4-mini",
				is_default: false,
			},
		],
		Anthropic: [
			{
				model_id: "claude-opus-5",
				label: "claude-opus-5",
				is_default: false,
			},
			{
				model_id: "claude-sonnet-5",
				label: "claude-sonnet-5",
				is_default: true,
			},
			{
				model_id: "claude-haiku-4-5",
				label: "claude-haiku-4-5",
				is_default: false,
			},
			{
				model_id: "claude-opus-4-8",
				label: "claude-opus-4-8",
				is_default: false,
			},
			{
				model_id: "claude-sonnet-4-6",
				label: "claude-sonnet-4-6",
				is_default: false,
			},
		],
		"Google Gemini": [
			{
				model_id: "gemini-3.6-flash",
				label: "gemini-3.6-flash",
				is_default: true,
			},
			{
				model_id: "gemini-2.5-pro",
				label: "gemini-2.5-pro",
				is_default: false,
			},
			{
				model_id: "gemini-3.1-flash-lite",
				label: "gemini-3.1-flash-lite",
				is_default: false,
			},
			{
				model_id: "gemini-2.5-flash",
				label: "gemini-2.5-flash",
				is_default: false,
			},
		],
		Mistral: [
			{
				model_id: "mistral-large-latest",
				label: "mistral-large-latest",
				is_default: true,
			},
			{
				model_id: "mistral-medium-latest",
				label: "mistral-medium-latest",
				is_default: false,
			},
			{
				model_id: "mistral-small-latest",
				label: "mistral-small-latest",
				is_default: false,
			},
		],
		Groq: [
			{
				model_id: "openai/gpt-oss-120b",
				label: "openai/gpt-oss-120b",
				is_default: true,
			},
			{
				model_id: "openai/gpt-oss-20b",
				label: "openai/gpt-oss-20b",
				is_default: false,
			},
			{
				model_id: "llama-3.3-70b-versatile",
				label: "llama-3.3-70b-versatile",
				is_default: false,
			},
			{
				model_id: "llama-3.1-8b-instant",
				label: "llama-3.1-8b-instant",
				is_default: false,
			},
		],
		"Together AI": [
			{
				model_id: "meta-llama/Llama-3.3-70B-Instruct-Turbo",
				label: "meta-llama/Llama-3.3-70B-Instruct-Turbo",
				is_default: true,
			},
		],
		DeepSeek: [
			{
				model_id: "deepseek-flash",
				label: "Latest Flash (V4.1)",
				is_default: true,
			},
			{
				model_id: "deepseek-v4-pro",
				label: "Latest Pro",
				is_default: false,
			},
			{
				model_id: "deepseek-v4-flash",
				label: "Legacy name, same model as deepseek-flash",
				is_default: false,
			},
			{
				model_id: "deepseek-chat",
				label: "Deprecated alias",
				is_default: false,
			},
		],
		"Moonshot (Kimi)": [
			{
				model_id: "kimi-k2.6",
				label: "kimi-k2.6",
				is_default: true,
			},
		],
		OpenRouter: [
			{
				model_id: "anthropic/claude-sonnet-4-6",
				label: "anthropic/claude-sonnet-4-6",
				is_default: true,
			},
			{
				model_id: "openai/gpt-5.5",
				label: "openai/gpt-5.5",
				is_default: false,
			},
		],
		"Ollama (local)": [
			{
				model_id: "qwen2.5:3b",
				label: "qwen2.5:3b",
				is_default: false,
			},
			{
				model_id: "qwen2.5:0.5b",
				label: "qwen2.5:0.5b",
				is_default: false,
			},
			{
				model_id: "llama3",
				label: "llama3",
				is_default: true,
			},
		],
		"vLLM (local)": [],
		"xAI Grok": [
			{
				model_id: "grok-4.5",
				label: "grok-4.5",
				is_default: true,
			},
			{
				model_id: "grok-4.3",
				label: "grok-4.3",
				is_default: false,
			},
			{
				model_id: "grok-build-0.1",
				label: "grok-build-0.1",
				is_default: false,
			},
		],
		"OpenAI-Compatible": [
			{
				model_id: "claude-sonnet-4-6",
				label: "claude-sonnet-4-6",
				is_default: false,
			},
			{
				model_id: "gpt-4o",
				label: "gpt-4o",
				is_default: false,
			},
			{
				model_id: "qwen2.5:3b",
				label: "qwen2.5:3b",
				is_default: false,
			},
			{
				model_id: "llama3",
				label: "llama3",
				is_default: false,
			},
		],
		"GLM / Z.ai": [
			{
				model_id: "glm-4.7",
				label: "glm-4.7",
				is_default: true,
			},
			{
				model_id: "glm-4.6",
				label: "glm-4.6",
				is_default: false,
			},
		],
		"GLM / Z.ai (Coding Plan)": [
			{
				model_id: "glm-4.7",
				label: "glm-4.7",
				is_default: true,
			},
			{
				model_id: "glm-4.6",
				label: "glm-4.6",
				is_default: false,
			},
		],
	},
	subscription_models: {
		OpenAI: ["gpt-5.6-terra", "gpt-5.6-sol", "gpt-5.6-luna", "gpt-6-astra", "gpt-5.5"],
		Anthropic: [
			"claude-opus-5",
			"claude-sonnet-5",
			"claude-fable-5-1",
			"claude-fable-5",
			"claude-opus-4-8",
			"claude-opus-4-7",
			"claude-sonnet-4-6",
			"claude-opus-4-6",
		],
		"Kimi (Moonshot)": ["kimi-k2.7-code", "kimi-k2.6"],
		"xAI Grok": ["grok-4.3", "grok-build-0.1"],
	},
	default_models: {
		OpenAI: "gpt-5.6-terra",
		Anthropic: "claude-opus-5",
		"Kimi (Moonshot)": "kimi-k2.7-code",
		"xAI Grok": "grok-4.3",
	},
	subscription_connect_providers: [
		{
			provider: "OpenAI",
			models: ["gpt-5.6-terra", "gpt-5.6-sol", "gpt-5.6-luna", "gpt-6-astra", "gpt-5.5"],
		},
	],
};
