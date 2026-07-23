#!/usr/bin/env python3
"""Temporary standalone diagnostic — NOT part of the pipeline. Isolates the
exact client.messages.create() calls that are failing in production, with
full exception diagnostics (type, status_code, body), without paying the
cost of a full 6000-company scrape. Delete after use."""

import os
import traceback

import anthropic

client = anthropic.Anthropic(api_key=os.environ["ANTHROPIC_API_KEY"])

VERIFY_MODEL = "claude-haiku-4-5-20251001"
CLAUDE_MODEL = "claude-sonnet-4-6"
THINKING_DISABLED = {"type": "disabled"}


def diag_call(label, **kwargs):
    print(f"\n=== {label} ===")
    try:
        msg = client.messages.create(**kwargs)
        print("SUCCESS")
        print("stop_reason:", getattr(msg, "stop_reason", "?"))
        print("content blocks:", len(msg.content) if msg.content else 0)
        if msg.content:
            for i, block in enumerate(msg.content):
                print(f"  block[{i}] type={getattr(block, 'type', '?')!r}")
                if hasattr(block, "text"):
                    print(f"  block[{i}].text (first 300 chars) = {block.text[:300]!r}")
        print("usage:", getattr(msg, "usage", "?"))
    except Exception as e:
        print("FAILED")
        print("exception type:", type(e).__name__)
        print("exception module:", type(e).__module__)
        print("str(e):", str(e))
        print("status_code:", getattr(e, "status_code", None))
        print("body:", getattr(e, "body", None))
        print("response:", getattr(e, "response", None))
        print("request:", getattr(e, "request", None))
        print("--- full traceback ---")
        traceback.print_exc()


# 1. Exact shape of verify_fact()'s call, with a real short prompt
diag_call(
    "verify_fact-shaped call (VERIFY_MODEL, small prompt)",
    model=VERIFY_MODEL,
    max_tokens=200,
    thinking=THINKING_DISABLED,
    messages=[{"role": "user", "content": 'Respond with ONLY valid JSON: {"verified": true, "reason": "test"}'}],
)

# 2. Same VERIFY_MODEL call WITHOUT thinking param at all, to see if that changes anything
diag_call(
    "verify_fact-shaped call (VERIFY_MODEL, NO thinking param)",
    model=VERIFY_MODEL,
    max_tokens=200,
    messages=[{"role": "user", "content": 'Respond with ONLY valid JSON: {"verified": true, "reason": "test"}'}],
)

# 3. pick_best_founder-shaped call (CLAUDE_MODEL, small prompt) -- this one reportedly succeeds in production
diag_call(
    "pick_best_founder-shaped call (CLAUDE_MODEL, small prompt)",
    model=CLAUDE_MODEL,
    max_tokens=200,
    thinking=THINKING_DISABLED,
    messages=[{"role": "user", "content": 'Respond with ONLY valid JSON: {"founder": "", "reason": "test"}'}],
)

# 4. rank_top_leads-shaped call (CLAUDE_MODEL, LARGE prompt) -- this one reportedly fails in production
big_prompt = "You are triaging leads.\n\nLeads:\n" + "\n".join(
    f"- role_url: https://example.com/jobs/{i}\n  Company: Company{i}\n  Product: widgets that do things, a fairly long description repeated several times to pad this out. " * 3
    for i in range(17)
) + '\n\nRespond with ONLY valid JSON: {"top_picks": [{"role_url": "https://example.com/jobs/1", "reason": "test"}]}'
print(f"\n(big_prompt length: {len(big_prompt)} chars)")
diag_call(
    "rank_top_leads-shaped call (CLAUDE_MODEL, large prompt)",
    model=CLAUDE_MODEL,
    max_tokens=400,
    thinking=THINKING_DISABLED,
    messages=[{"role": "user", "content": big_prompt}],
)

# 5. generate_message-shaped call (CLAUDE_MODEL, medium prompt) -- reportedly succeeds
diag_call(
    "generate_message-shaped call (CLAUDE_MODEL, medium prompt)",
    model=CLAUDE_MODEL,
    max_tokens=400,
    thinking=THINKING_DISABLED,
    messages=[{"role": "user", "content": "Write one short sentence introducing yourself as Rajat."}],
)

print("\n\nanthropic SDK version:", anthropic.__version__)
