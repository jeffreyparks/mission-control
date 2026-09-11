# Mission Control

Career operations automation. Two feeds, separate cadences, judged by the same standard.

Output is three static HTML pages. No server, no dashboard process.

```bash
uv run run_daily.py                  # respects cadences, rebuilds HTML
open artifacts/html/index.html
```

## What it does

| Feed | Cadence | Output |
|---|---|---|
| **Job Tracker** | daily | Fit read per role against your resume, org directory, outcome timing |
| **Content Radar** | weekly | Editorial brief: what is worth writing about, and the angle to take |
| **Profiles** | daily | GitHub, LinkedIn, BlueSky scans |

### Analysis model

Deterministic where lexical matching is genuinely correct, LLM where judgment is required.

- **Keyword coverage is used for LinkedIn only.** LinkedIn search and ATS parsing are literally
  lexical, so the metric is meaningful there. It is not used anywhere else.
- **Everything else uses LLM inference** against ground truth: `config/career-goals.md` plus your
  parsed resume.

Prompts explicitly reward negative and empty verdicts. "Nothing this week", "no change", and
"skip" are correct answers. A recent run returned 78 skips out of 93 roles, zero network signal,
and 10 of 12 repos as no-change. That is the system working.

### LLM layer

`agents/llm.py` shells out to the local `claude` CLI in headless mode.
**No API key and no external service.** Results are cached by content hash, so re-runs on
unchanged input are free. Agents batch 15-20 items per call.

Typical cost: about $1.60 for a cold job-intel run, $0.59 for a cold radar run, $0.00 cached.

### Incremental fit verdicts

Job roles carry a `fit_fingerprint` covering the role content, the prompt text, and
`PROMPT_VERSION`. Unchanged roles reuse their stored verdict; only new or edited roles reach
the model. A normal run judges 1 role and reuses 92.

The fingerprint hashes the prompt source itself, so editing the fit prompt invalidates stored
verdicts automatically. `PROMPT_VERSION` remains as a manual override.

`--refresh-intel` forces a full re-judge. `--no-intel` falls back to the legacy keyword scanner.

## Layout

```
agents/
  llm.py              LLM client, hash-cached
  context.py          ground truth: career goals + resume
  job_intel.py        fit analysis, org enrichment, outcome timing   (daily)
  content_radar.py    full-article fetch + editorial brief           (weekly)
  job_scanner.py      Greenhouse / Lever fetchers (used by job_intel)
  profile_scanner.py  linkedin_scanner.py  bluesky_scanner.py
  synthesizer.py
render/
  build.py            renders all three pages
  theme.css           shared design system
  index / radar / tracker .html.j2
config/
  career-goals.md         positioning + keywords  (ground truth)
  content-sources.yaml    RSS feeds
  job-sources.yaml        target companies + rules
data/
  resumes/*.pdf           parsed into LLM context automatically
  linkedin/profile.pdf    LinkedIn export
  llm-cache/              hash-keyed response cache
artifacts/
  html/                   the three rendered pages
  jobs/                   org-roles-tracker.xlsx + intel-*.json + backups
  content/                radar-*.json + .md
  profiles/  synthesis/
attic/                    retired Streamlit dashboard and one-off scripts
```

## Commands

```bash
uv run run_daily.py                 # normal run
uv run run_daily.py --force-radar   # run the weekly radar now
uv run run_daily.py --only jobs     # profiles | jobs | radar | synthesis | render
uv run run_daily.py --refresh-intel # re-judge every role
uv run run_daily.py --no-intel      # legacy keyword scanner instead of LLM fit

# add one job posting from a URL
uv run add_role.py <url> [--org X] [--title Y] [--priority 1|2|3] [--notes "..."]

# history / trends
uv run python agents/history.py --show
uv run python render/build.py       # rebuild HTML only
```

Stages are isolated. One failure does not stop the run.

## Cadence rule

Content Radar runs only when the newest `artifacts/content/radar-*.json` is 7 or more days old.
Everything else runs daily.

## Adding a role

```bash
uv run add_role.py https://job-boards.greenhouse.io/acme/jobs/123
```

Fetches the posting (Greenhouse, Lever, Ashby, LinkedIn, or generic), runs one fit analysis,
appends a row with Status `01 Open` and Source `manual-add`, then rebuilds the HTML.

Refuses duplicates by canonical URL (tracking parameters stripped) and by org+title. Nothing is
written until the fetch succeeds, so a bad URL never leaves a partial row. Both failure paths
exit 1.

## Role categories

The system proposes an archetype for uncategorised roles and writes it to
**`Role Cat (suggested)`**. Your manual `Role Cat` column is never written. Suggestions render
dimmed with a dashed "suggested" chip so a proposal never reads as a fact. When you fill in
`Role Cat` yourself, the suggestion is cleared.

"none" is an accepted answer. In the current run, 23 of 60 uncategorised roles got a suggestion
and 37 were left unmapped rather than forced.

## History

`data/history.db` snapshots every run and computes deltas: new and disappeared roles, fit score
movement, verdict changes, status changes, and LinkedIn coverage movement. The daily brief opens
with a "What changed" section. With a single snapshot it says so plainly instead of inventing
movement.

## Tracker

`artifacts/jobs/org-roles-tracker.xlsx` is the source of record. It is backed up before every
write. Your manual columns (Role Cat, Priority, Status, Outcomes, Notes) are asserted unchanged
in code after each update.

Columns added by the system: Fit Score, Fit Rationale, Recommendation, Sector, Days To Outcome.

## Automation

```cron
0 6 * * * cd /Users/jeff/Dev/jeffreyparks/mission-control && uv run run_daily.py
```
