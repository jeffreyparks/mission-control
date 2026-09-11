
import sys, json
from pathlib import Path
sys.path.insert(0, "agents")
from llm import LLM
from context import context_block

base = Path(".")
ctx = context_block(base)
print("context chars:", len(ctx))

prompt = f"""{ctx}

=== TASK ===
Assess fit for this role. Be blunt. Under-claim rather than over-claim.

ROLE: Staff Data Scientist, Forecasting @ Pinterest

Return ONLY valid JSON:
{{"fit_score": <0-100 int>, "seniority_read": "<one line>", "why": "<2 sentences>", "top_gaps": ["<gap>", "<gap>"]}}
"""

llm = LLM(base)
out = llm.complete_json(prompt, tag="smoke-fit")
print(json.dumps(out, indent=2))
print(llm.report())

out2 = llm.complete_json(prompt, tag="smoke-fit")   # should hit cache
print("cache check ->", llm.report())
