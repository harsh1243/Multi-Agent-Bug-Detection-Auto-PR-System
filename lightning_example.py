"""
Minimal example: using Claude via a third-party proxy (Lightning AI) with the
official `anthropic` Python SDK.

The ONLY difference vs. calling Anthropic directly is `base_url` — point it at the
provider's endpoint and use the provider's key + whatever model it serves.

    pip install anthropic
    python lightning_example.py
"""

import anthropic

# ── 1. Third-party provider config ────────────────────────────────────────────
BASE_URL = "https://lightning.ai/"   # proxy endpoint (NOT api.anthropic.com)
API_KEY  = "sk-lit-<your-lightning-api-key>"   # keep real keys out of source control
MODEL    = "claude-opus-4-8"         # the model this teamspace serves

# ── 2. Create the client pointed at the proxy ─────────────────────────────────
client = anthropic.Anthropic(
    api_key=API_KEY,
    base_url=BASE_URL,               # <── this is the whole integration
)

# ── 3. Call it exactly like the normal Anthropic API ──────────────────────────
resp = client.messages.create(
    model=MODEL,
    max_tokens=256,
    system="You are a helpful assistant.",
    messages=[{"role": "user", "content": "In one sentence, what is a knowledge graph?"}],
    # NOTE: do NOT pass `temperature` — claude-opus-4-8 deprecates it and 400s.
)

print(resp.content[0].text)
