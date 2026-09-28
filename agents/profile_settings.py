"""
Reading and writing the profile fields the dashboard's Settings page edits.

Three files, three groups, one place that knows how to change each safely:

  goals     me/profile.md          one `## Section` at a time
  accounts  <workspace>/.env       GITHUB_USERNAME, BLUESKY_HANDLE - never secrets
  review    config/profile.yaml    always the WORKSPACE copy, never the tracked default

profile.md is written in place, not regenerated. For a list section (target
roles, keywords, topics, excludes) the new items are written into the lines the
old items occupied, in order: hint text, `<!-- ... -->` blocks (parked keywords
included), group labels and blank lines all stay where they were. Extra items
go after the last old one; surplus old ones are removed. A prose section keeps
everything above its first line of content and replaces the rest. Every write
keeps the previous file as me/profile.prev.md.

Every setter validates before it touches a file and raises ValueError with a
message fit for the page. Each returns (old, new) so the caller can log it.
"""
import json
import re
import sys
from pathlib import Path
from urllib.parse import urlparse

sys.path.insert(0, str(Path(__file__).resolve().parent))
sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from profile_keywords import strip_html_comments, unquote  # noqa: E402

PROFILE = "me/profile.md"
PREVIOUS = "me/profile.prev.md"
TEMPLATE = Path(__file__).resolve().parent.parent / "templates/me/profile.md"

# key -> (heading, kind, item or text length cap, max items, help)
# kind: "numbered" list, "bullets" list, or "text" (free prose).
GOAL_SECTIONS = {
    "target_roles": ("## Target Roles", "numbered", 160, 12,
                     "In priority order. The number is how the tracker's Role Cat refers to "
                     "each one, so reordering renumbers them."),
    "technical_areas": ("## Key Technical Areas", "bullets", 160, 40,
                        "Core skills and domains you want roles to match on."),
    "domain_expertise": ("## Domain Expertise", "text", 3000, None,
                         "What you actually do, at what scale, for whom. The fit judge leans on this most."),
    "target_keywords": ("## Target Keywords", "bullets", 80, 60,
                        "Raise a role's score and become board searches. Only the first 8 or so "
                        "become searches, so the strongest lead. Write them bare, without quotes."),
    "watch_topics": ("## Watch Topics", "bullets", 240, 30,
                     "Emerging themes for the Content Radar. Format: topic: why you are watching it."),
    "exclude_keywords": ("## Exclude Keywords", "bullets", 80, 60,
                         "A match in a role's title drops it. Whole words, case-insensitive."),
    "career_positioning": ("## Career Positioning", "text", 3000, None,
                           "How you want to be perceived professionally."),
    "persona": ("## Persona", "text", 2000, None,
                "Optional. How you want to come across; the Profile review judges your voice "
                "against it. Falls back to Career Positioning when empty."),
    "notes": ("## Notes", "text", 3000, None,
              "Anything else the model should weigh."),
}

ACCOUNT_KEYS = {"github_username": "GITHUB_USERNAME", "bluesky_handle": "BLUESKY_HANDLE"}
_GITHUB_RE = re.compile(r"^(?!-)(?!.*--)[A-Za-z0-9-]{1,39}(?<!-)$")
_HANDLE_RE = re.compile(r"^(?=.{3,253}$)([a-z0-9]([a-z0-9-]{0,61}[a-z0-9])?\.)+[a-z]([a-z0-9-]{0,61}[a-z0-9])?$")

# key -> (min, max) for the numeric review settings.
REVIEW_NUMBERS = {
    "linkedin_stale_days": (1, 365),
    "reevaluate_days": (1, 90),
    "pursued_min_fit": (0, 100),
    "pursued_window_days": (1, 730),
}
MAX_SITES = 20
MAX_SITE_PAGES = 50

_NUMBERED_RE = re.compile(r"^(\s*)\d+\.(\s|$)")
_BULLET_RE = re.compile(r"^(\s*)-(\s|$)")


# ---------------------------------------------------------------- profile.md

def _profile_path(base_dir):
    return Path(base_dir) / PROFILE


def _profile_text(base_dir):
    path = _profile_path(base_dir)
    if path.exists():
        return path.read_text()
    return TEMPLATE.read_text() if TEMPLATE.exists() else ""


def _section_span(lines, heading):
    """(start, end) line indexes of a section's body - after the heading, up to
    the next `## ` heading - or None when the heading is absent. Headings inside
    comments do not count."""
    masked = strip_html_comments("\n".join(lines)).split("\n")
    start = None
    for i, line in enumerate(masked):
        if start is None:
            if line.strip() == heading or line.strip().startswith(heading + " "):
                start = i + 1
        elif line.startswith("## "):
            return start, i
    return (start, len(lines)) if start is not None else None


