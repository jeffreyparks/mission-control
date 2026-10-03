"""Fit rules: agents/fit_rules.py reads and writes the approved learned
preferences and profile review guidance, and the Settings page shows them.
All values are made up."""
import sys
from pathlib import Path

import pytest

REPO = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(REPO))
sys.path.insert(0, str(REPO / "agents"))
sys.path.insert(0, str(REPO / "render"))

import fit_rules as fr  # noqa: E402
import build  # noqa: E402
from context import guidance_block, learned_block  # noqa: E402

LEARNED_DRAFT = """# Learned preferences

<!-- DRAFT generated 2026-09-01 from 40 decisions in the last
     180 days (4 pursued, 36 passed). Nothing here is used until approved. -->

## Weigh down

- Treat widget makers as a weak fit.
  <!-- evidence: industry = Widgets: passed 9, pursued 0 of 9 -->

## Weigh up

- Do not screen out Lead titles on level alone.
  <!-- evidence: level = Lead: passed 8, pursued 3 of 11 -->

<!-- considered and dropped:
     - function = Sales (avoid): the model already follows it
-->
"""

GUIDANCE_DRAFT = """# Profile review guidance

<!-- DRAFT generated 2026-09-02 from 3 dismissed findings -->
<!-- evidence-hash: abc123 -->

- On BlueSky, do not flag jokes as a register clash.
  <!-- evidence: dismissed 'in-joke' because: it is informal -->
"""


@pytest.fixture
def ws(tmp_path):
    (tmp_path / "me").mkdir()
    (tmp_path / "me/learned.draft.md").write_text(LEARNED_DRAFT)
    (tmp_path / "me/profile-guidance.draft.md").write_text(GUIDANCE_DRAFT)
    return tmp_path


def test_reads_a_draft_with_its_groups_evidence_and_notes(ws):
    r = fr.read(ws, "learned")
    assert r["approved"] == [] and r["draft_date"] == "2026-09-01"
    assert [(d["group"], d["text"]) for d in r["draft"]] == [
        ("Weigh down", "Treat widget makers as a weak fit."),
        ("Weigh up", "Do not screen out Lead titles on level alone.")]
    assert r["draft"][0]["evidence"] == "industry = Widgets: passed 9, pursued 0 of 9"
    assert len(r["draft_notes"]) == 1 and "already follows" in r["draft_notes"][0]
    assert not any(d["adopted"] for d in r["draft"])
    g = fr.read(ws, "guidance")
    assert g["groups"] == [] and g["draft"][0]["group"] is None


def test_adopting_writes_the_approved_file_the_model_reads(ws):
    draft = fr.read(ws, "learned")["draft"]
    assert learned_block(ws) == ""
    fr.write(ws, "learned", draft)
    block = learned_block(ws)
    assert "Treat widget makers as a weak fit." in block and "evidence" not in block
    after = fr.read(ws, "learned")
    assert [r["text"] for r in after["approved"]] == [d["text"] for d in draft]
    assert all(d["adopted"] for d in after["draft"])
    assert after["approved"][1]["evidence"].startswith("level = Lead")      # evidence survives


def test_a_cli_approved_file_keeps_its_notes_when_edited_here(ws):
    (ws / "me/learned.md").write_text(LEARNED_DRAFT)            # what `mc preferences --approve` leaves
    rules = fr.read(ws, "learned")["approved"]
    assert len(rules) == 2 and fr.read(ws, "learned")["approved_notes"]
    fr.write(ws, "learned", rules[:1])
    text = (ws / "me/learned.md").read_text()
    assert "considered and dropped" in text and "DRAFT generated" not in text
    assert learned_block(ws).count("- ") == 1


def test_edit_regroup_add_and_remove(ws):
    fr.write(ws, "learned", fr.read(ws, "learned")["draft"])
    rules = fr.read(ws, "learned")["approved"]
    rules[0]["text"] = "Treat widget makers as a weak fit, whatever the title."
    rules[1]["group"] = "Weigh down"
    rules.append({"group": "Weigh up", "text": "Prefer roles that own a team.", "evidence": ""})
    fr.write(ws, "learned", rules)
    got = fr.read(ws, "learned")["approved"]
    assert [(r["group"], r["text"]) for r in got] == [
        ("Weigh down", "Treat widget makers as a weak fit, whatever the title."),
        ("Weigh down", "Do not screen out Lead titles on level alone."),
        ("Weigh up", "Prefer roles that own a team.")]
    assert got[2]["evidence"] == "written by you"
    assert (ws / "me/learned.prev.md").exists()
    fr.write(ws, "learned", got[:1])
    assert len(fr.read(ws, "learned")["approved"]) == 1


def test_saving_the_same_rules_leaves_the_file_alone(ws):
    fr.write(ws, "guidance", fr.read(ws, "guidance")["draft"])
    path = ws / "me/profile-guidance.md"
    before = path.read_text()
    fr.write(ws, "guidance", fr.read(ws, "guidance")["approved"])
    assert path.read_text() == before and not (ws / "me/profile-guidance.prev.md").exists()
    assert "do not flag jokes" in guidance_block(ws)


def test_no_rules_leaves_the_models_nothing(ws):
    fr.write(ws, "learned", fr.read(ws, "learned")["draft"])
    fr.write(ws, "learned", [{"text": "  "}])           # a blank row is dropped
    assert (ws / "me/learned.md").read_text() == "" and learned_block(ws) == ""


def test_rejects_bad_rules(ws):
    for bad in ("x" * 601, "# heading", "a <!-- sneaky -->"):
        with pytest.raises(ValueError):
            fr.write(ws, "learned", [{"text": bad}])
    with pytest.raises(ValueError):
        fr.write(ws, "learned", [{"text": f"rule {i}"} for i in range(fr.MAX_RULES + 1)])
    with pytest.raises(ValueError):
        fr.write(ws, "nonsense", [])


def test_settings_page_shows_every_rule_and_what_the_judge_reads(ws):
    (ws / "me/profile.md").write_text("# Goals\n\n## Notes\n\n- Seniority floor: Director.\n")
    (ws / "me/resume.txt").write_text("Built things.")
    fr.write(ws, "learned", fr.read(ws, "learned")["draft"])
    (ws / "artifacts/html").mkdir(parents=True)
    html = build.render_settings(build._env(), write=False, base=ws)[1]
    assert "Fit rules" in html and 'id="rules-job"' in html and 'id="rules-profile"' in html
    assert "Treat widget makers as a weak fit." in html            # in the settings data
    assert "=== LEARNED PREFERENCES" in html and "Seniority floor: Director." in html   # the judge's prompt
    assert "TASK: HONEST FIT ANALYSIS" in html
