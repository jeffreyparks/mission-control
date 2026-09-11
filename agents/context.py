"""Ground truth context: resume + career goals. Shared by every LLM agent."""
from pathlib import Path


def _resume_text(base_dir):
    resume_dir = Path(base_dir) / "data/resumes"
    cached = resume_dir / "resume.txt"
    if cached.exists():
        return cached.read_text()

    pdfs = sorted(resume_dir.glob("*.pdf"))
    if not pdfs:
        return ""

    from pypdf import PdfReader
    text = "".join(page.extract_text() or "" for page in PdfReader(pdfs[0]).pages)
    text = "\n".join(line.rstrip() for line in text.splitlines() if line.strip())
    cached.write_text(text)
    return text


def load_context(base_dir):
    """Return the ground-truth block injected into every analysis prompt."""
    base_dir = Path(base_dir)
    goals = (base_dir / "config/career-goals.md")
    goals_text = goals.read_text() if goals.exists() else ""
    return {
        "resume": _resume_text(base_dir),
        "goals": goals_text,
    }


def context_block(base_dir, max_resume_chars=6000):
    ctx = load_context(base_dir)
    return (
        "=== CANDIDATE POSITIONING (career-goals.md) ===\n"
        f"{ctx['goals']}\n\n"
        "=== CANDIDATE RESUME ===\n"
        f"{ctx['resume'][:max_resume_chars]}\n"
    )