def _hint_lines(masked, start, end):
    """Indexes of the `*( ... )*` explainer under a heading, which may wrap."""
    out, inside = set(), False
    for i in range(start, end):
        s = masked[i].strip()
        if not inside and s.startswith("*("):
            inside = True
        if inside:
            out.add(i)
            if s.endswith(")*"):
                inside = False
    return out


def _slots(masked, start, end, kind):
    """The list items of a section as [(first_line, last_line, indent, text)].
    An item is a bullet (or numbered) line outside any comment, plus the
    indented lines that continue it. Placeholder items that are empty once
    comments are removed still count as slots, so the template's `1.` `2.` `3.`
    are filled in rather than left behind."""
    pattern = _NUMBERED_RE if kind == "numbered" else _BULLET_RE
    slots, i = [], start
    while i < end:
        m = pattern.match(masked[i])
        if not m:
            i += 1
            continue
        first, parts = i, [masked[i][m.end():].strip()]
        j = i + 1
        while (j < end and masked[j].strip() and masked[j][:1] in (" ", "\t")
               and not _NUMBERED_RE.match(masked[j]) and not _BULLET_RE.match(masked[j])):
            parts.append(masked[j].strip())
            j += 1
        slots.append((first, j - 1, m.group(1), " ".join(p for p in parts if p)))
        i = j
    return slots


def _read_section(text, heading, kind):
    lines = text.split("\n")
    span = _section_span(lines, heading)
    if span is None:
        return [] if kind != "text" else ""
    start, end = span
    masked = strip_html_comments(text).split("\n")
    if kind == "text":
        hint = _hint_lines(masked, start, end)
        body = [masked[i] for i in range(start, end) if i not in hint]
        return re.sub(r"\n\s*\n(\s*\n)+", "\n\n", "\n".join(body)).strip()
    return [t for *_rest, t in _slots(masked, start, end, kind) if t]


def read_goals(base_dir):
    """{key: list or text} for every section in GOAL_SECTIONS."""
    text = _profile_text(base_dir)
    return {key: _read_section(text, heading, kind)
            for key, (heading, kind, *_rest) in GOAL_SECTIONS.items()}


def _clean_items(key, value):
    heading, kind, cap, max_items, _help = GOAL_SECTIONS[key]
    if not isinstance(value, list) or not all(isinstance(v, str) for v in value):
        raise ValueError(f"{key} must be a list of text")
    items, seen = [], set()
    for raw in value:
        item = " ".join(raw.split())                      # one line, single spaces
        item = re.sub(r"^(\d+\.|-)\s*", "", item)         # a pasted "1. " or "- "
        if key in ("target_keywords", "exclude_keywords"):
            item = unquote(item)
        if not item:
            continue
        if "<!--" in item or "-->" in item:
            raise ValueError("items cannot contain HTML comments")
        if len(item) > cap:
            raise ValueError(f"'{item[:30]}...' is too long (max {cap} characters)")
        fold = item.lower()
        if fold in seen:
            continue
        seen.add(fold)
        items.append(item)
    if len(items) > max_items:
        raise ValueError(f"too many items for {key}: {len(items)} (max {max_items})")
    return items


def _clean_text(key, value):
    _heading, _kind, cap, _max, _help = GOAL_SECTIONS[key]
    if not isinstance(value, str):
        raise ValueError(f"{key} must be text")
    text = "\n".join(line.rstrip() for line in value.replace("\r\n", "\n").split("\n")).strip()
    if len(text) > cap:
        raise ValueError(f"{key} is too long: {len(text)} characters (max {cap})")
    if "<!--" in text or "-->" in text:
        raise ValueError("text cannot contain HTML comments")
    if any(line.startswith("#") for line in text.split("\n")):
        raise ValueError("a line cannot start with '#' - it would start a new section")
    return text


