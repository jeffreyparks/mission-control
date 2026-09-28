# Applications tab - phase 1: tracking and nudges

Status: in progress. Phase 1 makes no LLM calls.

## Goal

A page listing every role with status `03 Applied`, sorted by when its next
action is due. From that page you can log contacts and touchpoints, set a
stage, snooze a nudge, or close the role in one step.

Later phases, not covered here:

- Phase 2: an LLM company brief, positioning, and outreach drafts for each role.
- Phase 3: matching content radar picks to each application.
- Phase 4: optional Gmail, Calendar and Apollo integrations.

## The date applied has to be filled in first

Nothing set `date_applied` when a role moved to `03 Applied`, so an existing
workspace can have applied roles with no date at all, and the change log only
records the move for roles moved since it existed. Every nudge is timed from
`date_applied`, so filling it in comes first (step 2 below).

## 1. Data changes (`agents/store.py`)

### New role fields

These go in `COLUMN_MAP` and `MANUAL_FIELDS`. `_add_missing_columns` adds them
to an existing database automatically.

| Header | Column | Values |
|---|---|---|
| Stage | `stage` | `Applied`, `Screen`, `Interviewing`, `Final`, `Offer` |
| Next Action | `next_action` | Free text, up to 200 characters. Overrides the suggested action. |
| Next Action Due | `next_action_due` | `YYYY-MM-DD`. Setting this is how you snooze. |

`stage` is a new field, separate from the status list, so nothing that reads
status needs to change. `outcomes` still records what the employer did, and
`close_reason` still records why you passed.

### New tables

```sql
CREATE TABLE IF NOT EXISTS contacts (
    id          INTEGER PRIMARY KEY AUTOINCREMENT,
    org         TEXT NOT NULL,       -- contacts belong to an org; a recruiter often covers several roles
    role_id     TEXT,                -- optional: the specific role, if any
    name        TEXT NOT NULL,
    title       TEXT,
    kind        TEXT,                -- recruiter | hiring_manager | referrer | peer | other
    url         TEXT,
    email       TEXT,
    source      TEXT,                -- how you found them
    notes       TEXT,
    archived    INTEGER DEFAULT 0,   -- soft delete
    created_at  TEXT, updated_at TEXT
);
CREATE TABLE IF NOT EXISTS touches (
    id          INTEGER PRIMARY KEY AUTOINCREMENT,
    role_id     TEXT NOT NULL,
    contact_id  INTEGER,             -- empty for "submitted via portal" and similar
    date        TEXT NOT NULL,       -- YYYY-MM-DD
    channel     TEXT,                -- email | linkedin | call | interview | portal | other
    direction   TEXT,                -- out | in
    summary     TEXT,
    archived    INTEGER DEFAULT 0,
    created_at  TEXT
);
```

### New store methods

- `add_contact` / `update_contact` / `archive_contact`
- `add_touch` / `archive_touch`
- `contacts_for(org)`: case-insensitive match on org, so a contact appears on
  every role at that company
- `touches_for(role_id)`

Every write also goes to the `changes` log, as `field="contact:<id>"` or
`field="touch:<id>"` with a JSON `new_value`, so the audit trail stays complete.
Nothing is ever hard-deleted.

## 2. Filling in the date applied

- **One-time backfill (`scripts/backfill_date_applied.py`).** For each applied
  role with no `date_applied`, use the first time its status changed to
  `03 Applied` in the change log. If there's none, use `last_updated`, then
  `date_opened`. Writes are logged with `actor="backfill"`, and the script
  supports `--dry-run`.
- **Unconfirmed dates.** A date counts as unconfirmed while its most recent
  change-log entry came from `backfill`. The page shows an "unconfirmed" badge
  and a first nudge, "Confirm date applied", until you edit the date. Saving
  it unchanged also confirms it: the dashboard logs that as a change with the
  old and new values equal (`Store.confirm_field`). No extra column is needed.
- **Going forward.** In `dashboard.py`'s POST handler, when status changes to
  `03 Applied`, fill `date_applied` with today and `stage` with `Applied`, but
  only where each is empty. Each fill is its own logged `set_field`.

## 3. Nudge rules (`agents/applications.py`)

`suggest(role, touches, today)` is a pure function: its answer depends only on
its inputs. It returns `(action, due, kind)`, where `kind` is `do` or `close`.
If you've set a manual next action with a due date that hasn't passed, that
wins over the suggestion.

| Condition | Suggested action | Due |
|---|---|---|
| Date applied missing or unconfirmed | Confirm date applied | Today |
| Stage `Applied`, no contact logged, no outbound touch | Find a recruiter or hiring manager and reach out | Applied + 5 days |
| An outbound touch with no reply | Follow up | Last outbound + 7 days |
| A touch with `channel=interview` and no outbound since | Send thank-you | Interview + 1 day |
| Stage Screen, Interviewing or Final, and nothing inbound recently | Check in on timeline | Last inbound + 7 days |
| No inbound touch 21 days after the last activity | Close as ghosted? (`kind=close`) | That day |
| Stage `Offer` | Respond to offer | Manual due date only |

"Inbound" means `direction=in` touches plus stage changes, since those mean the
employer did something.

