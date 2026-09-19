# Mission Control

Agentic CareerOps :)

Run `mc setup` to enter your professional profile and goals in the `me/` folder. Add your
resume/cv and your LinkedIn PDF export. Define your top target companies, job search boards, etc.

Produces daily job search results and assessments, weekly news topics and thought-starters.

Run `mc dashboard` to browse and edit your results in the browser (runs in the
background; `mc dashboard stop` shuts it down).

## Getting started

```bash
uv tool install --editable . --overrides tool-overrides.txt    # one-time: puts `mc` on your PATH
mc setup                        # set up your `me/` folder and do a first run
```

`--overrides tool-overrides.txt` is needed because `python-jobspy` pins an old numpy and caps
pandas below 3, while using only three functions that are stable in both. `uv sync` and `uv run`
pick the same overrides up from `pyproject.toml` automatically; `uv tool install` does not read
that table, so it is passed explicitly. Skip it and `mc` installs without JobSpy, which shows up
at scan time as `jobspy: python-jobspy is not installed`.

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
open workspace/default/artifacts/html/index.html

mc dashboard            # optional: browse and edit your results (runs in the background)
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

### Where roles come from

Company boards (Greenhouse, Lever, Ashby) are scanned per company from `job-sources.yaml`.
Two board-wide aggregators cover everything else, configured under `aggregators:`:

