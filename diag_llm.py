"""Diagnose the LLM (Lightning AI) connection — prints the FULL error message.

Run:  python diag_llm.py
It loads app/.env, then makes several single calls, varying one parameter at a
time, so we can see exactly which request shape the proxy rejects (400).
"""

import os
import sys
from pathlib import Path

APP = Path(__file__).parent
sys.path.insert(0, str(APP / "backend"))

# ── load .env ──
env = APP / ".env"
if env.exists():
    for line in env.read_text(encoding="utf-8").splitlines():
        line = line.strip()
        if line and not line.startswith("#") and "=" in line:
            k, _, v = line.partition("=")
            os.environ[k.strip()] = v.strip().strip('"').strip("'")

import anthropic  # noqa: E402

base = os.environ.get("ANTHROPIC_BASE_URL") or None
key = os.environ.get("ANTHROPIC_API_KEY") or ""
model = os.environ.get("CLAUDE_MODEL_PRIMARY", "claude-opus-4-8")

print(f"anthropic SDK   = {anthropic.__version__}")
print(f"ANTHROPIC_BASE_URL = {base}")
print(f"model           = {model}")
print(f"key             = {key[:14]}...{key[-10:]}" if key else "key = (missing)")
print("=" * 70)

kwargs = {"api_key": key}
if base:
    kwargs["base_url"] = base
client = anthropic.Anthropic(**kwargs)


def attempt(label: str, **call_kwargs) -> None:
    print(f"\n### {label}")
    try:
        r = client.messages.create(**call_kwargs)
        text = r.content[0].text if r.content else "(empty)"
        print("  OK ->", text[:200])
    except Exception as e:
        print("  ERROR ->", repr(e)[:2000])


MSG = [{"role": "user", "content": "Reply with the single word: ok"}]

attempt("1. minimal (max_tokens=64, no system, no temperature)",
        model=model, max_tokens=64, messages=MSG)

attempt("2. with system param",
        model=model, max_tokens=64, system="You are terse.", messages=MSG)

attempt("3. max_tokens=1024",
        model=model, max_tokens=1024, messages=MSG)

attempt("4. max_tokens=4096",
        model=model, max_tokens=4096, messages=MSG)

attempt("5. temperature=0.0",
        model=model, max_tokens=64, temperature=0.0, messages=MSG)

print("\n" + "=" * 70)
print("Done. Paste this whole output back.")