def _write_list(lines, span, kind, items):
    start, end = span
    masked = strip_html_comments("\n".join(lines)).split("\n")
    slots = _slots(masked, start, end, kind)

    def fmt(i, indent):
        return f"{indent}{i + 1}. {items[i]}" if kind == "numbered" else f"{indent}- {items[i]}"

    if not slots:
        # No list yet: put it after the hint and any leading comment/prose,
        # i.e. at the end of the section, before its trailing blank lines.
        at = end
        while at > start and not lines[at - 1].strip():
            at -= 1
        block = ([""] if at > start else []) + [fmt(i, "") for i in range(len(items))] + [""]
        return lines[:at] + block + lines[at:]

    out, prev = [], start
    out.extend(lines[:start])
    for n, (first, last, indent, _text) in enumerate(slots):
        out.extend(lines[prev:first])
        if n < len(items):
            out.append(fmt(n, indent))
        # last slot: every remaining item goes right after it
        if n == len(slots) - 1:
            out.extend(fmt(i, indent) for i in range(len(slots), len(items)))
        prev = last + 1
    out.extend(lines[prev:])
    return out


def _write_text(lines, span, text):
    start, end = span
    masked = strip_html_comments("\n".join(lines)).split("\n")
    hint = _hint_lines(masked, start, end)
    # Keep the preamble: everything up to the first real line of content.
    first = next((i for i in range(start, end) if masked[i].strip() and i not in hint), end)
    # Comment blocks below the content are kept too, moved after the new text.
    tail_comments = []
    for i in range(first, end):
        if lines[i].strip() and not masked[i].strip():
            tail_comments.append(lines[i])
    preamble = lines[start:first]
    while preamble and not preamble[-1].strip():
        preamble.pop()
    body = [""] + (text.split("\n") if text else [])
    body += ([""] + tail_comments if tail_comments else [])
    return lines[:start] + preamble + body + [""] + lines[end:]


def set_goal(base_dir, key, value):
    """Write one section of me/profile.md. Returns (old, new)."""
    if key not in GOAL_SECTIONS:
        raise ValueError(f"not a profile section: {key!r}")
    heading, kind, *_rest = GOAL_SECTIONS[key]
    new = _clean_text(key, value) if kind == "text" else _clean_items(key, value)

    text = _profile_text(base_dir)
    old = _read_section(text, heading, kind)
    # Compared normalised: `"junior"` and `junior` are the same keyword to the
    # pipeline, so saving an untouched list must not rewrite the file.
    clean = _clean_text if kind == "text" else _clean_items
    try:
        unchanged = clean(key, old) == new
    except ValueError:          # a hand-edited section can exceed the caps
        unchanged = False
    if unchanged:
        return old, new

    lines = text.split("\n")
    span = _section_span(lines, heading)
    if span is None:
        while lines and not lines[-1].strip():
            lines.pop()
        lines += ["", heading, ""]
        span = (len(lines), len(lines))
        lines.append("")
    lines = _write_list(lines, span, kind, new) if kind != "text" else _write_text(lines, span, new)
    out = "\n".join(lines)
    out = re.sub(r"\n{3,}", "\n\n", out).rstrip("\n") + "\n"

    path = _profile_path(base_dir)
    path.parent.mkdir(parents=True, exist_ok=True)
    if path.exists():
        (Path(base_dir) / PREVIOUS).write_text(path.read_text())
    path.write_text(out)
    return old, _read_section(out, heading, kind)


# ---------------------------------------------------------------- accounts

def read_accounts(base_dir):
    """Identity from the workspace .env. The app password is reported as set or
    not, and its value never leaves the file."""
    import workspace

    env = workspace.env_path(base_dir)
    return {
        "github_username": workspace.read_env(env, "GITHUB_USERNAME"),
        "bluesky_handle": workspace.read_env(env, "BLUESKY_HANDLE"),
        "bluesky_app_password_set": bool(workspace.read_env(env, "BLUESKY_APP_PASSWORD")),
    }


def set_account(base_dir, key, value):
    import workspace

    if key not in ACCOUNT_KEYS:
        raise ValueError(f"not an editable account field: {key!r}")
    if not isinstance(value, (str, type(None))):
        raise ValueError(f"{key} must be text")
    value = (value or "").strip()
    if key == "github_username":
        value = value.lstrip("@")
        if value and not _GITHUB_RE.match(value):
            raise ValueError("not a valid GitHub username (letters, digits and single hyphens, up to 39)")
    else:
        value = value.lstrip("@").lower()
        if value and not _HANDLE_RE.match(value):
            raise ValueError("not a valid BlueSky handle - it looks like a domain, e.g. name.bsky.social")
    env = workspace.env_path(base_dir)
    old = workspace.read_env(env, ACCOUNT_KEYS[key])
    if old != value:
        workspace.set_env(env, ACCOUNT_KEYS[key], value)
    return old, value


# ---------------------------------------------------------------- review settings

