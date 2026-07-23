#!/usr/bin/env python3
"""Temporary standalone preview — NOT part of the pipeline. Runs the real
JD fetch + Exa research + generate_message() against a few real companies
already known from today's digests, so the new Day 1/Day 90 message shape
can be reviewed before spending a full pipeline run + real email on it.
Does not touch seen_jobs.json or send anything. Delete after use."""

import yc_gtm_monitor as ycm
import anthropic
import os

client = anthropic.Anthropic(api_key=os.environ["ANTHROPIC_API_KEY"])

# Two single-founder companies (guaranteed "researched" tier if Exa finds
# anything verifiable) and one multi-founder, no-distinguishing-signal
# company (forced "jd_only" tier) for contrast.
CASES = [
    {
        "founder_name": "Thomas Aubry",
        "company_name": "Ooak Data",
        "yc_batch": "S26",
        "role_title": "Founding Marketing Lead",
        "role_url": "https://www.ycombinator.com/companies/ooak-data/jobs/atIhXqo-founding-marketing-lead",
        "product_summary": "We build the world's largest library of real-world business workflow data",
        "force_jd_only": False,
    },
    {
        "founder_name": "Priya Khandelwal",
        "company_name": "Nixo",
        "yc_batch": "S25",
        "role_title": "Founding GTM Lead",
        "role_url": "https://www.ycombinator.com/companies/nixo/jobs/1dCiXae-founding-gtm-lead",
        "product_summary": "The first ops platform for forward deployed engineers",
        "force_jd_only": False,
    },
    {
        "founder_name": "Clint Burgess",
        "company_name": "CharacterQuilt",
        "yc_batch": "P26",
        "role_title": "Go-to-Market Engineer",
        "role_url": "https://www.ycombinator.com/companies/characterquilt/jobs/NkPK05W-go-to-market-engineer",
        "product_summary": "Computer use agents to deploy enterprise marketing campaigns",
        "force_jd_only": True,
    },
]

for case in CASES:
    print(f"\n{'=' * 70}\n{case['founder_name']} @ {case['company_name']} — {case['role_title']}\n{'=' * 70}")

    jd_text = ycm.fetch_job_description(case["role_url"])
    print(f"[JD fetched: {len(jd_text)} chars]")

    research = []
    if not case["force_jd_only"]:
        research = ycm.research_and_verify_founder(
            client, case["founder_name"], case["company_name"], case["product_summary"], jd_text
        )
        print(f"[Research: {len(research)} verified fact(s)]")

    message, basis, grounded_in = ycm.generate_message(
        client, case["founder_name"], case["company_name"], case["yc_batch"],
        case["role_title"], case["product_summary"], jd_text=jd_text, research=research,
    )

    print(f"\nMessage basis: {basis}")
    if grounded_in:
        print(f"Pilot angle grounded in: {grounded_in}")
    print(f"\nMessage:\n{message}")
