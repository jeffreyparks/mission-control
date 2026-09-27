"""Store support for the Applications tab: stage fields, contacts and touches.

Covers:
  - an older database gains the new columns and tables without losing rows
  - the pipeline's save_df leaves stage and next action alone
  - contacts: required fields, unknown fields refused, a role_id must exist,
    org matched without regard to case, updates log only what changed,
    archiving hides a contact without deleting it
  - touches: required fields, role and contact must exist, newest first,
    archiving hides a touch without deleting it
  - every contact and touch write lands in the change log under its role
"""
import json
import sqlite3
import sys
from pathlib import Path

import pandas as pd
import pytest

REPO = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(REPO / "agents"))

from store import COLUMN_MAP, MANUAL_FIELDS, Store  # noqa: E402

COLS = list(COLUMN_MAP.keys())


def _seed(tmp_path):
    rows = [
        {**{c: None for c in COLS}, "Org": "Acme", "Title": "Head of Measurement",
         "Status": "03 Applied"},
        {**{c: None for c in COLS}, "Org": "Acme", "Title": "Director, Analytics",
         "Status": "03 Applied"},
        {**{c: None for c in COLS}, "Org": "Beta", "Title": "VP Data",
         "Status": "01 Open"},
    ]
    store = Store(tmp_path)
    store.save_df(pd.DataFrame(rows, columns=COLS), actor="seed")
    ids = list(store.to_df()["_id"])
    return store, ids


def _log(store, role_id=None):
    return [c for c in store.history(role_id=role_id, limit=500)
            if c["field"].startswith(("contact:", "touch:"))]


# ---------------------------------------------------------------- migration

def test_older_database_gains_new_columns_and_tables(tmp_path):
    (tmp_path / "data").mkdir()
    conn = sqlite3.connect(tmp_path / "data/mission-control.db")
    conn.execute("CREATE TABLE roles (id TEXT PRIMARY KEY, row_order INTEGER, "
                 "org TEXT, title TEXT, status TEXT)")
    conn.execute("INSERT INTO roles VALUES ('acme-pm', 0, 'Acme', 'PM', '03 Applied')")
    conn.commit()
    conn.close()

    store = Store(tmp_path)
    with store.connect() as c:
        columns = {r["name"] for r in c.execute("PRAGMA table_info(roles)")}
        tables = {r["name"] for r in c.execute(
            "SELECT name FROM sqlite_master WHERE type='table'")}
    assert {"stage", "next_action", "next_action_due"} <= columns
    assert {"contacts", "touches"} <= tables
    assert store.count() == 1


def test_new_role_fields_are_human_owned(tmp_path):
    assert {"stage", "next_action", "next_action_due"} <= set(MANUAL_FIELDS)
    store, ids = _seed(tmp_path)
    store.set_field(ids[0], "stage", "Screen")
    store.set_field(ids[0], "next_action_due", "2026-10-01")

    # A pipeline run loads the tracker, touches other fields and saves it back.
    frame = store.load(verbose=False)
    frame.loc[0, "Fit Score"] = 81
    store.save_df(frame, actor="pipeline")

    row = store.to_df().set_index("_id").loc[ids[0]]
    assert row["Stage"] == "Screen"
    assert row["Next Action Due"] == "2026-10-01"


# ---------------------------------------------------------------- contacts

def test_add_contact_requires_org_and_name(tmp_path):
    store, _ = _seed(tmp_path)
    with pytest.raises(ValueError):
        store.add_contact(org="Acme", name=" ")
    with pytest.raises(ValueError):
        store.add_contact(org="", name="Dana")


def test_add_contact_refuses_unknown_fields_and_missing_roles(tmp_path):
    store, _ = _seed(tmp_path)
    with pytest.raises(ValueError):
        store.add_contact(org="Acme", name="Dana", phone="555")
    with pytest.raises(KeyError):
        store.add_contact(org="Acme", name="Dana", role_id="no-such-role")


def test_add_contact_stores_and_logs_under_its_role(tmp_path):
    store, ids = _seed(tmp_path)
    contact = store.add_contact(org="Acme", name=" Dana Lee ", kind="recruiter",
                                role_id=ids[0], actor="test")
    assert contact["name"] == "Dana Lee"
    assert contact["archived"] == 0
    assert contact["created_at"]

    entries = _log(store, ids[0])
    assert len(entries) == 1
    assert entries[0]["field"] == f"contact:{contact['id']}"
    assert entries[0]["actor"] == "test"
    assert entries[0]["old_value"] is None
    assert json.loads(entries[0]["new_value"])["kind"] == "recruiter"