The thresholds are module constants (`FOLLOW_UP_DAYS=7`, `GHOST_DAYS=21`, and
so on). A per-workspace `config/applications.toml` override can come later.

Grouping on the page: Overdue, Due today, This week, Later, Waiting (nothing due).

## 4. API changes (`dashboard.py`)

All new routes follow the existing rules: loopback only, a fixed list of
allowed values, length caps, and a single write lock.

- **Existing `POST /api/role/<id>`:** add `stage` (fixed list), `next_action`
  (free text, max 200), `next_action_due` and `date_applied` to `EDITABLE`.
  The two date fields must match `YYYY-MM-DD` or be empty.
- **`POST /api/role/<id>/close`** with `{outcome, close_reason}`: sets
  `status=04 Closed`, then `outcomes`, then `close_reason`. Each is a separate
  logged `set_field`. `outcome` must be one of the existing outcome options,
  and `close_reason` must be blank or one of `CLOSE_REASONS`. When you close a
  ghosted role, the dialog fills `outcome=Ghosted` and leaves the close reason
  blank, which the preferences learner correctly ignores.
- **Contacts:**
  - `POST /api/contacts` creates one. Required: `org`, `name`. Optional:
    `role_id`, `title`, `kind`, `url`, `email`, `source`, `notes`. `kind` comes
    from a fixed list, `url` must start with `http(s)://`, and `notes` is
    capped at 1000 characters.
  - `POST /api/contact/<id>` updates one; `{"archived": true}` soft-deletes it.
- **Touches:**
  - `POST /api/touches` creates one. Required: `role_id`, `date`. Optional:
    `contact_id`, `channel`, `direction`, `summary`. `channel` and `direction`
    come from fixed lists, and `summary` is capped at 1000 characters.
  - `POST /api/touch/<id>` with `{"archived": true}` soft-deletes it.
- **`GET /applications.html`** is rendered live, the same way
  `/job-tracker.html` is. If that fails, it serves the static copy.

## 5. The page

Where the code goes:

- A new template, `render/applications.html.j2`.
- A tab linked from `_nav.html.j2` between Job Tracker and Content Radar, with
  `page='applications'`.
- One shared function, `_applications_context(store, intel, today)`, in
  `render/build.py`, used by both the live render and a static
  `render_applications()`. `build.main()` and `run_daily.run_render` both call it.
- The page is built from the database. It reads the latest intel JSON only for
  `pitch_angle`, `top_gaps` and `fit_score`, and still works if that file
  doesn't exist.

Layout:

- **Header counts:** Active · Overdue · Due this week · Waiting.
- **One row per role, grouped by urgency:** org and title, a stage chip, days
  since applied (with the unconfirmed badge if needed), the last touch (e.g.
  "3d ago · in · recruiter"), the next action and its due date, and quick
  buttons: Log touch, Snooze 3d / 7d, Close.
- **Expanding a row (a `<details>` element) shows:**
  - Timeline: date applied, touches, and stage and status changes from
    `changes`, newest first.
  - Contacts: everyone at the org, an inline form to add a contact, and
    generated LinkedIn people-search links (`<org> recruiter`,
    `<org> <function> director`). The URLs contain only the org name and function.
  - Positioning: `pitch_angle` and `top_gaps` from the fit review, read-only.
    Phase 2's brief goes here.
  - Notes, editable.
- **Close dialog:** an outcome dropdown and an optional close-reason dropdown,
  both filled in from the suggestion.
- **Recently closed:** a collapsed section listing applications closed in the
  last 14 days - roles whose status moved from `03 Applied` to `04 Closed`, not
  everything closed in the tracker - each with a Reopen button that sets status
  back to `03 Applied`.

How edits work: like the tracker, the page is read-only until `/api/health`
responds. After any write, the page reloads. With about 20 rows that's instant,
and it avoids the in-place DOM updates the tracker uses.

Home page: a small card reading "Applications: N overdue, M this week" that
links to the tab.

## 6. Tests

| File | What it covers |
|---|---|
| `tests/test_applications.py` | Each rule in the table, with a fixed `today`; a manual next action overriding the suggestion; the switch from a nudge to a close suggestion; unconfirmed-date detection |
| `tests/test_applications_store.py` | An older database gains the new tables and columns; each contact and touch write lands in `changes`; soft delete; case-insensitive org matching |
| `tests/test_dashboard.py` (additions) | Validation on every new route (vocabularies, dates, URL, lengths, loopback); the close route writing three logged changes; the auto-fill when status becomes Applied; `/applications.html` rendering live, including with no intel JSON |
| `tests/test_backfill_date_applied.py` | The order of fallbacks, dry run, and running twice changes nothing |

## 7. Build order (each step is one PR)

1. Store: new tables, columns and methods, plus their tests.
2. Backfill script, and the auto-fill in the dashboard. Then run the backfill
   on `default` with `--dry-run` first.
3. `agents/applications.py` rules, plus their tests.
4. Dashboard: the new fields and routes.
5. Template, nav link, both render paths, and the home card.

## Not in phase 1

- LLM briefs and outreach drafts (phase 2)
- Content tie-ins with the radar (phase 3)
- Gmail, Calendar and Apollo (phase 4)
- Any automatic status changes: every close still needs your click.
