// Shared by desktop, Desk and mobile. Python reads the same ordered rules.
import rules from "./turn_error_rules.mjs";

const compiled = rules.map((rule) => ({
  ...rule,
  test: new RegExp(rule.pattern, "i"),
}));
const byCode = Object.fromEntries(compiled.map((rule) => [rule.code, rule]));
const MAX_CLASSIFY_CHARS = 8192;

// Chat-subscription sign-ins. The labels and the upstream patterns live in the
// subscription-expired rule data, which chat/error_taxonomy.py also reads.
const SUBSCRIPTION = byCode["subscription-expired"].upstreams;
export const SUBSCRIPTION_LABELS = SUBSCRIPTION.labels;
// `providers=<id>` is how CLIProxy names the account that failed.
const PROVIDERS_TOKEN = /providers=([a-z]+)/i;
const SIGNAL = new RegExp(SUBSCRIPTION.signal, "i");
const MENTIONS = Object.entries(SUBSCRIPTION.mentions).map(
  ([upstream, pattern]) => [upstream, new RegExp(pattern, "gi")]
);
// The message's persisted provider id, when the text names no upstream.
const PROVIDER_ALIASES = {
  openai: "openai",
  "openai-codex": "openai",
  anthropic: "anthropic",
  anthropic_cli: "anthropic",
  "claude-cli": "anthropic",
  xai: "xai",
  kimi: "kimi",
  moonshot: "kimi",
};

function toText(raw) {
  return (
    typeof raw === "object" && raw !== null
      ? JSON.stringify(raw)
      : String(raw ?? "")
  ).slice(0, MAX_CLASSIFY_CHARS);
}

// Which subscription upstream an error text is about: "openai" | "anthropic" |
// "xai" | "kimi", or "" when it cannot be told. A `providers=<id>` token wins;
// else the upstream named nearest BEFORE the first sign-in-dead signal (the one
// that failed in "openai/gpt: 500 auth_unavailable ... | xai/grok: timeout"),
// else the first one after it. `contextProvider` is the chat message's own
// provider id, used only when the text names nothing.
// KEEP IN SYNC with subscription_upstream_from_error in chat/error_taxonomy.py
// (fixtures/subscription_upstreams.json pins both).
export function subscriptionUpstreamFromError(raw, contextProvider = "") {
  const text = toText(raw);
  const token = PROVIDERS_TOKEN.exec(text)?.[1].toLowerCase();
  // Own keys only: "providers=constructor" must not resolve to a prototype member.
  const named = Object.hasOwn(SUBSCRIPTION.providerTokens, token)
    ? SUBSCRIPTION.providerTokens[token]
    : "";
  if (named) return named;
  const signalAt = SIGNAL.exec(text)?.index ?? text.length;
  let before = null;
  let after = null;
  for (const [upstream, pattern] of MENTIONS) {
    for (const match of text.matchAll(pattern)) {
      if (match.index < signalAt) {
        if (!before || match.index > before.at)
          before = { upstream, at: match.index };
      } else if (!after || match.index < after.at) {
        after = { upstream, at: match.index };
      }
    }
  }
  const nearest = before || after;
  if (nearest) return nearest.upstream;
  if (/refresh_token_/i.test(text)) return "openai";
  if (/\/login|\bnot logged in\b/i.test(text)) return "anthropic";
  return PROVIDER_ALIASES[String(contextProvider || "").toLowerCase()] || "";
}
// Routing / inference hosts: when one of these is named it is the provider
// that answered, whatever model brand the text also mentions.
const HOSTS = new Set(["openrouter", "groq", "together"]);
// Official status destinations only; never turn a URL in an error into a link.
// An OpenAI-compatible endpoint does not identify OpenAI as the provider.
const providers = [
  [
    "openrouter",
    "OpenRouter",
    "https://status.openrouter.ai/",
    /\bopenrouter\b/i,
  ],
  ["groq", "Groq", "https://groqstatus.com/", /\bgroq\b/i],
  [
    "together",
    "Together AI",
    "https://status.together.ai/",
    // Suffix is mandatory: bare "together" is ordinary prose ("all retries
    // failed together"), and this entry outranks model-author matches.
    /\btogether(?:\.ai|\s?ai|computer)\b/i,
  ],
  [
    "anthropic",
    "Claude",
    "https://status.claude.com/",
    /\b(anthropic|claude)\b/i,
  ],
  [
    "openai",
    "OpenAI / ChatGPT",
    "https://status.openai.com/",
    /\b(openai(?![_ -]compat)|chatgpt|codex)\b/i,
  ],
  [
    "google",
    "Google Gemini",
    "https://aistudio.google.com/status",
    /\b(google|gemini)\b/i,
  ],
  ["mistral", "Mistral AI", "https://status.mistral.ai/", /\bmistral\b/i],
  ["deepseek", "DeepSeek", "https://status.deepseek.com/", /\bdeepseek\b/i],
  ["xai", "xAI", "https://status.x.ai/", /\b(xai|x\.ai|grok)\b/i],
  [
    "moonshot",
    "Moonshot / Kimi",
    "https://status.moonshot.cn/",
    /\b(moonshot|kimi)\b/i,
  ],
];
function providerFor(raw, context) {
  // Persisted actual provider outranks model names (e.g. Claude via OpenRouter).
  if (context?.provider) {
    const id = String(context.provider).toLowerCase();
    return providers.find(
      ([key]) =>
        key === id ||
        (key === "google" && id === "gemini") ||
        (key === "openai" && id === "openai-codex")
    );
  }
  // Do not infer a hosting company from a model name alone or the current
  // model picker: failover may have used a different connection for this run.
  if (/openai[_ -]compat|\b(ollama|vllm|azure|bedrock|vertex)\b/i.test(raw))
    return;
  const matches = providers.filter(([, , , pattern]) => pattern.test(raw));
  const hosts = matches.filter(([key]) => HOSTS.has(key));
  // Exactly one host named: it outranks model brands ("claude via openrouter").
  // Two hosts, or several brands and no host: ambiguous, so no link.
  if (hosts.length === 1) return hosts[0];
  return matches.length === 1 && hosts.length === 0 ? matches[0] : undefined;
}

