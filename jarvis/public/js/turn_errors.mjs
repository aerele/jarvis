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
  if (
    /\/login|\bnot logged in\b|oauth session expired and could not be refreshed/i.test(
      text
    )
  )
    return "anthropic";
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
// rate-limit, safety, ...) are never overridden, nor is empty-reply-tools: its warning that some
// actions may already be done must stay.
const GENERIC_CODES = new Set([
  "provider",
  "gateway",
  "service-unavailable",
  "internal",
  "empty-reply",
]);
// "openai_compat/gpt-5.6-terra" and "gpt-5.6-terra" are the same model.
const bareModel = (id) =>
  String(id ?? "")
    .trim()
    .split("/")
    .pop();

// A failed turn's row has no `model` (only answered turns are stamped), but the agent runtime writes
// "<provider>/<model> request failed (...)" into the error, so read the model from there.
const FAILED_MODEL =
  /^[\s\p{Extended_Pictographic}\uFE0F]*[\w.-]+\/([\w.:+-]+) request failed/u;

function expiredUpstreamFor(code, context, text) {
  const map = context?.expiredModels;
  const model = bareModel(context?.model) || FAILED_MODEL.exec(text)?.[1] || "";
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

function inferCode(text) {
  return compiled.find((rule) => rule.test.test(text))?.code || "gateway";
}

function singleErrorInfo(text, explicitCode, context) {
  {
    const inferred = inferCode(text);
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
    const expiredUpstream = expiredUpstreamFor(code, context, text);
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
  }
}

// "All models failed (2): <provider>/<model>: <reason> | <provider>/<model>: <reason>" is how the
// agent runtime reports a failover run in which every model failed. Each part names its own cause,
// and they often differ (one model out of allowance, another signed out), so one headline for the
// whole text hides the one the customer can act on.
// KEEP IN SYNC with model_failures in chat/error_taxonomy.py (fixtures/model_failures.json pins both).
const FAILOVER_HEAD = /\bAll models failed \(\d+\):\s*/i;
const FAILOVER_PART = /^([\w.-]+)\/([^\s:]+):\s*\S/;
// A pool proxy answers under its own alias; the model it was asked for is named in the reason.
const NAMED_MODEL = /\bfor model ([\w.:+-]+)/i;
const RETRY_AFTER = /\bretry-after:\s*(\d+)\s*s/i;
// Older rows were stored cut at 240 characters ("... | anthropic/claude-...").
const CUT_TAIL = /(?:\.\.\.|\u2026)\s*$/;

export function modelFailures(raw) {
  const text = toText(raw);
  const head = FAILOVER_HEAD.exec(text);
  if (!head) return [];
  const parts = text.slice(head.index + head[0].length).split(/\s+\|\s+/);
  return parts.map((part, i) => {
    const segment = part.trim();
    const cut = i === parts.length - 1 && CUT_TAIL.test(segment);
    const named = FAILOVER_PART.exec(segment);
    if (!named) {
      const bare = segment
        .replace(CUT_TAIL, "")
        .split("/")
        .pop()
        .replace(/:.*$/, "");
      return {
        model: bare ? `${bare}\u2026` : "",
        segment,
        clipped: true,
        cut,
        retryAfterS: null,
      };
    }
    const retry = RETRY_AFTER.exec(segment);
    return {
      model: NAMED_MODEL.exec(segment)?.[1] || named[2],
      segment,
      clipped: false,
      cut,
      retryAfterS: retry ? Number(retry[1]) : null,
    };
  });
}

// "about 2 days" / "about 3 hours" / "about 20 minutes" for a wait in seconds.
export function waitPhrase(seconds) {
  const s = Number(seconds);
  if (!Number.isFinite(s) || s <= 0) return "";
  const unit = (n, word) => `about ${n} ${word}${n === 1 ? "" : "s"}`;
  if (s >= 36 * 3600) return unit(Math.round(s / 86400), "day");
  if (s >= 90 * 60) return unit(Math.round(s / 3600), "hour");
  return unit(Math.max(1, Math.round(s / 60)), "minute");
}

const WAIT_CODES = new Set(["quota", "rate-limit"]);

// The wait still left: Retry-After counts from when the turn failed (`failedAtMs`, when the caller
// knows it), not from now.
function waitLeft(seconds, failedAtMs) {
  if (!Number.isFinite(Number(seconds)) || Number(seconds) <= 0)
    return { phrase: "", over: false };
  if (!Number.isFinite(failedAtMs))
    return { phrase: waitPhrase(seconds), over: false };
  const left = (failedAtMs + Number(seconds) * 1000 - Date.now()) / 1000;
  return left > 0
    ? { phrase: waitPhrase(left), over: false }
    : { phrase: "", over: true };
}

const NOT_RECEIVED = {
  code: "",
  reason: "Reason not received",
  next: "",
  retryable: true,
};

// One short line per failed model: what went wrong, and the one next step.
function modelLine(failure, context) {
  if (failure.clipped) return { model: failure.model, ...NOT_RECEIVED };
  const info = singleErrorInfo(failure.segment, undefined, {
    ...context,
    provider: "",
    model: failure.model,
  });
  // A reason cut mid-sentence that no rule recognises says nothing; do not dress it up.
  if (failure.cut && info.code === "gateway")
    return { model: failure.model, ...NOT_RECEIVED };
  const admin = context?.admin === true;
  const left = WAIT_CODES.has(info.code)
    ? waitLeft(failure.retryAfterS, context?.failedAtMs)
    : { phrase: "", over: false };
  const wait = left.phrase;
  const copy = {
    quota: [
      "Usage limit reached",
      wait
        ? `Resets in ${wait}.`
        : left.over
        ? "It may have reset. Try again."
        : "Wait for it to reset.",
    ],
    "rate-limit": [
      "Too many requests",
      wait ? `Try again in ${wait}.` : "Try again shortly.",
    ],
    "subscription-expired": [
      info.headline,
      admin
        ? "Reconnect it in AI models."
        : "Ask your workspace admin to reconnect it.",
    ],
    authentication: [
      "Sign-in or key not accepted",
      admin
        ? "Check it in AI models."
        : "Ask your workspace admin to check it.",
    ],
    billing: ["Billing problem", "Ask the account owner to check billing."],
  }[info.code] || [info.headline, ""];
  return {
    model: failure.model,
    code: info.code,
    reason: copy[0],
    next: copy[1],
    // A usage cap whose wait has run out is worth one more try.
    retryable: info.retryable || left.over,
    upstream: info.upstream || "",
  };
}

// Codes whose headline cannot name the cause. For these the stored error text is the only reason
// there is, so the card shows it as a plain line; every other card says the cause in its headline
// (and one line per model), with no raw text to expand.
const RAW_REASON_CODES = new Set(["gateway", "provider", "internal"]);

export function showsRawReason(info) {
  return Boolean(info) && !info.models && RAW_REASON_CODES.has(info.code);
}

const TEXT_CODES = new Set([
  "gateway",
  "provider",
  "models-exhausted",
  "subscription-expired",
]);

export function turnErrorInfo(raw, explicitCode, context = {}) {
  try {
    // Cap what the regexes see: error text is model-supplied and unbounded,
    // and a few patterns have bounded-but-real backtracking. Same cap as
    // the Python classifier so both sides read the same prefix.
    const text = toText(raw);
    const whole = singleErrorInfo(text, explicitCode, context);
    const failures =
      explicitCode && !TEXT_CODES.has(explicitCode) ? [] : modelFailures(text);
    if (failures.length < 2) return whole;
    const models = failures.map((failure) => modelLine(failure, context));
    const signedOut = models.find((m) => m.code === "subscription-expired");
    const causes = new Set(models.map((m) => m.code));
    const oneCause = causes.size === 1 && !causes.has("");
    const rule =
      oneCause && Object.hasOwn(byCode, models[0].code)
        ? byCode[models[0].code]
        : null;
    return {
      code: whole.code,
      headline: oneCause
        ? models[0].reason
        : "None of your models could answer",
      hint: oneCause && rule && !signedOut ? rule.hint : "",
      retryable: models.some((m) => m.retryable),
      statusUrl: "",
      statusLabel: "",
      models,
      ...(signedOut
        ? {
            action: "reconnect",
            actionLabel: `Reconnect ${SUBSCRIPTION_LABELS[signedOut.upstream]}`,
            upstream: signedOut.upstream,
          }
        : {}),
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
