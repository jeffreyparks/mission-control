"""Ground truth context: resume + career goals. Shared by every LLM agent.

Both live in me/, the single local input folder (see me/README.md). Nothing
here is committed to git; templates/me/ ships the placeholder shape a fresh
checkout starts from.
"""
import hashlib
import re
from pathlib import Path


def _resume_text(base_dir):
    me_dir = Path(base_dir) / "me"
    cached = me_dir / "resume.txt"
    pdf = me_dir / "resume.pdf"
    stamp = me_dir / ".resume.pdf.sha256"

    if pdf.exists():
        # resume.txt is the parse of the PDF whose hash is in the stamp. Reuse
        # it until the PDF's contents change: re-parsing every call is wasted
        # work (and noisy for PDFs pypdf has to repair), and would discard hand
        # edits to resume.txt. Contents, not mtime - copy2 and Finder keep an
        # old PDF's date, which could make a placeholder resume.txt look newer.
        digest = hashlib.sha256(pdf.read_bytes()).hexdigest()
        if cached.exists() and stamp.exists() and stamp.read_text().strip() == digest:
            return cached.read_text()

        from pypdf import PdfReader
        text = "".join(page.extract_text() or "" for page in PdfReader(pdf).pages)
        text = "\n".join(line.rstrip() for line in text.splitlines() if line.strip())
        cached.write_text(text)
        stamp.write_text(digest + "\n")
        return text

    if cached.exists():
        return cached.read_text()

    return ""


def load_context(base_dir):
    """Return the ground-truth block injected into every analysis prompt."""
    base_dir = Path(base_dir)
    goals = (base_dir / "me/profile.md")
    goals_text = goals.read_text() if goals.exists() else ""
    return {
        "resume": _resume_text(base_dir),
        "goals": goals_text,
    }


def learned_block(base_dir):
    """The candidate's APPROVED learned preferences (me/learned.md), or "".

    Written by agents/preferences.py from past decisions, as a draft the
    candidate approves - an unapproved me/learned.draft.md is never read here.
    HTML comments (the draft's evidence counts and review notes) are stripped:
    the judge reads the guidance, not the bookkeeping."""
    path = Path(base_dir) / "me/learned.md"
    if not path.exists():
        return ""
    text = re.sub(r"<!--.*?-->", "", path.read_text(), flags=re.S)
    text = "\n".join(line for line in text.splitlines() if line.strip()).strip()
    if not text:
        return ""
    return (
        "=== LEARNED PREFERENCES (from the candidate's own past decisions) ===\n"
        "Weigh these alongside the profile. They record what the candidate has\n"
        "actually passed on and pursued; they never override facts in the JD.\n"
        f"{text}\n"
    )


def context_block(base_dir, max_resume_chars=6000):
    ctx = load_context(base_dir)
    return (
        "=== CANDIDATE POSITIONING (profile.md) ===\n"
        f"{ctx['goals']}\n\n"
        "=== CANDIDATE RESUME ===\n"
        f"{ctx['resume'][:max_resume_chars]}\n"
    )
