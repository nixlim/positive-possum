# Mubit API notes (verified 2026-10-23)

Source: https://docs.mubit.ai/llms-full.txt. Console: https://app.mubit.ai/console/.

## What Mubit is

A recording proxy in front of OpenAI, Anthropic and Gemini. You keep the provider's
own API, SDK and model IDs; you only change the base URL and send a Mubit key
(`mbt_...`, from `.env` as `MUBIT_API_KEY`). Mubit records every call, groups calls
into **tasks**, and trains a small **adapter** per task once it has ~25 kept examples.
Adapters are compared in the console's **Try models**; there is no Mubit model ID you
can call from the API.

## Base URLs

| Client | Base URL | Key header |
| --- | --- | --- |
| OpenAI SDK / curl | `https://api.mubit.ai/openai/v1` | `Authorization: Bearer` |
| Anthropic SDK / curl | `https://api.mubit.ai/anthropic` (SDK adds `/v1`) | `x-api-key` or Bearer |
| Gemini (google-genai) | `https://api.mubit.ai/gemini` | `x-goog-api-key` |
| OpenAI SDK on Gemini | `https://api.mubit.ai/gemini/v1beta/openai` | Bearer |
| OpenRouter/Groq/Together/Mistral | `https://api.mubit.ai/compat/{service}/v1` | Bearer |

Send only the Mubit key. Any extra credential (e.g. `ANTHROPIC_AUTH_TOKEN`) gets a 401.
No CORS: call from a server, not a browser.

## Verified working on this key (starter credit, no provider keys added)

- OpenAI: `GET /models`, `POST /chat/completions` (plain, streamed, `response_format`
  json_schema), `POST /responses` (with `metadata.session_id`), `POST /embeddings`.
- Anthropic: `GET /v1/models`, `POST /v1/messages` (plain, streamed, forced tool use
  with `tool_choice`), `POST /v1/messages/count_tokens`. The model the forced tool
  use was checked on was not recorded; claude-sonnet-5-5 is said to refuse it
  (unchecked, item 2 below).
- Gemini: `generateContent`, `streamGenerateContent?alt=sse`.
- Python SDKs (openai, anthropic) work unchanged with the base URLs above.

## Facts the triage request relies on (check: `uv run python mubit_smoke.py`)

`mubit_smoke.py` sends the exact request `triage.prompt.build_request` builds,
through `triage.llm.client()`, plus two probes, and prints status, Mubit error
code and `x-middleware-request-id` for each. It uses its own repo name, so its
calls form their own task and never count toward a real repo's task. Write the
date and the request ids here after each check. Until then these are **unchecked**
on the starter credit:

1. `POST /v1/messages` with `output_config.format` (`json_schema`) and
   `metadata.user_id` together, model claude-sonnet-5-5: accepted, reply is JSON
   matching the schema. If refused, every triage call errors and every run scores n=0.
2. claude-sonnet-5-5 refuses forced tool choice (`tool_choice` type `tool`/`any`).
   This is why the request uses `output_config` instead of a forced tool.
3. A second credential next to the Mubit key gets `401` with code
   `extra_credential_refused` (the 401 is from the docs; the code string is what
   `triage/llm.py` and the tests quote).
4. Mubit counts `output_config.format` as the "output format" in its task identity
   (the docs say "output format" without naming the Anthropic field). There is no
   read API: confirm in the console's Tasks page that smoke calls with the same
   schema land in one task.

## Starter-credit limits observed

- OpenAI models: gpt-6-sol, gpt-6.1-sol, gpt-6-luna, gpt-6-astra, gpt-5.5, gpt-4o-mini, gpt-4.1-mini
  (plus dated variants). `text-embedding-3-small` also worked.
- Anthropic models: claude-opus-5-5, claude-sonnet-5-5, claude-haiku-4-5(-20251001).
- Gemini: gemini-3.8-flash works; `GET /gemini/v1beta/models` is refused (`starter_credit_route`).
- OpenRouter/Groq compat paths: `403 provider_key_missing` until a key is added in the console.
- `store: true` on OpenAI is refused (`starter_credit_option`). Consequence: on **Chat
  Completions** you cannot send `metadata` at all, because OpenAI requires `store`
  for it. The docs' session example (`metadata.session_id` on chat completions)
  therefore fails on the starter credit. Use the **Responses** API for
  `metadata.session_id`, or rely on Mubit's default session (instructions + first user
  message), or add your own OpenAI key.
- On Anthropic, session = `metadata.user_id`.
- Body limits on starter credit: 8 MiB, plain JSON, arrives within 60 s.

## Headers

- Every reply carries `x-middleware-request-id: mreq_...` (quote it for support).
- `x-middleware-store: false` or `x-middleware-record: metadata|off` keeps the call
  out of Requests and out of training.
- Mubit errors have `type: middleware_gateway_error` and a `code` in the provider's
  error format; anything else is the provider's own error passed through.
- `x-should-retry: false` on non-retryable Mubit errors; `retry-after` on 429/503.

## What makes a task

Same model (date suffix ignored) + same instructions/system prompt + same tools +
tool choice + output format. Per-call text (names, etc.) belongs in the user message,
not the system prompt, or each variant becomes its own task. A task trains after
~25 kept examples; test cases are held out by session.

## No read API

There is no documented HTTP endpoint for reading records, tasks or adapters.
`/v1/requests` returns `404 unknown_route`. Read-only keys exist in the console but
the docs expose no path for them. Records are viewed in the console only.
