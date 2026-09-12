"""Routing eval part 2: the T1 candidates on role-cat and org-sectors."""
import json, sys, time
from pathlib import Path

BASE = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(BASE / "agents"))

from job_intel import JobIntel, load_archetypes
from llm import LLM, LLMError

CANDIDATES = [
    "openrouter/qwen/qwen3-30b-a3b-instruct-2507",   # T1
    "openrouter/openai/gpt-oss-20b",                 # T1
    "openrouter/openai/gpt-oss-120b",                # T2
]


def eval_sectors():
    truth = json.loads((BASE / "data/org-sectors.json").read_text())
    orgs = [o for o, v in truth.items() if v and "Unknown" not in v][:20]
    listed = "\n".join(f"- {o}" for o in orgs)
    prompt = f'''Label each company below with a short two-part sector line for a
job-tracker UI. Format exactly: "<Industry> - <Niche>".

Examples of the required style:
  Google            -> Tech - Advertising
  Anthropic         -> Tech - AI Lab
  Zocdoc            -> Healthcare - Health Tech
  Gusto             -> Tech - HR & Payroll SaaS
  Chewy             -> Retail - E-commerce
  Horizon Media     -> Media - Media Agency

Rules:
  - Keep each side to 1-3 words. No sentences, no hedging, no punctuation beyond "&".
  - If you genuinely do not recognise the company, return "Unknown - Unknown".
    Guessing wrong is worse than admitting you do not know. Do not force a label.

COMPANIES:
{listed}

Return ONLY a JSON object mapping each company name exactly as given to its sector line:
{{"Company Name": "Industry - Niche"}}'''

    print(f"== org-sectors | {len(orgs)} orgs (truth = claude)")
    for selector in CANDIDATES:
        client = LLM(BASE, model=selector, timeout=600)
        t0 = time.time()
        try:
            data = client.complete_json(prompt, tag="eval-org-sectors", force=True)
        except LLMError as exc:
            print(f"  {selector:48s} FAILED {exc}"); continue
        if not isinstance(data, dict):
            print(f"  {selector:48s} FAILED not a dict"); continue
        exact = sum(1 for o in orgs if data.get(o, "").strip() == truth[o].strip())
        industry = sum(1 for o in orgs
                       if data.get(o, "").split("-")[0].strip().lower()
                       == truth[o].split("-")[0].strip().lower())
        shape_ok = sum(1 for o in orgs if len(data.get(o, "").split("-")) == 2)
        missing = [o for o in orgs if o not in data]
        print(f"  {selector:48s} {time.time()-t0:5.1f}s  exact {exact}/{len(orgs)}  "
              f"industry {industry}/{len(orgs)}  shape {shape_ok}/{len(orgs)}  "
              f"missing {len(missing)}  ${client.stats['cost_usd']:.4f}")
        disagree = [(o, truth[o], data.get(o)) for o in orgs if data.get(o, "").strip() != truth[o].strip()]
        for row in disagree[:5]:
            print(f"      {row[0]:22s} claude={row[1]:28s} cheap={row[2]}")


def eval_rolecat():
    files = sorted((BASE / "artifacts/jobs").glob("intel-*.json"))
    intel_data = json.loads(files[-1].read_text())
    truth = {r["id"]: r.get("suggested_role_cat")
             for r in intel_data["roles"] if r.get("suggested_role_cat")}

    intel = JobIntel(BASE)
    df = intel.load_tracker()
    archetypes = load_archetypes(BASE, df)
    roles = [r for r in intel.tracker_roles(df) if r["id"] in truth][:12]
    if len(roles) < 3:
        live = [r for r in intel.tracker_roles(df)][:12]
        roles = live
        print("== role-cat | no overlap with tracker rows; skipping")
        return
    prompt = intel._cat_prompt(roles, archetypes)
    ids = [r["id"] for r in roles]
    label_by_id = {a["id"]: a["label"] for a in archetypes}

    print(f"\n== role-cat | {len(roles)} roles (truth = claude)")
    for selector in CANDIDATES:
        client = LLM(BASE, model=selector, timeout=600)
        t0 = time.time()
        try:
            data = client.complete_json(prompt, tag="eval-role-cat", force=True)
        except LLMError as exc:
            print(f"  {selector:48s} FAILED {exc}"); continue
        if not isinstance(data, list):
            print(f"  {selector:48s} FAILED not a list"); continue
        by_id = {d.get("id"): d for d in data if isinstance(d, dict)}
        agree = 0
        for rid in ids:
            got = by_id.get(rid, {})
            label = label_by_id.get(str(got.get("archetype")), "none")
            if label.strip() == str(truth[rid]).strip():
                agree += 1
        missing = [i for i in ids if i not in by_id]
        print(f"  {selector:48s} {time.time()-t0:5.1f}s  agree {agree}/{len(ids)}  "
              f"missing {len(missing)}  ${client.stats['cost_usd']:.4f}")


if __name__ == "__main__":
    eval_sectors()
    eval_rolecat()
