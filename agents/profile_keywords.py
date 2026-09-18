"""
Shared reader for the keyword lists in me/profile.md.

Every keyword a user cares about lives in ONE file, me/profile.md, under two
headings:

  ## Target Keywords    what you want. Each match RAISES a role's score, and
                        each term also generates a board query for the JobSpy
                        aggregator. A role that matches none of them is not
                        disqualified - it simply scores low and is dropped by
                        rules.min_match_score, which is the only threshold.

  ## Exclude Keywords   what you never want. Any match anywhere in a posting
                        kills the role outright, and each term is also passed
                        to the board as a `-term` so the noise is filtered
                        before it is ever downloaded.

Previously the gate list lived under "## Keywords to Track" and a second,
weaker list lived in job-sources.yaml as rules.must_have_keywords. The two
overlapped, so they were collapsed into "## Target Keywords".
"""
from pathlib import Path

TARGET_HEADING = "## Target Keywords"
EXCLUDE_HEADING = "## Exclude Keywords"


def parse_section(text, heading):
    """Bullets under `heading`, lowercased, until the next `##` heading.

    Template placeholders (`<!-- e.g. ... -->`) are skipped, so an untouched
    profile.md yields an empty list rather than example data.
    """
    out = []
    inside = False
    for line in (text or "").split("\n"):
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