- **Built In** - one platform, many employers, searched by query or category.
- **JobSpy** - Indeed and LinkedIn by default, via [python-jobspy](https://github.com/speedyapply/JobSpy).
  Indeed is unrestricted; LinkedIn works without proxies as long as the volume stays low, which
  is why `results_wanted` is small and `delay_seconds` sits between queries. A board that starts
  returning 429s is dropped for the rest of that run and whatever it already found is kept.
  Glassdoor and ZipRecruiter can be added to `sites:` at your own risk.

### Keywords live in one place

`me/profile.md`, and nowhere else:

- **`## Target Keywords`** - what you want. Each match anywhere in the job **title or
  description** raises a role's score, and each term becomes a search on Indeed, LinkedIn and
  Built In (multi-word terms are quoted for an exact match).
  Missing them all does not disqualify a role; it just scores low. `rules.min_match_score` in
  `job-sources.yaml` is the only threshold - set it to `0` to send everything to the LLM.
- **`## Exclude Keywords`** - what you never want. A match in the job **title only** (never the
  description) drops the posting immediately: it never enters the tracker and never costs an LLM
  call. Each term is also sent to the boards as `-term` so the noise is filtered before it is
  downloaded.

Want the boards' full query syntax instead? Set `search_query` on the JobSpy aggregator and it
is sent verbatim.

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
appends a row - Status `01 Open`, Source `Manual` - then rebuilds the HTML. It won't add the
same role twice (checked by URL and by org+title), and it won't write anything at all unless the
fetch actually succeeds.

**Want to edit things by hand?** Fire up the dashboard:

```bash
mc dashboard          # starts in the background, prints the URL, hands your terminal back
mc dashboard stop     # shuts it down
```

It serves your pages on `127.0.0.1:8787` with a small edit API. Status, Priority, Role Cat,
Outcomes, Notes, and Salary are all editable right there - change one and it's saved to the
database immediately. `Recommendation` is the model's call, not yours to
overwrite, so it's shown as a plain badge instead.

Running it again while it's already up just tells you it's already running, not a second copy.
Prefer to watch it run in this terminal instead (Ctrl-C to stop)? `mc dashboard --foreground`.
Background output goes to `.mc/dashboard.log`.

No server running? The page still opens fine, just read-only - nothing breaks. And it only
listens on your own machine, with a fixed list of fields and values it will accept - no
free-for-all editing from a stray request.

One thing to remember: **restart the dashboard after any code change** (`mc dashboard stop` then
`mc dashboard`). It doesn't hot-reload, so a long-lived process keeps serving whatever code was
current when it started, until you stop it and start it again. If something looks stale, check
`/api/health` - `server_commit` tells you which commit it's actually running, so you can compare
it to `git rev-parse --short HEAD`.

## Automation

Want this running on its own every morning?

```cron
0 6 * * * cd /path-to-this-repo/mission-control && mc run
```

## Architecture

### Analysis model

Deterministic where lexical matching is genuinely correct, LLM where judgment is required.

- **Keyword coverage is used for board search and LinkedIn only.** Board queries and ATS
  parsing are literally lexical, so the metric is meaningful there. It decides what gets
  *fetched*, never whether a role is a good fit.
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

`agents/routing.py` decides, from two files this project owns - no machine-wide or external
policy is consulted:

| file | says | example |
|---|---|---|
| `.env` | **which model** each tier is, beside the keys that pay for them | `MC_MODEL_T1=openrouter/qwen/qwen3-30b-a3b-instruct-2507` |
| `config/model-routing.toml` | **which tier** a call starts at, versioned with the pipeline | `"job-fit" = "T3"` |

```bash
MC_TIER_ORDER=T1,T2,T3,T4
MC_MODEL_T1=openrouter/qwen/qwen3-30b-a3b-instruct-2507
MC_MODEL_T2=openrouter/openai/gpt-oss-120b
MC_MODEL_T3=claude-sonnet-5
MC_MODEL_T4=claude-opus-5
```

Costs are reported per run. The claude CLI and OpenRouter each report their own real spend; the
Anthropic API returns token counts but no price, so those calls are costed from the `[prices]`
table in `config/model-routing.toml` (list prices, edited when the rate card changes). A model
with no entry is reported as unpriced rather than silently counted as free.

One model per tier is the simple case; a comma separated list gives more than one attempt inside
a tier. Model names are transport agnostic: an `openrouter/` prefix goes to OpenRouter, anything
else is a Claude model reached by CLI or API according to `LLM_BACKEND`. So `LLM_BACKEND` chooses
**who bills you, never which model answers.** Every run logs its routing:

```
route: job-fit        -> claude-sonnet-5
route: role-cat       -> qwen/qwen3-30b-a3b-instruct-2507
route: org-sectors    -> qwen/qwen3-30b-a3b-instruct-2507
route: content-radar  -> openai/gpt-oss-120b
```

**Fit judgement deliberately stays on Claude.** Measured against existing claude verdicts over
18 real roles, the best cheap model drifted 6.8 points and flipped 1 recommendation in 6. That
is too much drift for the output you act on. So `job-fit` starts at the frontier tier while the
mechanical calls start cheap. One line in `config/model-routing.toml` changes that.

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

`workspace/<name>/data/mission-control.db` is the only source of record. Edits go through the
dashboard, which writes straight to it.

Because one SQLite file holds everything, every run takes two rotating copies, newest 7 kept:

| copy | where | why |
|---|---|---|
| `data/backups/mission-control-*.db` | before the run writes | restore the record itself; taken with SQLite's backup API, so it is safe under WAL |
| `artifacts/jobs/org-roles-tracker-*.csv` | at the end of the run | plain text, readable without this tool - or any tool |

There used to be an Excel tracker, re-exported on every write and imported back at the start of
each run so hand edits were not lost. The dashboard owns those edits now, so the spreadsheet and
its round trip are gone. Any `.xlsx` left in a workspace is inert and can be deleted.

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

`workspace/<name>/data/history.db` snapshots every run and computes deltas: new and disappeared roles, fit score
movement, verdict changes, status changes, and LinkedIn coverage movement. The daily brief opens
with a "What changed" section. With a single snapshot it says so plainly instead of inventing
movement.

### Tracker

`workspace/<name>/data/mission-control.db` is the source of record. It is backed up before every
run. Your manual columns (Role Cat, Priority, Status, Outcomes, Notes) are asserted unchanged
in code after each update.

Columns added by the system: Fit Score, Fit Rationale, Recommendation, Sector, Days To Outcome.

**Live scan size.** Each daily run judges at most `rules.max_live_roles` freshly-scraped postings
(default 200) - the knob that caps how many LLM fit calls one run can spend. Set it in
`workspace/<name>/config/job-sources.yaml`, or override a single run with `--max-live N` on
`mc run` / `run_daily.py`. `add_role.py` defaults this to `0` (add one role, do not rescan every
board) regardless of the config; pass `--max-live N` there to opt into a scan too.

### Layout

Code and app config live in the repo. **All user data lives in `workspace/<name>/`** -
one directory per person, so a whole dataset is a single swappable unit.

```
src/mission_control/
  __init__.py         `mc` CLI - thin dispatcher to setup.py/run_daily.py/dashboard.py
workspace.py            resolves --user NAME -> workspace/NAME (one person's data)
setup.py               scaffold a workspace's me/, first run
run_daily.py            the daily/weekly pipeline
dashboard.py            editable local dashboard (formerly serve.py)
agents/
  llm.py              LLM client, hash-cached, routed
  routing.py          tag -> tier (config) -> model (.env)
  store.py            SQLite source of record + db backup + csv export
  context.py          ground truth: me/profile.md + me/resume
  job_intel.py        fit analysis, org enrichment, outcome timing   (daily)
  content_radar.py    full-article fetch + editorial brief           (weekly)
  job_scanner.py      Greenhouse / Lever fetchers (used by job_intel)
  profile_scanner.py  linkedin_scanner.py  bluesky_scanner.py
  synthesizer.py
render/
  build.py            renders all three pages for one workspace
  theme.css           shared design system
  index / radar / tracker .html.j2
config/                     STARTER TEMPLATES, tracked in git
  content-sources.yaml    example RSS feeds       -> seeded into each workspace
  job-sources.yaml        example companies+rules -> seeded into each workspace
  model-routing.toml      tag -> tier map (models live in .env)
templates/me/               placeholder scaffold for a workspace's me/, tracked in git
.env                        secrets (API keys, BlueSky password) - gitignored, see .env.example
.mc/                        mc dashboard runtime state (pidfile, log) - gitignored
logs/                       daily run logs - gitignored
attic/                    retired Streamlit dashboard and one-off scripts

workspace/                  ALL user data - gitignored in full, never upstream
  default/                    your data (the default workspace)
    me/                       your single input folder
      profile.md                positioning + keywords  (ground truth)
      resume.pdf / resume.txt   parsed into LLM context automatically
      linkedin/                 optional LinkedIn export
    data/
      mission-control.db        source of record
      backups/                  rotating db backups, newest 7
      history.db                run-over-run snapshots
      llm-cache/                hash-keyed response cache
    artifacts/
      html/                     the three rendered pages
      jobs/                     intel-*.json + rotating org-roles-tracker-*.csv
      content/                  radar-*.json + .md
      profiles/
    config/                     THIS person's targets and feeds
      job-sources.yaml            companies, aggregators, scanning rules
      content-sources.yaml        RSS feeds
    .env                        THIS person's identity: GITHUB_USERNAME,
                                BLUESKY_HANDLE/APP_PASSWORD/PDS_URL
  ariel/                      someone else's data, identical shape
```

### Secrets: two files, on purpose

| file | holds | shared |
|---|---|---|
| `.env` (repo root) | `LLM_BACKEND`, `ANTHROPIC_API_KEY`, `OPENROUTER_API_KEY`, `MC_TIER_ORDER`, `MC_MODEL_T*` | yes - machine-wide |
| `workspace/<name>/.env` | `GITHUB_USERNAME`, `BLUESKY_HANDLE`, `BLUESKY_APP_PASSWORD`, `BLUESKY_PDS_URL` | no - one person |

Identity follows `--user`. `mc run --user ariel` reads ariel's `.env`, so it can
never scan your GitHub or post-history instead of theirs. A workspace with no
`.env` simply skips those scans - it never falls back to whoever ran last.

`uv run setup.py --user NAME` writes each answer to the correct file. Start a
new one from `templates/workspace.env.example`. Both files are gitignored.

### Config is per person too

`job-sources.yaml` (your target companies, keywords, auto-close rules) and
`content-sources.yaml` (your feeds) live in `workspace/<name>/config/`.
`setup.py` seeds them from the repo templates on first run, so each workspace
is self-contained and a whole person's setup can be copied or handed over as
one directory. A workspace with no config of its own falls back to the repo
template, so nothing breaks before setup runs. `model-routing.toml` stays
repo-level - it is app config, not user data.

### Working with more than one dataset

Every command takes `--user NAME`, selecting `workspace/NAME`. Nothing outside
that directory is read or written, so testing against someone else's data can
never disturb your own.

```
mc users                        # list workspaces ( * marks the active default )
mc run --user ariel             # full pipeline against workspace/ariel
mc render --user ariel          # rebuild just the HTML from existing data, no LLM calls
mc dashboard --user ariel       # browse/edit that workspace
```

Defaults to `$MC_WORKSPACE`, then `default`. To set one for a whole shell
session: `export MC_WORKSPACE=ariel`.

To add a dataset, drop it in as `workspace/<name>/` with the shape above - at
minimum `artifacts/jobs/intel-*.json` for the pages to have content, plus
`me/profile.md` if you want role-category suggestions. Missing directories are
created automatically on first use.