def read_review(base_dir):
    from profile_snapshot import load_config

    config = load_config(base_dir)
    return {"sites": config["sites"], **{k: config[k] for k in REVIEW_NUMBERS}}


def _clean_sites(value):
    if not isinstance(value, list):
        raise ValueError("sites must be a list")
    sites, seen = [], set()
    for entry in value:
        if isinstance(entry, str):
            entry = {"url": entry}
        if not isinstance(entry, dict):
            raise ValueError("each site needs a url")
        url = str(entry.get("url") or "").strip()
        if not url:
            continue
        parsed = urlparse(url)
        if parsed.scheme not in ("http", "https") or not parsed.netloc:
            raise ValueError(f"not an http(s) address: {url}")
        try:
            pages = int(entry.get("max_pages") or 0)
        except (TypeError, ValueError):
            raise ValueError(f"max_pages must be a whole number for {url}")
        if not 0 <= pages <= MAX_SITE_PAGES:
            raise ValueError(f"max_pages must be 0 to {MAX_SITE_PAGES} for {url}")
        if url in seen:
            continue
        seen.add(url)
        sites.append({"url": url, "max_pages": pages})
    if len(sites) > MAX_SITES:
        raise ValueError(f"too many sites: {len(sites)} (max {MAX_SITES})")
    return sites


def _review_yaml(config, extra):
    import yaml

    sites = "sites: []" if not config["sites"] else "sites:\n" + "\n".join(
        f"  - url: {json.dumps(s['url'])}\n    max_pages: {s['max_pages']}" for s in config["sites"])
    out = f"""# Mission Control - the Profile tab, for this workspace.
#
# Written by the dashboard's Settings page; editing by hand is fine too. It
# overrides the tracked default in the repo's config/profile.yaml.

# Personal or portfolio sites to read, alongside LinkedIn, GitHub and BlueSky.
# max_pages: 0 reads the page at `url` only; N also reads up to N more pages
# listed in the site's sitemap.xml.
{sites}

# A LinkedIn export older than this many days is flagged as stale.
linkedin_stale_days: {config['linkedin_stale_days']}

# What the evaluation judges your profile against:
reevaluate_days: {config['reevaluate_days']}          # re-judge at least this often, even with nothing changed
pursued_min_fit: {config['pursued_min_fit']}         # fit score (0-100) at which a role counts as pursued, applied or not
pursued_window_days: {config['pursued_window_days']}     # how far back pursued roles are read
"""
    if extra:
        out += "\n" + yaml.safe_dump(extra, sort_keys=False)
    return out


def set_review(base_dir, key, value):
    """Write one review setting to <workspace>/config/profile.yaml. Keys this
    page does not know about are carried over untouched."""
    import yaml
    import workspace

    if key == "sites":
        value = _clean_sites(value)
    elif key in REVIEW_NUMBERS:
        lo, hi = REVIEW_NUMBERS[key]
        if isinstance(value, bool):
            raise ValueError(f"{key} must be a whole number")
        try:
            value = int(str(value).strip())
        except (TypeError, ValueError):
            raise ValueError(f"{key} must be a whole number")
        if not lo <= value <= hi:
            raise ValueError(f"{key} must be between {lo} and {hi}")
    else:
        raise ValueError(f"not an editable review setting: {key!r}")

    current = read_review(base_dir)
    old = current[key]
    if old == value:
        return old, value
    source = workspace.config_path(base_dir, "profile.yaml")
    raw = (yaml.safe_load(source.read_text()) or {}) if source.exists() else {}
    extra = {k: v for k, v in raw.items() if k not in current}
    current[key] = value

    target = Path(base_dir) / "config" / "profile.yaml"
    target.parent.mkdir(parents=True, exist_ok=True)
    target.write_text(_review_yaml(current, extra))
    return old, value


# ---------------------------------------------------------------- together

GROUPS = {"goals": set_goal, "accounts": set_account, "review": set_review}


def read_all(base_dir):
    return {"goals": read_goals(base_dir), "accounts": read_accounts(base_dir),
            "review": read_review(base_dir)}


def spec():
    """What the page needs to draw the goals editors, in profile.md order (a
    list, since the page's JSON has its keys sorted)."""
    return [{"key": key, "heading": h.lstrip("# "), "kind": kind, "cap": cap, "max": mx, "help": hp}
            for key, (h, kind, cap, mx, hp) in GOAL_SECTIONS.items()]


def set_field(base_dir, group, field, value):
    if group not in GROUPS:
        raise ValueError(f"unknown settings group: {group!r}")
    return GROUPS[group](base_dir, field, value)