// Codes that say "the provider failed" without saying why. A dead sign-in on a pool tenant arrives
// this way (CLIProxy's 503 is flattened to a bare "provider internal error"), so these are the only
// codes the site's own expired-sign-in state may upgrade. Specific causes (authentication, quota,
// rate-limit, safety, ...) are never overridden.
const GENERIC_CODES = new Set([
  "provider",
  "gateway",
  "service-unavailable",
  "internal",
]);
// "openai_compat/gpt-5.6-terra" and "gpt-5.6-terra" are the same model.
const bareModel = (id) =>
  String(id ?? "")
    .trim()
    .split("/")
    .pop();

function expiredUpstreamFor(code, context) {
  const map = context?.expiredModels;
  const model = bareModel(context?.model);
  if (!model || !map || !GENERIC_CODES.has(code)) return "";
  for (const [id, entry] of Object.entries(map)) {
    if (
      bareModel(id) === model &&
      Object.hasOwn(SUBSCRIPTION_LABELS, entry?.upstream)
    )
      return entry.upstream;
  }
  return "";
}

export function turnErrorInfo(raw, explicitCode, context = {}) {
  try {
    // Cap what the regexes see: error text is model-supplied and unbounded,
    // and a few patterns have bounded-but-real backtracking. Same cap as
    // the Python classifier so both sides read the same prefix.
    const text = toText(raw);
    const inferred =
      compiled.find((rule) => rule.test.test(text))?.code || "gateway";
    // Older servers publish broad gateway/provider codes. Refine those from
    // the saved text too, so historical and live errors get the same remedy.
    let code =
      !explicitCode || ["gateway", "provider"].includes(explicitCode)
        ? inferred === "gateway"
          ? explicitCode || inferred
          : inferred
        : explicitCode;
    // A dead chat-subscription sign-in needs an upstream to name. Without one, or
    // when the caller says the workspace has no subscription row for it (an array
    // that lacks it; undefined means "unknown"), the plain authentication copy fits.
    let upstream = "";
    if (code === "subscription-expired") {
      upstream = subscriptionUpstreamFromError(text, context?.provider);
      const known = context?.subscriptionUpstreams;
      if (!upstream || (Array.isArray(known) && !known.includes(upstream)))
        code = "authentication";
    }
    // The site already holds this model's sign-in as expired (admin poll): a generic failure on
    // that model is that sign-in, whatever the flattened error text says.
    const expiredUpstream = expiredUpstreamFor(code, context);
    if (expiredUpstream) {
      code = "subscription-expired";
      upstream = expiredUpstream;
    }
    const rule = Object.hasOwn(byCode, code) ? byCode[code] : byCode.internal;
    if (code === "subscription-expired") {
      const label = SUBSCRIPTION_LABELS[upstream];
      return {
        code,
        headline: rule.headline.replace("{provider}", label),
        hint:
          context?.admin === true
            ? "Reconnect it in AI models, then send again."
            : rule.hint,
        retryable: rule.retryable,
        statusUrl: "",
        statusLabel: "",
        action: "reconnect",
        actionLabel: `Reconnect ${label}`,
        upstream,
      };
    }
    const provider = rule.status ? providerFor(text, context) : undefined;
    // A status link supplements the cause-specific guidance; it must not
    // turn a timeout or connection failure into an asserted provider outage.
    const providerCopy = provider
      ? {
          "service-unavailable": {
            headline: `${provider[1]} could not complete this request`,
            hint: "Try again shortly, or choose another available model.",
          },
          timeout: {
            headline: `${provider[1]} did not respond in time`,
            hint: "Try again, split a large request into smaller parts, or choose another available model.",
          },
          connection: {
            headline: `Jarvis could not connect to ${provider[1]}`,
            hint: "Try again or choose another available model. If this continues, ask your administrator to check the connection.",
          },
        }
      : {};
    const copy = Object.hasOwn(providerCopy, code) ? providerCopy[code] : rule;
    return {
      code,
      headline: copy.headline,
      hint: copy.hint,
      retryable: rule.retryable,
      statusUrl: provider?.[2] || "",
      statusLabel: provider ? `Check ${provider[1]} status` : "",
    };
  } catch {
    return {
      code: "internal",
      headline: byCode.internal.headline,
      hint: byCode.internal.hint,
      retryable: true,
      statusUrl: "",
      statusLabel: "",
    };
  }
}
