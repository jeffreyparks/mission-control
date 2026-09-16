# Mission Control

Career operations automation. Two feeds, separate cadences, judged by the same standard.

Output is three static HTML pages. The source of record is a SQLite database; the Excel
tracker is exported on every run and stays a real physical backup.

## Getting started

```bash
uv run setup.py                      # one command: scaffold me/, enter your basics, first run
```

`me/` is your single input folder - resume, target roles, optional LinkedIn export. `.env`
holds secrets (API keys, BlueSky app password). Both are gitignored; `templates/me/` and
`.env.example` ship in the repo so a fresh checkout always has something to copy from. See
`me/README.md` (created on first run) for what each file does.

```bash
uv run setup.py --status             # what's configured, no prompts, no LLM calls
uv run setup.py --dry-run            # preview scan scope before spending anything
```

Once `me/` and `.env` are filled in:

```bash
uv run run_daily.py                  # respects cadences, rebuilds HTML
open artifacts/html/index.html       # read-only

uv run serve.py                      # optional: editable dashboard on localhost
open http://127.0.0.1:8787/job-tracker.html
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
- **Everything else uses LLM inference** against ground truth: `me/profile.md` plus your
  parsed resume.

Prompts explicitly reward negative and empty verdicts. "Nothing this week", "no change", and
"skip" are correct answers. A recent run returned 78 skips out of 93 roles, zero network signal,
and 10 of 12 repos as no-change. That is the system working.

### LLM layer

`agents/llm.py` speaks to two backends and picks one per call:

- **Claude**, via either the local `claude` CLI (subscription, no API key) or the Anthropic API
  directly (`ANTHROPIC_API_KEY`, billed per token) - choose explicitly with `LLM_BACKEND=cli|api`
  in `.env`; default is `cli`
- **OpenRouter** for cheap open-weight models (`OPENROUTER_API_KEY` in `.env`)

`agents/routing.py` decides, reading the machine-wide compute-routing policy at
`~/.prime/agent/skills/compute-routing/config.toml`. `config/model-routing.toml` maps each call
to a tier and is versioned with the pipeline. Every run logs its routing:

```
route: job-fit        -> claude CLI default
route: role-cat       -> qwen/qwen3-30b-a3b-instruct-2507
route: org-sectors    -> qwen/qwen3-30b-a3b-instruct-2507
route: content-radar  -> openai/gpt-oss-120b
```

**Fit judgement deliberately stays on claude.** Measured against existing claude verdicts over
18 real roles, the best cheap model drifted 6.8 points and flipped 1 recommendation in 6. That
is too much drift for the output you act on. The mechanical calls are routed; the judgement is
not. One line in `config/model-routing.toml` flips it.

Cheap answers are validated before use. Wrong shape, dropped roles, a score outside 0-100 or an
invented recommendation are rejected, retried once with a stricter prompt, then escalated up the
tier ladder to `claude-opus-5`. The worst case is a slower call, not a bad verdict.

Results are cached by content hash, so re-runs on unchanged input are free. Agents batch 15-20
items per call. Typical cost: about $1.60 for a cold job-intel run, $0.59 for a cold radar run,
$0.00 cached.

### Incremental fit verdicts

Job roles carry a `fit_fingerprint` covering the role content, the prompt text, and
`PROMPT_VERSION`. Unchanged roles reuse their stored verdict; only new or edited roles reach
the model. A normal run judges 1 role and reuses 92.

The fingerprint hashes the prompt source itself, so editing the fit prompt invalidates stored
verdicts automatically. `PROMPT_VERSION` remains as a manual override.

`--refresh-intel` forces a full re-judge. `--no-intel` falls back to the legacy keyword scanner.

## Data model

`data/mission-control.db` is the source of record. `artifacts/jobs/org-roles-tracker.xlsx` is
re-exported on every write, so the spreadsheet remains a physical backup you can open or restore
from.

Hand-editing the spreadsheet is still legal. The next run notices the file changed and imports
your edits before it does anything else, logged as actor `xlsx-edit`.

Every write is recorded in a `changes` table with the field, old value, new value, actor and
timestamp:

```
2026-09-12T13:12:41  anthropic-customer-...  status  00 New find -> 02 Researching  (dashboard)
```

Live finds from the daily scan are appended automatically as status `00 New find`, so the tracker
records everything the pipeline has seen. Manual columns are left blank for you, and rows that
already exist are asserted byte-identical. Set `AUTO_ADD_LIVE = False` in `agents/job_intel.py`
to keep the tracker hand-curated.

## Editable dashboard (optional)

```bash
uv run serve.py
```

Serves `artifacts/html/` on `127.0.0.1:8787` and exposes a small JSON API. `Status`, `Priority`,
`Role Cat`, `Outcomes`, `Notes`, and `Salary` are all editable from the dashboard; changing one
writes to the database and re-exports the spreadsheet immediately. `Recommendation` is shown as
a plain badge, not editable - it is the model's fit judgement, not a status you set.

The page is a static file first. Opened with `file://`, or with the server off, `/api/health`
simply fails and the table stays read-only. Nothing breaks.

Limits are deliberate: loopback only, a closed list of editable fields (or, for Notes/Salary, a
length cap instead of a vocabulary), and a closed vocabulary of accepted values everywhere else.

**Restart `serve.py` after any code change.** Python does not hot-reload a running process, so
a long-lived `serve.py` keeps serving whatever code was current when it started - a new field,
a bug fix, anything - until you stop it and run it again. `/api/health`'s `server_commit` shows
which commit the running process actually started from; compare it to `git rev-parse --short
HEAD` if the dashboard seems to be missing something you know shipped.

## Layout

```
agents/
  llm.py              LLM client, hash-cached, routed
  routing.py          tag -> tier -> model selector
  store.py            SQLite source of record + xlsx export
  context.py          ground truth: me/profile.md + me/resume
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
  content-sources.yaml    RSS feeds (project defaults)
  job-sources.yaml        target companies + rules (project defaults)
  model-routing.toml      tag -> tier map, versioned with the pipeline
me/                        your single input folder - gitignored, local only
  profile.md               positioning + keywords  (ground truth)
  resume.pdf / resume.txt  parsed into LLM context automatically
  linkedin/                optional LinkedIn export
templates/me/               placeholder scaffold for me/, tracked in git
.env                        secrets (API keys, BlueSky password) - gitignored, see .env.example
data/
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
