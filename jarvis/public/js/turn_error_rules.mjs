// Keep this literal valid JSON: the Python classifier reads the same data.
// prettier-ignore
export default [
  {
    "code": "cancelled",
    "pattern": "^(you cancelled this message|waited too long in the queue|stopped before it started)",
    "headline": "This message was cancelled",
    "hint": "",
    "retryable": false,
    "status": false
  },
  {
    "code": "internal",
    "pattern": "^unexpected worker error",
    "headline": "Jarvis hit an unexpected error",
    "hint": "Try again. If the error returns, share the details with your administrator or support.",
    "retryable": true,
    "status": false
  },
  {
    "code": "subscription-expired",
    "pattern": "refresh_token_(reused|expired|invalidated|revoked)|auth_unavailable[\\s\\S]{0,600}unauthori[sz]ed|please run /login|\\bnot logged in\\b|(providers=(codex|xai|kimi)|\\b(codex|kimi|xai)\\b|claude code)[\\s\\S]{0,400}(invalid_grant|oauth token has expired|token expired)|(invalid_grant|oauth token has expired|token expired)[\\s\\S]{0,400}(providers=(codex|xai|kimi)|\\b(codex|kimi|xai)\\b|claude code)",
    "headline": "{provider} sign-in expired",
    "hint": "Ask your workspace admin to reconnect it, then send again.",
    "retryable": false,
    "status": false,
    "upstreams": {
      "labels": {
        "openai": "OpenAI",
        "anthropic": "Anthropic",
        "xai": "xAI Grok",
        "kimi": "Kimi (Moonshot)"
      },
      "providerTokens": {
        "codex": "openai",
        "openai": "openai",
        "xai": "xai",
        "kimi": "kimi",
        "claude": "anthropic",
        "anthropic": "anthropic"
      },
      "signal": "refresh_token_(?:reused|expired|invalidated|revoked)|auth_unavailable[\\s\\S]{0,600}unauthori[sz]ed|please run /login|\\bnot logged in\\b|invalid_grant|oauth token has expired|token expired",
      "mentions": {
        "openai": "\\b(?:codex|openai|chatgpt)\\b",
        "anthropic": "\\b(?:claude code|anthropic|claude)\\b",
        "xai": "\\b(?:xai|grok)\\b",
        "kimi": "\\b(?:kimi|moonshot)\\b"
      }
    }
  },
  {
    "code": "models-exhausted",
    "pattern": "all (models|providers|fallbacks) failed",
    "headline": "The models tried could not complete this request",
    "hint": "Try again. If it returns, share the details with your administrator to check each connection.",
    "retryable": true,
    "status": false
  },
  {
    "code": "runtime-permission",
    "pattern": "\\b(eperm|eacces|erofs)\\b|operation not permitted|read.only (file system|database)",
    "headline": "Jarvis was blocked from accessing a required file",
    "hint": "A server permission setting needs attention. Share the details with your administrator or support.",
    "retryable": false,
    "status": false
  },
  {
    "code": "storage",
    "pattern": "\\b(enospc|edquot|sqlite_full|sqlite_corrupt|sqlite_busy)\\b|no space left on device|database (is locked|disk image is malformed)|disk quota exceeded",
    "headline": "Jarvis could not use its storage",
    "hint": "Ask your administrator or support to check the server’s storage and database. Share the details below.",
    "retryable": false,
    "status": false
  },
  {
    "code": "runtime-setup",
    "pattern": "\\benoent\\b|cannot find module|module not found|pairing required|device.{0,80}(pairing|signature).{0,80}invalid|no auth profiles|no api key found|agent_url not set",
    "headline": "The assistant’s setup needs attention",
    "hint": "Ask your administrator to check the assistant’s configuration. The details below can help identify what needs fixing.",
    "retryable": false,
    "status": false
  },
  {
    "code": "session-reset",
    "pattern": "\\bcli_live_session_(changed|missing)\\b|live session is no longer reusable",
    "headline": "The model session restarted",
    "hint": "This can happen right after switching models. Try again.",
    "retryable": true,
    "status": false
  },
  {
    "code": "unreachable",
    "pattern": "ws open failed|\\bunreachable\\b|connection timed out",
    "headline": "Jarvis could not reach the assistant",
    "hint": "Try again shortly. If the error returns, ask your administrator to check the assistant’s connection.",
    "retryable": true,
    "status": false
  },
  {
    "code": "recovery-expired",
    "pattern": "recovery window",
    "headline": "Jarvis could not confirm whether this request finished",
    "hint": "Check for completed actions before sending the message again.",
    "retryable": true,
    "status": false
  },
  {
    "code": "context-limit",
    "pattern": "context[_ ](length[_ ]exceeded|overflow|window)|maximum context length|too many tokens|prompt is too long|input.{0,80}exceeds.{0,80}token",
    "headline": "This conversation is too long for the model",
    "hint": "Start a new chat with a short summary, or send less content.",
    "retryable": false,
    "status": false
  },
  {
    "code": "request-size",
    "pattern": "request[_ ]too[_ ]large|payload too large|request entity too large|(?:\\b(?:http(?:error)?|status(?:[_ ]code)?|error(?: code)?|code|api|openai|anthropic|claude|gemini|google|groq|mistral|deepseek|openrouter|xai|x\\.ai|together)\\b[\\s\\x22\\x27=:({\\[]*413\\b|^413\\b)",
    "headline": "This request is too large",
    "hint": "Use smaller attachments or split your request into smaller parts.",
    "retryable": false,
    "status": false
  },
  {
    "code": "model-unavailable",
    "pattern": "model[_ ]not[_ ]found|unknown model|no such model|model.*(does not exist|not available|not supported|decommissioned|not allowed)",
    "headline": "This model is not available for this connection",
    "hint": "Choose another available model, or ask your administrator to check the model settings and account access.",
    "retryable": false,
    "status": false
  },
  {
    "code": "billing",
    "pattern": "insufficient[_ ](quota|credit|balance|funds)|credit.{0,20}(expired|exhausted)|credit balance|out of credits|billing|payment required|(?:\\b(?:http(?:error)?|status(?:[_ ]code)?|error(?: code)?|code|api|openai|anthropic|claude|gemini|google|groq|mistral|deepseek|openrouter|xai|x\\.ai|together)\\b[\\s\\x22\\x27=:({\\[]*402\\b|^402\\b)|spend(ing)? limit",
    "headline": "The model account needs a billing check",
    "hint": "Ask the account owner to check the balance, payment details, and spending limit before trying again.",
    "retryable": false,
    "status": false
  },
  {
    "code": "rate-limit",
    "pattern": "rate[_ -]?limit|too many requests|cooldown|(?:\\b(?:http(?:error)?|status(?:[_ ]code)?|error(?: code)?|code|api|openai|anthropic|claude|gemini|google|groq|mistral|deepseek|openrouter|xai|x\\.ai|together)\\b[\\s\\x22\\x27=:({\\[]*429\\b|^429\\b)|resource_exhausted",
    "headline": "The model’s request limit was reached",
    "hint": "Wait before trying again, or choose another available model. If the error returns, ask the account owner to check the usage limit.",
    "retryable": true,
    "status": false
  },
  {
    "code": "quota",
    "pattern": "\\bquota\\b|usage limit|usage cap",
    "headline": "The model account’s usage allowance was reached",
    "hint": "Try another available model, or ask the account owner when the allowance resets and whether it can be increased.",
    "retryable": false,
    "status": false
  },
  {
    "code": "authentication",
    "pattern": "authentication[_ ]error|unauthenticated|unauthori[sz]ed|invalid[_ ]api[_ ]key|incorrect api key|api key.*(invalid|expired|revoked)|token.*(expired|revoked)|invalid_grant|(?:\\b(?:http(?:error)?|status(?:[_ ]code)?|error(?: code)?|code|api|openai|anthropic|claude|gemini|google|groq|mistral|deepseek|openrouter|xai|x\\.ai|together)\\b[\\s\\x22\\x27=:({\\[]*401\\b|^401\\b)|auth_permanent|(?:auth|oauth|token)_expired|api_key_invalid|authentication failed|invalid x-api-key",
    "headline": "The service could not verify this connection",
    "hint": "Ask your administrator to reconnect the service or update its access key.",
    "retryable": false,
    "status": false
  },
  {
    "code": "safety",
    "pattern": "content[_ ](policy|filter)|safety.*(block|reject)|blocked.*safety|prohibited_content|responsibleaipolicyviolation",
    "headline": "The model declined this request under its safety rules",
    "hint": "Review your message and attachments. If you think this was a mistake, ask your administrator to raise it with the model provider.",
    "retryable": false,
    "status": false
  },
  {
    "code": "permission",
    "pattern": "permission[_ ](denied|error)|access denied|forbidden|(?:\\b(?:http(?:error)?|status(?:[_ ]code)?|error(?: code)?|code|api|openai|anthropic|claude|gemini|google|groq|mistral|deepseek|openrouter|xai|x\\.ai|together)\\b[\\s\\x22\\x27=:({\\[]*403\\b|^403\\b)",
    "headline": "Access was denied for this request",
    "hint": "Ask your administrator to check permission to use the affected model, service, or record.",
    "retryable": false,
    "status": false
  },
  {
    "code": "invalid-request",
    "pattern": "invalid[_ ]request|bad request|unsupported.*(parameter|format|image|media|file)|invalid.*(parameter|schema)|(?:\\b(?:http(?:error)?|status(?:[_ ]code)?|error(?: code)?|code|api|openai|anthropic|claude|gemini|google|groq|mistral|deepseek|openrouter|xai|x\\.ai|together)\\b[\\s\\x22\\x27=:({\\[]*(400|415|422)\\b|^(400|415|422)\\b)",
    "headline": "The service could not accept this request",
    "hint": "Check your message and attachments. If the error returns, share the details with your administrator.",
    "retryable": false,
    "status": false
  },
  {
    "code": "service-unavailable",
    "pattern": "overload|service[_ ]unavailable|temporarily unavailable|high demand|(?:\\b(?:http(?:error)?|status(?:[_ ]code)?|error(?: code)?|code|api|openai|anthropic|claude|gemini|google|groq|mistral|deepseek|openrouter|xai|x\\.ai|together)\\b[\\s\\x22\\x27=:({\\[]*(500|502|503|529)\\b|^(500|502|503|529)\\b)|internal server error|bad gateway|server_error|no endpoints found",
    "headline": "The service could not complete this request",
    "hint": "Wait a little and try again. If the error returns, share the details with your administrator or support.",
    "retryable": true,
    "status": true
  },
  {
    "code": "timeout",
    "pattern": "timed out|timeout|deadline|(?:\\b(?:http(?:error)?|status(?:[_ ]code)?|error(?: code)?|code|api|openai|anthropic|claude|gemini|google|groq|mistral|deepseek|openrouter|xai|x\\.ai|together)\\b[\\s\\x22\\x27=:({\\[]*(408|504)\\b|^(408|504)\\b)",
    "headline": "The request took too long to finish",
    "hint": "Try again, or split a large request into smaller parts. If the error returns, share the details with your administrator.",
    "retryable": true,
    "status": true
  },
  {
    "code": "connection",
    "pattern": "\\b(econnreset|econnrefused|enotfound|eai_again|etimedout)\\b|failed to fetch|socket hang up|certificate.*(expired|invalid)|tls handshake",
    "headline": "Jarvis could not connect to the service",
    "hint": "Try again. If the error returns, ask your administrator to check the service’s connection. Share the details below.",
    "retryable": true,
    "status": true
  },
  {
    "code": "not-found",
    "pattern": "not[_ ]found|(?:\\b(?:http(?:error)?|status(?:[_ ]code)?|error(?: code)?|code|api|openai|anthropic|claude|gemini|google|groq|mistral|deepseek|openrouter|xai|x\\.ai|together)\\b[\\s\\x22\\x27=:({\\[]*404\\b|^404\\b)",
    "headline": "Something needed for this request could not be found",
    "hint": "Check that the referenced item still exists. Share the details with your administrator if you need help.",
    "retryable": false,
    "status": false
  },
  {
    "code": "empty-reply-tools",
    "pattern": "could(?:n.?t| not) generate a (?:response|reply)[\\s\\S]{0,300}tool actions may have already been executed",
    "headline": "The reply stopped before it was complete",
    "hint": "Some actions may already be done, and a retry could repeat them. Check for completed actions before you try again.",
    "retryable": true,
    "status": false
  },
  {
    "code": "empty-reply",
    "pattern": "could(?:n.?t| not) generate a (?:response|reply)",
    "headline": "The model returned an empty reply",
    "hint": "Try again. If the error returns, start a new chat or choose another available model.",
    "retryable": true,
    "status": false
  },
  {
    "code": "provider",
    "pattern": "(?!)",
    "headline": "The model provider could not complete this request",
    "hint": "Share the details with the model account owner to check what prevented the request.",
    "retryable": false,
    "status": false
  },
  {
    "code": "gateway",
    "pattern": "(?!)",
    "headline": "Jarvis could not complete this request",
    "hint": "The cause is not clear from the error received. Try again. If it happens again, share the details with your administrator or support.",
    "retryable": true,
    "status": false
  }
];
