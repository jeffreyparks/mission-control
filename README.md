# Mission Control

A hands-on project in **agentic-system design and LLM evaluation**, built around a use case that
rewards getting both right: continuous career development.

It's a pipeline of small LLM agents that read the professional landscape (job postings, industry
writing, and your own public profiles) and turn it into judgements you can act on: which skills and
roles a field is rewarding, what's worth learning or writing about, and how your profile reads to
people in that field. Role postings are a big part of it, because they are the clearest public
signal of what a field values, but the goal is ongoing learning and professional growth, not just
a job hunt.

Everything runs on your own machine. Your resume, notes and results never leave it, except for the
text sent to the AI model that does the judging.

## Why it exists

**To practise building agents that hold up on real, messy data.** Each part is a small problem in
agent design:

- **Model routing with escalation.** Cheap models handle mechanical labeling; judgements start on a
  stronger model. Malformed or impossible answers are retried and moved up a tier.
  See [Models](#models).
- **LLM-as-judge, calibrated to say no.** The fit prompts make "skip" and "nothing this week"
  normal answers instead of inflating scores.
- **Grounded output.** Every profile finding must quote your actual text, and a quote that isn't
  really there is dropped in code.
- **Code for facts, models for words.** Counts and evidence are computed deterministically; the
  model only phrases them.
- **Cost as a design constraint.** Verdicts are fingerprinted against the input and prompt, and
  responses are cached, so only new or changed work costs anything.
- **Human in the loop.** Preferences learned from your decisions are drafted with their evidence
  and change nothing until you approve them.
- **Failure isolation.** If one stage fails, the others still finish.

**To evaluate models on a task where the answers can be checked.** The scripts in `tests/evals/`
compare cheaper open models against frontier verdicts on real prompts. That's how the routing
tiers were chosen: the fit judgement stays on Claude Sonnet because, in testing, cheaper models
changed too many verdicts.

## What you get

| Page | How often | What's on it |
|---|---|---|
| **Tracker** | daily | Every role found, with a fit score, a plain-English reason, a recommendation and how long ago it was posted |
| **Content Radar** | weekly | Articles worth reacting to, with a suggested angle for each, and what's new in your GitHub repos |
| **Profile** | when a profile changes, or weekly | How your LinkedIn, GitHub, BlueSky and sites read for the roles you pursue: scores, findings to fix, draft rewrites |
| **Home** | daily | What changed since the last run, the radar's top picks, and a Profile summary |

See [How it reviews your profile](#how-it-reviews-your-profile).

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

An optional `## Content Radar Guidance` section is free-text direction for the radar alone:
subjects to skip, stances to take (*"Smart glasses are a bad product; I only write about them
critically"*). The radar follows it over its own breadth rules, and the job-fit judge never sees it.

An optional `## Persona` section in `profile.md` says how you want to come across; the Profile
review judges your voice against it. List personal sites in `workspace/default/config/profile.yaml`.

**Or edit it all in the dashboard.** The **Settings** page (`mc dashboard`, then Settings in the
top bar) edits every section of `profile.md`, your GitHub and BlueSky accounts, and the Profile
review settings, one card at a time, with the radar guidance in its own Content Radar section. Each save changes just that section and keeps the rest of the
file, comments included; the previous `profile.md` is kept as `profile.prev.md`. Reordering Target
Roles renumbers them, and the numbers are how Role Cat refers to them. The BlueSky app password
stays file-only. Nothing re-runs on save: the next `mc run` or `mc profile` uses the new values.

**Where roles come from.** Your target companies' own job boards (Greenhouse, Lever, Ashby), plus
Built In, Indeed and LinkedIn. Companies, boards and scanning rules are set in
`workspace/default/config/job-sources.yaml`.

## Everyday use

| Command | What it does |
|---|---|
| `mc run` | The normal daily run |
| `mc run --force-radar` | Run the weekly Content Radar now (it normally runs once every 7 days) |
| `mc run --radar-only` | Run the Content Radar now and rebuild the pages, with no job scan |
| `mc run --only jobs` | Run one stage: `profiles`, `jobs`, `radar`, `preferences`, `history` or `render` |
| `mc run --refresh-open` | Re-judge every role you haven't closed; closed roles keep their verdicts |
| `mc run --refresh-intel` | Re-judge every role, closed ones included (the most expensive run there is) |
| `mc run --max-live 50` | Judge at most 50 newly found roles this run (the default limit is 200) |
| `mc profile` | Snapshot your profiles, and re-judge them if something changed or a week has passed |
| `mc profile --force --report` | Re-judge now and write a markdown report (`mc profile --help` for more) |
| `mc render` | Rebuild the pages from existing data, with no scanning and no AI calls |
| `mc preferences` | Draft learned preferences from your decisions now (see [Learning from your decisions](#learning-from-your-decisions)) |
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

- **Edit** Status, Role Cat, Outcomes, Close reason, Notes and Salary. Changes save immediately.
  The Recommendation is the model's judgement, so it's shown but can't be edited.
- **Close reason** records why *you* passed on a role (not a fit on function, level, skills or
  industry; comp; location; chose another role at the company; posting closed; other).
  Outcomes records what the *employer* did. Keep them apart: the close reasons are what the
  tracker learns from.
- **Posted** shows how many days ago the board says a role went up, highlighted when it's 3 days
  old or less, and the **Posted** filter narrows to the last 3, 7 or 14 days. With no board date,
  an italic `~Nd` shows days since the tracker first saw the role instead; that's only a lower
  bound, so the filter ignores it.
- **Edit many rows at once** by ticking their checkboxes and using the bar above the table. For
  example, to clear out rejects: filter to `Rec = skip`, **Select all shown**, set Status to
  Closed, pick a close reason, **Apply**. Only dropdown fields can be bulk-edited, and rows
  hidden by a filter are never changed.
- **Settings** edits your profile, accounts and review settings. See
  [Tell it what you want](#tell-it-what-you-want).
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
- **Function:** every role is also tagged with the kind of work it is (Data Science & ML,
  Marketing, Finance & Accounting and so on), shown under its title. That's separate from the
  employer's sector: a data scientist at a bank is Data Science & ML, at a Finance company.
- **Posting date:** taken from the board itself (Greenhouse, Lever, Ashby, Built In, Indeed and
  LinkedIn searches), never guessed.
- **Only new work costs money:** each role's verdict is stored with a fingerprint of the posting
  and the prompt. A normal day only judges new or edited roles. AI responses are also cached, so
  re-running on unchanged input is free.

### Learning from your decisions

Your profile says what you *want*. Your decisions show what you actually *do*, and the two drift
apart: maybe you keep passing on roles at banks, or applying to roles the model said to skip.
Once a week the daily run reads those decisions and drafts `me/learned.md`, a few plain
sentences for the fit judge, such as "treat roles at banks as a weak fit whatever the function".

- **What counts:** roles you closed with a close reason about the role itself, and roles you
  researched, applied to or heard back on. Closes with no reason, or a neutral one ("chose
  another role at the company", "posting closed"), teach it nothing.
- **Only corrections:** a pattern is kept only when you overrode the model's recommendation at
  the time, on a real share of those roles. Closing what it already said to skip changes nothing.
- **Evidence, not vibes:** a pattern needs at least 5 decisions from the last 180 days. The
  counts are computed from your data; the AI only words the sentences and drops patterns that
  are side effects of another one.
- **Nothing changes until you approve it.** The draft is `me/learned.draft.md`, with the
  evidence behind each line in comments. Edit or delete lines, then run
  `mc preferences --approve` (the previous version is kept as `me/learned.prev.md`).

Approving guides new and edited roles from then on. To re-score what's still in play straight
away, run `mc run --refresh-open`.

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

## How it reviews your profile

- **Sources:** your LinkedIn export in `me/linkedin/` (phone numbers and emails stripped), GitHub
  and BlueSky through their public APIs, and your sites (respecting `robots.txt`). A new snapshot
  is kept only when what a profile says has changed.
- **Yardstick:** roles you acted on or that scored 70+ in the last 60 days. What they ask for is
  counted in code, not by the model.
- **Evidence:** every finding quotes your actual text; a quote that isn't really there is dropped.
- **Triage:** accept, dismiss or mark findings fixed on the Profile page. Dismissed stays
  dismissed; fixes are detected on the next review.
- **Review guidance:** when your dismissals change, the daily run drafts
  `me/profile-guidance.md` - standing rules for the reviewer, from the findings you dismissed and
  the reasons you gave. Like learned preferences, it is only a draft until you approve it.
- **Drafts:** suggested headline, bio and tagline rewrites, checked against your resume. Approved
  text goes to `me/persona.md`; nothing is ever posted for you. Open coverage gaps also feed the
  Content Radar.
- **Cost:** about five top-tier calls per review, and nothing when your profiles haven't changed.

## Your data

All of it lives in `workspace/<name>/`, which git ignores:

```
workspace/default/
  me/           your resume, profile.md, LinkedIn export, learned.md and approved profile text
  config/       your target companies, job boards, news feeds and sites
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
  preferences.py         learned preferences from your decisions
  profile_guidance.py    profile review guidance from your dismissed findings
  posted.py              posting dates, from every board's format
  content_radar.py       the weekly Content Radar
  profile_*.py           the Profile review: snapshots, brief, evaluation, drafts
  {linkedin,github,bluesky,site}_scanner.py   the Profile's collectors
  llm.py, routing.py     AI calls and model tiers
  store.py               the tracker database
  history.py             what changed between runs
render/                page templates and styles
config/                starter config, copied into each new workspace
templates/me/          starter profile, copied into each new workspace
scripts/               one-off backfills for existing trackers
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
