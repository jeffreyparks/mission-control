"""
Shared reader for the keyword lists in me/profile.md.

Every keyword a user cares about lives in ONE file, me/profile.md, under two
headings:

  ## Target Keywords    what you want. Each match RAISES a role's score, and
                        each term also generates a board query for the JobSpy
                        and Built In aggregators. Matches count anywhere in the
                        TITLE or the DESCRIPTION. A role that matches none of
                        them is not disqualified - it simply scores low and is
                        dropped by rules.min_match_score, the only threshold.

  ## Exclude Keywords   what you never want. A match in the TITLE ONLY - never
                        the description - kills the posting immediately, before
                        it enters the tracker and before any LLM call is spent
                        on it. Each term is also passed to the boards that
                        support it as a `-term`, so the noise is filtered before
                        it is ever downloaded. Title-only is deliberate: a
                        description that merely mentions "mentoring interns" is
                        not an intern role.

These are the ONLY keyword lists in the project. Earlier versions also kept
rules.must_have_keywords, rules.auto_close_titles and a per-aggregator
`queries:` list in job-sources.yaml; all three overlapped these two lists and
were removed.
"""
import re
from pathlib import Path

TARGET_HEADING = "## Target Keywords"
EXCLUDE_HEADING = "## Exclude Keywords"


def strip_html_comments(text):
    """Blank out every `<!-- ... -->` span, including multi-line ones.

    Commenting a block of bullets out is the documented way to park a keyword
    in profile.md ("promote any of them by moving it up"), so a parked bullet
    must never reach the keyword lists. Line breaks are preserved so the
    line-by-line section parser still sees the same structure.
    """
    return re.sub(
        r"<!--.*?(?:-->|$)",
        lambda m: "\n" * m.group(0).count("\n"),
        text or "",
        flags=re.DOTALL,
    )


def parse_section(text, heading):
    """Bullets under `heading`, lowercased, until the next `##` heading.

    Template placeholders and parked keywords inside `<!-- ... -->` comments
    are skipped, so an untouched profile.md yields an empty list rather than
    example data.
    """
    out = []
    inside = False
    for line in strip_html_comments(text).split("\n"):
        if line.strip() == heading or line.strip().startswith(heading + " "):
            inside = True
            continue
        if inside:
            if line.startswith("##"):
                break
            if line.strip().startswith("-"):
                value = line.strip().lstrip("-").strip().lower()
                if value and not value.startswith("<!--"):
                    out.append(value)
    return out


def load_keywords(base_dir):
    """Read both lists from <base_dir>/me/profile.md.

    Returns {"target": [...], "exclude": [...]}. A missing file gives two empty
    lists; the caller decides whether that is worth a warning.
    """
    path = Path(base_dir) / "me" / "profile.md"
    if not path.exists():
        return {"target": [], "exclude": []}
    text = path.read_text()
    return {
        "target": parse_section(text, TARGET_HEADING),
        "exclude": parse_section(text, EXCLUDE_HEADING),
    }


def build_board_query(term, excludes=(), max_excludes=8):
    """Turn one target keyword into an Indeed/LinkedIn search string.

    Indeed searches the description as well as the title, which is the usual
    source of its noise, so the query is made as specific as the syntax allows:

      - a multi-word term is quoted, forcing an exact-phrase match
      - each exclude term becomes `-term`, dropped by the board itself

    Keyword lists are also read by the GitHub/BlueSky profile scanners, where
    slug spellings like `causal-inference` are normal. Hyphens are turned back
    into spaces so the same list works as a job query.

    `max_excludes` keeps the query from growing past what the boards accept;
    every exclude term is applied again locally as a hard filter regardless.
    """
    phrase = str(term or "").replace("-", " ").strip()
    if not phrase:
        return ""
    if " " in phrase:
        phrase = f'"{phrase}"'
    parts = [phrase]
    for raw in list(excludes)[:max_excludes]:
        word = str(raw or "").replace("-", " ").strip()
        if not word:
            continue
        parts.append(f'-"{word}"' if " " in word else f"-{word}")
    return " ".join(parts)


def _exclude_pattern(term):
    """Whole-phrase matcher for one exclude term.

    Case-insensitive, because a posting titled "Data Science Intern" must be
    caught by the exclude term `intern`.

    Whole-phrase, because plain substring matching is quietly wrong here:
    `intern` inside "internal", "international" or "alternative" would close
    roles the user never meant to exclude. A trailing plural is still caught
    ("interns"), and spaces match hyphens, so `entry level` also matches
    "entry-level".
    """
    words = [re.escape(w) for w in re.split(r"[\s-]+", str(term).strip()) if w]
    if not words:
        return None
    return re.compile(r"(?<!\w)" + r"[\s\-]+".join(words) + r"s?(?!\w)", re.IGNORECASE)


def matches_exclude(text, excludes):
    """The first exclude term present in `text`, or None.

    Returning the term rather than a bool lets callers say WHICH word killed a
    role, which is the difference between a trustworthy filter and a black box.
    """
    if not text:
        return None
    for term in excludes or []:
        pattern = _exclude_pattern(term)
        if pattern and pattern.search(text):
            return str(term)
    return None
