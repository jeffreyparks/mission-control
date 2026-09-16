# Mission Control

Agentic CareerOps :)

Run `mc setup` to enter your professional profile and goals in the `me/` folder. Add your
resume/cv and your LinkedIn PDF export. Define your top target companies, job search boards, etc.

Produces daily job search results and assessments, weekly news topics and thought-starters.

Run `mc dashboard` to browse and edit your results in the browser.

## Getting started

```bash
uv tool install --editable .    # one-time: puts the `mc` command on your PATH
mc setup                        # set up your `me/` folder and do a first run
```

`mc setup` walks you through it - drop in your resume, write a couple lines about what you're
after, and you're basically done. Everything you enter lives in `me/`: your resume, your goals,
an optional LinkedIn export. None of it gets committed to git.

Secrets (API keys, your BlueSky app password) go in `.env` instead, also gitignored. Both `me/`
and `.env` start from templates already in the repo, so a fresh clone always has something to
copy from - see `me/README.md` (created on first run) for what each file is.

Want to peek before running anything for real?

```bash
mc setup --status    # what's filled in, what's missing - no LLM calls
mc setup --dry-run   # see what a real run would scan - still no LLM calls
```

Once you're set up:

```bash
mc run                 # pulls new roles, judges fit, rebuilds the pages
open artifacts/html/index.html

mc dashboard            # optional: browse and edit your results in the browser
open http://127.0.0.1:8787
```

Skip the install and just want it running from inside the repo? `uv run mc setup` works the
same way, no PATH changes needed - the only difference is typing `uv run` first. (`setup.py`,
`run_daily.py`, and `dashboard.py` also still run directly, if you prefer that - `mc` is just a
shortcut on top.) Forgot the commands? `mc help` lists them.

## What it does

| Feed | Cadence | Output |
|---|---|---|
| **Job Tracker** | daily | Fit read per role against your resume, org directory, outcome timing |
| **Content Radar** | weekly | Editorial brief: what is worth writing about, and the angle to take |
| **Profiles** | daily | GitHub, LinkedIn, BlueSky scans |

## Using it day to day

```bash
mc run                       # normal run
mc run --force-radar         # run the weekly radar now
mc run --only jobs           # profiles | jobs | radar | synthesis | render
mc run --refresh-intel       # re-judge every role
mc run --no-intel            # legacy keyword scanner instead of LLM fit

# add one job posting from a URL
uv run add_role.py <url> [--org X] [--title Y] [--priority 1|2|3] [--notes "..."]

# history / trends
uv run python agents/history.py --show
uv run python render/build.py       # rebuild HTML only
```

Each stage runs on its own - if one fails, the rest still finish.

**Cadence:** Content Radar only runs about once a week (whenever the newest radar file is 7+
days old). Everything else runs every day.

**Found a role the scanner missed?** Add it yourself:

```bash
uv run add_role.py https://job-boards.greenhouse.io/acme/jobs/123
```

It fetches the posting (Greenhouse, Lever, Ashby, LinkedIn, or generic), runs one fit check, and
appends a row - Status `01 Open`, Source `manual-add` - then rebuilds the HTML. It won't add the
same role twice (checked by URL and by org+title), and it won't write anything at all unless the
fetch actually succeeds.

**Want to edit things by hand?** Fire up the dashboard:

```bash
mc dashboard
```

It serves your pages on `127.0.0.1:8787` with a small edit API. Status, Priority, Role Cat,
Outcomes, Notes, and Salary are all editable right there - change one and it's saved to the
database and the spreadsheet immediately. `Recommendation` is the model's call, not yours to
overwrite, so it's shown as a plain badge instead.

No server running? The page still opens fine, just read-only - nothing breaks. And it only
listens on your own machine, with a fixed list of fields and values it will accept - no
free-for-all editing from a stray request.

One thing to remember: **restart the dashboard after any code change.** It doesn't hot-reload,
so a long-lived process keeps serving whatever code was current when it started, until you stop
it and start it again. If something looks stale, check `/api/health` - `server_commit` tells you
which commit it's actually running, so you can compare it to `git rev-parse --short HEAD`.

## Automation

Want this running on its own every morning?

```cron
0 6 * * * cd /Users/jeff/Dev/jeffreyparks/mission-control && mc run
```

## Architecture

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

### Data model

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

### Role categories

The system proposes an archetype for uncategorised roles and writes it to
**`Role Cat (suggested)`**. Your manual `Role Cat` column is never written. Suggestions render
dimmed with a dashed "suggested" chip so a proposal never reads as a fact. When you fill in
`Role Cat` yourself, the suggestion is cleared.

"none" is an accepted answer. In the current run, 23 of 60 uncategorised roles got a suggestion
and 37 were left unmapped rather than forced.

### History

`data/history.db` snapshots every run and computes deltas: new and disappeared roles, fit score
movement, verdict changes, status changes, and LinkedIn coverage movement. The daily brief opens
with a "What changed" section. With a single snapshot it says so plainly instead of inventing
movement.

### Tracker

`artifacts/jobs/org-roles-tracker.xlsx` is the source of record. It is backed up before every
write. Your manual columns (Role Cat, Priority, Status, Outcomes, Notes) are asserted unchanged
in code after each update.

Columns added by the system: Fit Score, Fit Rationale, Recommendation, Sector, Days To Outcome.

### Layout

```
src/mission_control/
  __init__.py         `mc` CLI - thin dispatcher to setup.py/run_daily.py/dashboard.py
setup.py               scaffold me/, first run
run_daily.py            the daily/weekly pipeline
dashboard.py            editable local dashboard (formerly serve.py)
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
