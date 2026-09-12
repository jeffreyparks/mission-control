"""Routing eval: do cheap models match the frontier verdicts on real prompts?"""
import json, sys, time
from pathlib import Path

BASE = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(BASE / "agents"))

from job_intel import JobIntel
from llm import LLM, LLMError

CANDIDATES = [
    "openrouter/openai/gpt-oss-120b",                   # T2
    "openrouter/z-ai/glm-4.6",                          # T2
]
N_ROLES = int(sys.argv[1]) if len(sys.argv) > 1 else 8
OFFSET = int(sys.argv[2]) if len(sys.argv) > 2 else 0

REQUIRED = {"id", "fit_score", "seniority_read", "why", "top_gaps",
            "what_theyre_really_hiring_for", "pitch_angle", "recommendation"}


def truth_map():
    files = sorted((BASE / "artifacts/jobs").glob("intel-*.json"))
    data = json.loads(files[-1].read_text())
    out = {}
    for role in data.get("roles", []):
        if role.get("fit_score") is not None:
            out[role["id"]] = role
    return out, files[-1].name


def main():
    truth, truth_file = truth_map()
    intel = JobIntel(BASE)
    df = intel.load_tracker()
    roles = [r for r in intel.tracker_roles(df) if r["id"] in truth][OFFSET:OFFSET + N_ROLES]
    if len(roles) < 3:
        print("not enough overlapping roles"); return 1

    prompt = intel._fit_prompt(roles)
    ids = [r["id"] for r in roles]
    print(f"ground truth: {truth_file} | {len(roles)} roles | prompt {len(prompt)} chars\n")

    results = {}
    for selector in CANDIDATES:
        client = LLM(BASE, model=selector, timeout=900)
        started = time.time()
        try:
            data = client.complete_json(prompt, tag="eval-job-fit", force=True)
        except LLMError as exc:
            print(f"{selector:50s} FAILED {exc}"); results[selector] = None; continue
        secs = time.time() - started

        if not isinstance(data, list):
            print(f"{selector:50s} FAILED not a list"); results[selector] = None; continue
        by_id = {d.get("id"): d for d in data if isinstance(d, dict)}

        missing_ids = [i for i in ids if i not in by_id]
        bad_schema = [i for i, d in by_id.items() if not REQUIRED.issubset(d)]
        deltas, rec_match = [], 0
        for rid in ids:
            got, want = by_id.get(rid), truth[rid]
            if not got or not isinstance(got.get("fit_score"), int):
                continue
            deltas.append(abs(got["fit_score"] - want["fit_score"]))
            if got.get("recommendation") == want.get("recommendation"):
                rec_match += 1
        mae = sum(deltas) / len(deltas) if deltas else None
        results[selector] = {
            "secs": round(secs, 1), "scored": len(deltas), "mae": mae,
            "rec_agree": f"{rec_match}/{len(ids)}",
            "missing": len(missing_ids), "bad_schema": len(bad_schema),
            "cost": round(client.stats["cost_usd"], 4),
            "sample": {rid: (by_id[rid]["fit_score"] if rid in by_id else None) for rid in ids},
        }
        r = results[selector]
        print(f"{selector:50s} {r['secs']:6.1f}s  MAE {mae if mae is None else round(mae,1):>5}  "
              f"rec {r['rec_agree']}  missing {r['missing']}  schema-bad {r['bad_schema']}  ${r['cost']}")

    print("\ntruth scores: ", {rid: truth[rid]["fit_score"] for rid in ids})
    for sel, r in results.items():
        if r:
            print(f"{sel.split('/')[-1]:38s}", r["sample"])
    (BASE / f"attic/eval-routing-{OFFSET}.json").write_text(json.dumps(
        {"ids": ids, "truth": {i: truth[i] for i in ids}, "results": results}, indent=2))
    return 0


if __name__ == "__main__":
    sys.exit(main())
