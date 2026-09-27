"""Next-action rules for the Applications tab (agents/applications.py).

Every case runs against a fixed `today`, so nothing here depends on the clock.
"""
import sys
from datetime import date, timedelta
from pathlib import Path

import pandas as pd
import pytest

REPO = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(REPO / "agents"))

import applications as app                # noqa: E402
from store import COLUMN_MAP, Store       # noqa: E402

TODAY = date(2026, 9, 27)


def ago(days):
    return (TODAY - timedelta(days=days)).isoformat()


def role(**fields):
    return {"id": "acme-ds", "org": "Acme", "title": "DS", "status": "03 Applied",
            "stage": "Applied", "date_applied": ago(2), **fields}


def touch(days_ago, direction="out", channel="email", **fields):
    return {"id": days_ago, "date": ago(days_ago), "direction": direction,
            "channel": channel, "archived": 0, **fields}


def contact(cid, name, kind, role_id=None, archived=0):
    return {"id": cid, "name": name, "kind": kind, "role_id": role_id, "archived": archived}


def run(r=None, touches=(), contacts=(), changes=()):
    return app.suggest(r or role(), touches, contacts, changes, today=TODAY)


# ---------------------------------------------------------------- gating

def test_only_applied_roles_get_a_suggestion():
    assert run(role(status="01 Open")) is None
    assert run(role(status="04 Closed")) is None


def test_missing_date_applied_comes_first():
    s = run(role(date_applied=None))
    assert (s.action, s.due, s.kind) == ("Add the date you applied", TODAY, "do")


def test_estimated_date_asks_for_confirmation_until_edited():
    backfilled = [{"seq": 1, "field": "date_applied", "actor": app.BACKFILL_ACTOR}]
    assert run(changes=backfilled).action == "Confirm date applied"
    assert app.date_is_estimate(backfilled)

    edited = backfilled + [{"seq": 2, "field": "date_applied", "actor": "dashboard"}]
    assert not app.date_is_estimate(edited)
    assert run(changes=edited).action.startswith("Reach out")


def test_offer_waits_on_you_with_no_clock():
    s = run(role(stage="Offer", date_applied=ago(60)))
    assert (s.action, s.due) == ("Respond to offer", None)
    assert app.bucket(s.due, TODAY) == "waiting"


# ---------------------------------------------------------------- stage Applied

def test_reach_out_after_applying():
    s = run()
    assert s.action == "Reach out to a recruiter or hiring manager"
    assert s.due == TODAY + timedelta(days=app.REACH_OUT_DAYS - 2)


def test_reach_out_names_the_best_contact():
    people = [contact(1, "Rae", "recruiter"),
              contact(2, "Hal", "hiring_manager"),
              contact(3, "Ari", "hiring_manager", archived=1)]
    assert run(contacts=people).action == "Reach out to Hal (hiring manager)"

    # A contact tied to this role beats a better title that is not.
    people.append(contact(4, "Pat", "peer", role_id="acme-ds"))
    assert run(contacts=people).action == "Reach out to Pat (peer)"


def test_the_application_itself_is_not_outreach():
    s = run(touches=[touch(2, channel="portal")])
    assert s.action.startswith("Reach out")


def test_follow_up_after_a_message():
    s = run(role(date_applied=ago(10)), touches=[touch(3)])
    assert (s.action, s.due) == ("Follow up", TODAY + timedelta(days=app.FOLLOW_UP_DAYS - 3))


def test_archived_touches_are_ignored():
    s = run(role(date_applied=ago(10)), touches=[touch(3, archived=1)])
    assert s.action.startswith("Reach out")


def test_reply_when_they_wrote_last():
    s = run(role(date_applied=ago(10)), touches=[touch(5), touch(2, direction="in")])
    assert (s.action, s.due) == ("Reply", TODAY - timedelta(days=1))
    assert app.bucket(s.due, TODAY) == "overdue"


def test_no_reply_nudge_once_you_answered():
    s = run(role(date_applied=ago(10)),
            touches=[touch(4, direction="in"), touch(3, direction="out")])
    assert s.action == "Follow up"


# ---------------------------------------------------------------- interviews

def test_thank_you_after_an_interview():
    s = run(role(stage="Interviewing", date_applied=ago(20)),
            touches=[touch(1, direction="in", channel="interview")])
    assert (s.action, s.due) == ("Send thank-you", TODAY)


def test_no_thank_you_once_sent():
    s = run(role(stage="Interviewing", date_applied=ago(20)),
            touches=[touch(2, direction="in", channel="interview"), touch(2, direction="out")])
    assert s.action == "Follow up"


