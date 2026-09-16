"""Ground truth context: resume + career goals. Shared by every LLM agent.

Both live in me/, the single local input folder (see me/README.md). Nothing
here is committed to git; templates/me/ ships the placeholder shape a fresh
checkout starts from.
"""
from pathlib import Path


def _resume_text(base_dir):
    me_dir = Path(base_dir) / "me"
    cached = me_dir / "resume.txt"
    pdf = me_dir / "resume.pdf"

    # A hand-provided resume.txt always wins - it may be edited by hand after
    # the first PDF parse, and re-parsing would silently discard those edits.
    if cached.exists() and not pdf.exists():
        return cached.read_text()

    if pdf.exists():
        from pypdf import PdfReader
        text = "".join(page.extract_text() or "" for page in PdfReader(pdf).pages)
        text = "\n".join(line.rstrip() for line in text.splitlines() if line.strip())
        cached.write_text(text)
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


def context_block(base_dir, max_resume_chars=6000):
    ctx = load_context(base_dir)
    return (
        "=== CANDIDATE POSITIONING (profile.md) ===\n"
        f"{ctx['goals']}\n\n"
        "=== CANDIDATE RESUME ===\n"
        f"{ctx['resume'][:max_resume_chars]}\n"
    )