def test_contacts_for_matches_org_ignoring_case_and_spaces(tmp_path):
    store, _ = _seed(tmp_path)
    store.add_contact(org="Acme", name="Dana")
    store.add_contact(org=" acme ", name="Avery")
    store.add_contact(org="Beta", name="Sam")
    names = [c["name"] for c in store.contacts_for("ACME")]
    assert names == ["Avery", "Dana"]


def test_update_contact_logs_only_what_changed(tmp_path):
    store, _ = _seed(tmp_path)
    contact = store.add_contact(org="Acme", name="Dana", title="Recruiter")

    same = store.update_contact(contact["id"], title="Recruiter")
    assert same["changed"] is False

    result = store.update_contact(contact["id"], title="Senior Recruiter", notes="met at a meetup")
    assert result["changed"] is True
    assert result["contact"]["title"] == "Senior Recruiter"

    latest = _log(store)[0]
    assert json.loads(latest["old_value"]) == {"title": "Recruiter", "notes": None}
    assert json.loads(latest["new_value"]) == {"title": "Senior Recruiter",
                                               "notes": "met at a meetup"}


def test_update_contact_refuses_blanking_required_fields(tmp_path):
    store, _ = _seed(tmp_path)
    contact = store.add_contact(org="Acme", name="Dana")
    with pytest.raises(ValueError):
        store.update_contact(contact["id"], name="")
    with pytest.raises(KeyError):
        store.update_contact(9999, name="x")


def test_archive_contact_hides_without_deleting(tmp_path):
    store, _ = _seed(tmp_path)
    contact = store.add_contact(org="Acme", name="Dana")
    assert store.archive_contact(contact["id"])["changed"] is True
    assert store.archive_contact(contact["id"])["changed"] is False

    assert store.contacts_for("Acme") == []
    kept = store.contacts_for("Acme", include_archived=True)
    assert [c["id"] for c in kept] == [contact["id"]]
    assert kept[0]["archived"] == 1


# ---------------------------------------------------------------- touches

def test_add_touch_requires_role_and_date(tmp_path):
    store, ids = _seed(tmp_path)
    with pytest.raises(ValueError):
        store.add_touch(role_id=ids[0])
    with pytest.raises(ValueError):
        store.add_touch(date="2026-09-20")
    with pytest.raises(KeyError):
        store.add_touch(role_id="no-such-role", date="2026-09-20")
    with pytest.raises(KeyError):
        store.add_touch(role_id=ids[0], date="2026-09-20", contact_id=9999)
    with pytest.raises(ValueError):
        store.add_touch(role_id=ids[0], date="2026-09-20", mood="good")


def test_touches_for_newest_first_and_logged(tmp_path):
    store, ids = _seed(tmp_path)
    contact = store.add_contact(org="Acme", name="Dana")
    first = store.add_touch(role_id=ids[0], date="2026-09-12", channel="portal",
                            direction="out", summary="Applied")
    second = store.add_touch(role_id=ids[0], date="2026-09-20", channel="email",
                             direction="in", contact_id=contact["id"])
    store.add_touch(role_id=ids[1], date="2026-09-21", channel="email", direction="out")

    assert [t["id"] for t in store.touches_for(ids[0])] == [second["id"], first["id"]]
    fields = {e["field"] for e in _log(store, ids[0])}
    assert {f"touch:{first['id']}", f"touch:{second['id']}"} <= fields


def test_archive_touch_hides_without_deleting(tmp_path):
    store, ids = _seed(tmp_path)
    touch = store.add_touch(role_id=ids[0], date="2026-09-12")
    assert store.archive_touch(touch["id"])["changed"] is True
    assert store.archive_touch(touch["id"])["changed"] is False
    with pytest.raises(KeyError):
        store.archive_touch(9999)

    assert store.touches_for(ids[0]) == []
    assert len(store.touches_for(ids[0], include_archived=True)) == 1
    assert json.loads(_log(store, ids[0])[0]["new_value"]) == {"archived": 1}
