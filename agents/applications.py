"""
Next actions for active applications: what to do on each one, and by when.

Deterministic on purpose - no LLM. suggest() looks at a role, its touches, the
contacts at its org and its change log, and returns one Suggestion: an action,
a due date and a kind ("do", or "close" when the application looks dead). The
Applications tab sorts by the due date and groups by bucket().

The rules, first match wins:

  1. A next action you typed yourself.
  2. No date applied, or one the backfill estimated -> confirm it. Every other
     rule is timed from that date, so it comes first.
  3. Stage Offer -> respond to the offer (no clock).
  4. An interview with nothing sent since -> send a thank-you, the next day.
  5. They got in touch and you have not answered -> reply, the next day.
  6. Nothing from them for GHOST_DAYS, and any follow-up you sent has had
     FOLLOW_UP_DAYS to land -> suggest closing it as ghosted.
  7. Still at Applied: reach out to someone REACH_OUT_DAYS after applying,
     then follow up FOLLOW_UP_DAYS after each message.
  8. Further along: check in on the timeline CHECK_IN_DAYS after you last
     heard, then follow up FOLLOW_UP_DAYS after each check-in.

A Next Action Due with no Next Action text is a snooze: the rule still picks
the action, but it is not due before the snooze date.

"From them" means an inbound touch, an interview, or a stage change past
Applied - each says the employer did something. "Sent" means an outbound
touch other than the application itself (channel portal) or an interview.
"""
from dataclasses import asdict, dataclass
from datetime import date, datetime, timedelta

from store import STAGES

APPLIED_STATUS = "03 Applied"

# The actor scripts/backfill_date_applied.py writes as. A date whose latest
# change came from it is an estimate until you edit it.
BACKFILL_ACTOR = "backfill_date_applied"

REACH_OUT_DAYS = 5
FOLLOW_UP_DAYS = 7
CHECK_IN_DAYS = 7
GHOST_DAYS = 21
REPLY_DAYS = 1
THANK_YOU_DAYS = 1
WEEK_DAYS = 7

# Who to reach out to first, when there is anyone to name.
CONTACT_PREFERENCE = ["hiring_manager", "recruiter", "referrer", "peer", "other"]

# Touches that are not you reaching out to someone.
NOT_OUTREACH_CHANNELS = {"portal", "interview"}

BUCKETS = ["overdue", "today", "week", "later", "waiting"]


@dataclass(frozen=True)
class Suggestion:
    action: str
    due: date | None      # None: nothing to do until something happens
    kind: str             # "do" or "close"
    reason: str           # one line, for a tooltip
    source: str = "rule"  # "rule" or "manual"

    def to_dict(self):
        out = asdict(self)
        out["due"] = self.due.isoformat() if self.due else None
        return out


def _day(value):
    """A date from 'YYYY-MM-DD' or an ISO timestamp; None for anything else."""
    if isinstance(value, datetime):
        return value.date()
    if isinstance(value, date):
        return value
    try:
        return datetime.strptime(str(value or "").strip()[:10], "%Y-%m-%d").date()
    except ValueError:
        return None


def _latest(days):
    days = [d for d in days if d]
    return max(days) if days else None


def date_is_estimate(changes):
    """True when the latest write to date_applied came from the backfill."""
    writes = [c for c in changes if c.get("field") == "date_applied"]
    if not writes:
        return False
    return max(writes, key=lambda c: c.get("seq") or 0).get("actor") == BACKFILL_ACTOR


def _pick_contact(contacts, role_id):
    """The contact to name in a reach-out: one tied to this role first, then by
    CONTACT_PREFERENCE. None when there is no one."""
    live = [c for c in contacts if not c.get("archived")]
    if not live:
        return None
    rank = {kind: i for i, kind in enumerate(CONTACT_PREFERENCE)}
    return min(live, key=lambda c: (c.get("role_id") != role_id,
                                    rank.get(c.get("kind"), len(rank)), c.get("id") or 0))


def _who(contact):
    if not contact:
        return "a recruiter or hiring manager"
    kind = (contact.get("kind") or "").replace("_", " ")
    return f"{contact['name']} ({kind})" if kind else contact["name"]


