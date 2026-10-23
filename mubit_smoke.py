"""Smoke test for the Mubit gateway using the official OpenAI and Anthropic SDKs.

Run:  set -a; source .env; set +a
      uv run --with 'openai>=1.70' --with 'anthropic>=0.50' python3 mubit_smoke.py
(The Responses API needs openai>=1.66; the system-wide openai 1.55 is too old.)
Each call prints the Mubit request ID (x-middleware-request-id) so it can be
found in the console under Requests.
"""

import os
import sys

from anthropic import Anthropic
from openai import OpenAI

KEY = os.environ.get("MUBIT_API_KEY")
if not KEY:
    sys.exit("MUBIT_API_KEY is not set (source .env first)")

INSTRUCTIONS = (
    "Sort the support message into one category: "
    "billing, bug, account or other. Reply with the category only."
)
MESSAGE = "I was charged twice for my October invoice."

failures = 0


def check(label: str, got: str, want: str = "billing") -> None:
    global failures
    ok = got.strip().lower() == want
    failures += 0 if ok else 1
    print(f"{'PASS' if ok else 'FAIL'}  {label}: {got.strip()!r}")


# --- OpenAI SDK -> Mubit ----------------------------------------------------
oai = OpenAI(base_url="https://api.mubit.ai/openai/v1", api_key=KEY)

raw = oai.chat.completions.with_raw_response.create(
    model="gpt-6-luna",
    messages=[
        {"role": "system", "content": INSTRUCTIONS},
        {"role": "user", "content": MESSAGE},
    ],
)
print("openai chat      mreq:", raw.headers.get("x-middleware-request-id"))
check("openai chat.completions", raw.parse().choices[0].message.content)

resp = oai.responses.with_raw_response.create(
    model="gpt-6-luna",
    instructions=INSTRUCTIONS,
    input=MESSAGE,
    metadata={"session_id": "smoke-session"},  # session grouping, allowed on Responses
)
print("openai responses mreq:", resp.headers.get("x-middleware-request-id"))
check("openai responses", resp.parse().output_text)

chunks = []
for chunk in oai.chat.completions.create(
    model="gpt-6-luna",
    messages=[
        {"role": "system", "content": INSTRUCTIONS},
        {"role": "user", "content": MESSAGE},
    ],
    stream=True,
):
    if chunk.choices and chunk.choices[0].delta.content:
        chunks.append(chunk.choices[0].delta.content)
check("openai stream", "".join(chunks))

# --- Anthropic SDK -> Mubit -------------------------------------------------
ant = Anthropic(base_url="https://api.mubit.ai/anthropic", api_key=KEY)

raw = ant.messages.with_raw_response.create(
    model="claude-haiku-4-5",
    max_tokens=16,
    system=INSTRUCTIONS,
    messages=[{"role": "user", "content": MESSAGE}],
    metadata={"user_id": "smoke-session"},  # Anthropic's one metadata field = session
)
print("anthropic msg    mreq:", raw.headers.get("x-middleware-request-id"))
check("anthropic messages", raw.parse().content[0].text)

with ant.messages.stream(
    model="claude-haiku-4-5",
    max_tokens=16,
    system=INSTRUCTIONS,
    messages=[{"role": "user", "content": MESSAGE}],
) as stream:
    text = "".join(stream.text_stream)
check("anthropic stream", text)

print()
print("all passed" if failures == 0 else f"{failures} failure(s)")
sys.exit(1 if failures else 0)
