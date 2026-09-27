# Mission Control

A personal job-search assistant. Give it your resume and what you're looking for, and every day it
finds new openings at the companies and job boards you care about, reads each one, and tells you
honestly whether it's worth your time. Once a week it also suggests what to write about.

Everything runs on your own machine. Your resume, notes and results never leave it, except for the
text sent to the AI model that judges each role.

## What you get

| Page | How often | What's on it |
|---|---|---|
| **Job Tracker** | daily | Every role found, with a fit score, a plain-English reason and a recommendation |
| **Content Radar** | weekly | Articles worth reacting to, with a suggested angle for each, and what's new in your GitHub repos |
| **Home** | daily | What changed since the last run, and the radar's top picks |

Each run also scans your LinkedIn export, GitHub and BlueSky, saving a report for each in
`artifacts/profiles/`. How well your LinkedIn covers your keywords is tracked on the home page.

## Quick start

You need [uv](https://docs.astral.sh/uv/) and either the `claude` CLI (logged in) or an Anthropic API key.

```bash
uv tool install --editable . --overrides tool-overrides.txt   # one time: installs the `mc` command
mc setup                                                      # answer a few questions, then a first run
```

`mc setup` asks for your resume and a few lines about what you want, then does a first run. Then:

```bash
mc run          # find new roles, judge them, rebuild the pages
mc dashboard    # browse and edit your results at http://127.0.0.1:8787
```

Not ready to spend anything yet? `mc setup --status` shows what's filled in, and
`mc setup --dry-run` shows what a run would scan. Neither makes an AI call.

Prefer not to install? Run everything from inside the repo as `uv run mc ...` instead.
The `--overrides` flag is needed because one dependency (JobSpy) pins old library versions;
leave it out and Indeed and LinkedIn searches are skipped.

## Tell it what you want

Everything about you lives in `workspace/default/me/`, created by `mc setup`:

- `resume.pdf` or `resume.txt` - read automatically
- `profile.md` - your goals, target roles and keywords (the most important file)
- `linkedin/` - optional LinkedIn export

Three lists in `profile.md` control what it looks for:

| Section | What it does |
|---|---|
| `## Target Keywords` | Terms you want. Each becomes a job-board search, and each match in a posting raises its score. |
| `## Exclude Keywords` | Terms you never want. A match in a **job title** drops the posting before any AI call is spent on it. |
| `## Watch Topics` | Themes the Content Radar should look for, even with no track record in them. Format: `- topic: why`. They never affect jobs. |

Write keywords plainly, without quotes: `marketing science`, not `"marketing science"`.

**Where roles come from.** Your target companies' own job boards (Greenhouse, Lever, Ashby), plus
Built In, Indeed and LinkedIn. Companies, boards and scanning rules are set in
`workspace/default/config/job-sources.yaml`.

## Everyday use

| Command | What it does |
|---|---|
| `mc run` | The normal daily run |
| `mc run --force-radar` | Run the weekly Content Radar now (it normally runs once every 7 days) |
| `mc run --radar-only` | Run the Content Radar now and rebuild the pages, with no job scan |
| `mc run --only jobs` | Run one stage: `profiles`, `jobs`, `radar`, `history` or `render` |
| `mc run --refresh-intel` | Re-judge every role, not just new or changed ones |
| `mc run --max-live 50` | Judge at most 50 newly found roles this run (the default limit is 200) |
| `mc render` | Rebuild the pages from existing data, with no scanning and no AI calls |
| `mc help` | List all commands |

If one stage fails, the others still finish.

**Add a role the scan missed** by pasting its link:

```bash
uv run add_role.py https://job-boards.greenhouse.io/acme/jobs/123
```

It reads the posting, judges it, and adds it to the tracker as `01 Open`. It won't add the same
role twice, and it adds nothing if the page can't be read.

**Run it every morning** with cron:

```cron
0 6 * * * cd /path/to/mission-control && mc run
```

## The dashboard

`mc dashboard` starts a small local server in the background and prints its address.
`mc dashboard stop` shuts it down.

- **Edit** Status, Priority, Role Cat, Outcomes, Notes and Salary. Changes save immediately.
  The Recommendation is the model's judgement, so it's shown but can't be edited.
- **Edit many rows at once** by ticking their checkboxes and using the bar above the table. For
  example, to clear out rejects: filter to `Rec = skip`, **Select all shown**, set Status to
  Closed, **Apply**. Only dropdown fields can be bulk-edited, and rows hidden by a filter are
  never changed.
- **Light or dark:** the switch in the top bar cycles Auto, Light and Dark.
- Without the server, the pages still open as read-only files.
- It only accepts connections from your own machine.

After changing the code, restart it (`mc dashboard stop`, then `mc dashboard`). It doesn't
reload on its own. `http://127.0.0.1:8787/api/health` shows which commit it's running.

## How it judges roles

Keywords decide what gets **fetched**. An AI model decides what's a good **fit**, reading each
posting against your profile and resume. Its prompts are designed to make "skip" and "nothing this
week" normal answers, so expect most roles to be skipped. That's it working, not failing.

- **Category:** if a role has no Role Cat, the model picks one of the role types defined in
  your profile, or leaves it empty if none fits. A category you set yourself is never changed.
- **Only new work costs money:** each role's verdict is stored with a fingerprint of the posting
  and the prompt. A normal day only judges new or edited roles. AI responses are also cached, so
  re-running on unchanged input is free.

### Models

Calls go to Claude, through the `claude` CLI (billed to your subscription) or the Anthropic API
(billed per token), and optionally to cheaper open models on [OpenRouter](https://openrouter.ai).

Models are arranged in tiers. Simple, mechanical jobs such as labeling a company's sector start
on a cheap tier. The fit judgement you act on starts on Claude Sonnet, because in testing cheaper
models changed too many verdicts. An answer with the wrong shape or an impossible score is
retried, then moved up a tier, ending at Claude Opus. The worst case is a slower answer, not a
wrong one.

Two files control this:

| File | Sets |
|---|---|
| `.env` | Which backend to use (`LLM_BACKEND=cli` or `api`), your API keys, and which model each tier is (`MC_MODEL_T1` to `MC_MODEL_T4`) |
| `config/model-routing.toml` | Which tier each kind of call starts at |

Each run logs which model handled each kind of call and how many tokens it used.

## Your data

All of it lives in `workspace/<name>/`, which git ignores:

```
workspace/default/
  me/           your resume, profile.md and LinkedIn export
  config/       your target companies, job boards and news feeds
  data/         mission-control.db (the tracker), backups, history, AI cache
  artifacts/    the generated pages, plus CSV and JSON exports
  .env          your GitHub and BlueSky details
```

- **The tracker is a single SQLite file**, `data/mission-control.db`. Every run backs it up first
  and exports a CSV copy afterwards, keeping the newest 7 of each. Every edit is logged with the
  old value, the new value and who made it.
- **Two `.env` files, on purpose.** The one in the repo root holds machine-wide settings (API keys,
  model choices). The one in each workspace holds that person's GitHub and BlueSky accounts, so a
  run for one person can never scan someone else's. Start them from `.env.example` and
  `templates/workspace.env.example`.

**More than one person?** Every command takes `--user NAME` to use `workspace/NAME` instead, and
nothing outside that folder is touched. `mc users` lists workspaces. To switch for a whole
terminal session, run `export MC_WORKSPACE=NAME`.

## Project layout

```
src/mission_control/   the `mc` command, which runs the scripts below
setup.py               first-time setup
run_daily.py           the daily pipeline
dashboard.py           the local dashboard server
add_role.py            add one role from a URL
workspace.py           picks the workspace for --user
agents/                the pipeline's parts
  job_intel.py           finds, judges and tracks roles
  job_scanner.py         company boards, Built In and JobSpy
  content_radar.py       the weekly Content Radar
  llm.py, routing.py     AI calls and model tiers
  store.py               the tracker database
  history.py             what changed between runs
render/                page templates and styles
config/                starter config, copied into each new workspace
templates/me/          starter profile, copied into each new workspace
tests/                 the test suite
attic/                 retired code, kept for reference
```

## Tests

```bash
uv run pytest                        # the whole suite
uv run pytest tests/test_store.py    # one file
```

Each test is a standalone script that also runs on its own (`uv run python tests/test_store.py`).
None of them makes an AI call or depends on your `.env` or workspace. GitHub Actions runs the suite on
every pull request. The scripts in `tests/evals/` are not part of it, because they make real, paid
AI calls.