# ---------------------------------------------------------------- ghosting

def test_silence_suggests_closing():
    s = run(role(date_applied=ago(25)))
    assert s.kind == "close"
    assert s.action == "Close as ghosted?"
    assert s.due == TODAY - timedelta(days=25 - app.GHOST_DAYS)


def test_a_recent_follow_up_holds_off_closing():
    s = run(role(date_applied=ago(30)), touches=[touch(3)])
    assert (s.kind, s.action) == ("do", "Follow up")


def test_close_once_the_follow_up_has_had_time():
    s = run(role(date_applied=ago(40)), touches=[touch(10)])
    assert s.kind == "close"


def test_hearing_back_resets_the_ghost_clock():
    s = run(role(date_applied=ago(40)),
            touches=[touch(12, direction="in"), touch(11, direction="out")])
    assert (s.kind, s.action) == ("do", "Follow up")


# ---------------------------------------------------------------- later stages

def test_check_in_after_a_stage_move():
    moved = [{"seq": 5, "field": "stage", "new_value": "Screen",
              "actor": "dashboard", "ts": ago(4) + "T10:00:00"}]
    s = run(role(stage="Screen", date_applied=ago(15)), changes=moved)
    assert (s.action, s.due) == ("Check in on timeline",
                                 TODAY + timedelta(days=app.CHECK_IN_DAYS - 4))


def test_follow_up_after_checking_in():
    moved = [{"seq": 5, "field": "stage", "new_value": "Final",
              "actor": "dashboard", "ts": ago(10) + "T10:00:00"}]
    s = run(role(stage="Final", date_applied=ago(20)), touches=[touch(2)], changes=moved)
    assert s.action == "Follow up"


# ---------------------------------------------------------------- your own next action

def test_your_next_action_wins():
    s = run(role(next_action="Send portfolio link", next_action_due=ago(-3)))
    assert (s.action, s.due, s.source) == ("Send portfolio link", TODAY + timedelta(days=3), "manual")
    assert run(role(next_action="Call Hal")).due == TODAY


def test_a_due_date_alone_snoozes_the_rule():
    s = run(role(next_action_due=ago(-10)))
    assert s.action.startswith("Reach out")
    assert s.due == TODAY + timedelta(days=10)
    assert s.reason.startswith("Snoozed")


def test_a_snooze_never_pulls_a_due_date_earlier():
    s = run(role(next_action_due=ago(1)))
    assert s.due == TODAY + timedelta(days=app.REACH_OUT_DAYS - 2)


def test_snoozing_a_close_keeps_it_a_close():
    s = run(role(date_applied=ago(25), next_action_due=ago(-7)))
    assert (s.kind, s.due) == ("close", TODAY + timedelta(days=7))


# ---------------------------------------------------------------- buckets and plumbing

@pytest.mark.parametrize("due, expected", [
    (None, "waiting"),
    (TODAY - timedelta(days=1), "overdue"),
    (TODAY, "today"),
    (TODAY + timedelta(days=7), "week"),
    (TODAY + timedelta(days=8), "later"),
    ("2026-09-26", "overdue"),
])
def test_bucket(due, expected):
    assert app.bucket(due, TODAY) == expected


def test_to_dict_serialises_the_date():
    assert run().to_dict()["due"] == (TODAY + timedelta(days=3)).isoformat()


def test_suggest_for_reads_everything_from_the_store(tmp_path):
    cols = list(COLUMN_MAP.keys())
    store = Store(tmp_path)
    store.save_df(pd.DataFrame([{**{c: None for c in cols}, "Org": "Acme", "Title": "DS",
                                 "Status": "03 Applied", "Date Applied": ago(10),
                                 "Stage": "Applied"}], columns=cols), actor="seed")
    role_id = store.to_df().iloc[0]["_id"]
    store.add_contact(org="ACME", name="Hal", kind="hiring_manager")
    assert app.suggest_for(store, role_id, today=TODAY).action == "Reach out to Hal (hiring manager)"

    store.add_touch(role_id=role_id, date=ago(3), direction="out", channel="linkedin")
    assert app.suggest_for(store, role_id, today=TODAY).action == "Follow up"

    store.set_field(role_id, "date_applied", ago(10), actor=app.BACKFILL_ACTOR)
    store.set_field(role_id, "date_applied", ago(9), actor=app.BACKFILL_ACTOR)
    assert app.suggest_for(store, role_id, today=TODAY).action == "Confirm date applied"

    with pytest.raises(KeyError):
        app.suggest_for(store, "no-such-role", today=TODAY)