def _rule(role, touches, contacts, changes, today):
    applied = _day(role.get("date_applied"))
    stage = role.get("stage") or STAGES[0]

    if applied is None:
        return Suggestion("Add the date you applied", today, "do",
                          "Every nudge is timed from the date applied.")
    if date_is_estimate(changes):
        return Suggestion("Confirm date applied", today, "do",
                          f"{applied.isoformat()} is an estimate from the backfill.")
    if stage == "Offer":
        return Suggestion("Respond to offer", None, "do", "You have an offer.")

    live = [t for t in touches if not t.get("archived") and _day(t.get("date"))]
    interviews = [_day(t["date"]) for t in live if t.get("channel") == "interview"]
    sent = [_day(t["date"]) for t in live
            if t.get("direction") == "out" and t.get("channel") not in NOT_OUTREACH_CHANNELS]
    replies = [_day(t["date"]) for t in live
               if t.get("direction") == "in" and t.get("channel") != "interview"]
    stage_moves = [_day(c.get("ts")) for c in changes
                   if c.get("field") == "stage" and c.get("new_value") in STAGES[1:]]

    last_interview = _latest(interviews)
    last_sent = _latest(sent)
    last_reply = _latest(replies)
    last_heard = _latest(replies + interviews + stage_moves)

    if last_interview and not (last_sent and last_sent >= last_interview):
        return Suggestion("Send thank-you", last_interview + timedelta(days=THANK_YOU_DAYS), "do",
                          f"Interview on {last_interview.isoformat()}, nothing sent since.")

    if last_reply and last_reply > (last_sent or date.min):
        return Suggestion("Reply", last_reply + timedelta(days=REPLY_DAYS), "do",
                          f"They got in touch on {last_reply.isoformat()}.")

    silent_since = max(applied, last_heard) if last_heard else applied
    ghost_due = silent_since + timedelta(days=GHOST_DAYS)
    if last_sent:
        ghost_due = max(ghost_due, last_sent + timedelta(days=FOLLOW_UP_DAYS))
    if today >= ghost_due:
        return Suggestion("Close as ghosted?", ghost_due, "close",
                          f"Nothing from them since {silent_since.isoformat()}.")

    if last_sent and last_sent >= silent_since:
        return Suggestion("Follow up", last_sent + timedelta(days=FOLLOW_UP_DAYS), "do",
                          f"Last message sent {last_sent.isoformat()}, no reply yet.")
    if stage == STAGES[0]:
        contact = _pick_contact(contacts, role.get("id"))
        return Suggestion(f"Reach out to {_who(contact)}",
                          applied + timedelta(days=REACH_OUT_DAYS), "do",
                          "Nobody contacted yet." if contact is None
                          else f"{contact['name']} is on file; no message sent yet.")
    return Suggestion("Check in on timeline", silent_since + timedelta(days=CHECK_IN_DAYS), "do",
                      f"Last heard {silent_since.isoformat()}.")


def suggest(role, touches=(), contacts=(), changes=(), today=None):
    """The next action for one role, or None when the role is not an active
    application. `role` is a roles row as a dict (db column names); touches,
    contacts and changes are store rows as dicts."""
    today = _day(today) or date.today()
    if role.get("status") != APPLIED_STATUS:
        return None

    manual_action = (role.get("next_action") or "").strip()
    manual_due = _day(role.get("next_action_due"))
    if manual_action:
        return Suggestion(manual_action, manual_due or today, "do",
                          "Set by you.", source="manual")

    found = _rule(role, list(touches), list(contacts), list(changes), today)
    if manual_due and (found.due is None or manual_due > found.due):
        return Suggestion(found.action, manual_due, found.kind,
                          f"Snoozed to {manual_due.isoformat()}. {found.reason}", found.source)
    return found


def bucket(due, today=None):
    """Where a due date sits: overdue, today, week, later, or waiting (no date)."""
    today = _day(today) or date.today()
    due = _day(due)
    if due is None:
        return "waiting"
    if due < today:
        return "overdue"
    if due == today:
        return "today"
    if due <= today + timedelta(days=WEEK_DAYS):
        return "week"
    return "later"


def suggest_for(store, role_id, today=None):
    """suggest() with its inputs read from the store."""
    role = store.get_role(role_id)
    if role is None:
        raise KeyError(role_id)
    return suggest(role,
                   touches=store.touches_for(role_id),
                   contacts=store.contacts_for(role.get("org")),
                   changes=store.history(role_id=role_id, limit=1000),
                   today=today)
