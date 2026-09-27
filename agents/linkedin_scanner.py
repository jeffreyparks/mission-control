"""
LinkedIn collector: your LinkedIn profile, from your own export, as one snapshot.

LinkedIn has no usable API for this and scraping breaks its terms, so this reads
what you download yourself into me/linkedin/ (see me/linkedin/README.md):

  linkedin-export.zip   the full data export: profile, positions, skills, posts
  profile.pdf           "Save to PDF" from your profile page: text only

When both are present the NEWER file wins, so a fresh PDF is not shadowed by a
months-old zip. An export older than `linkedin_stale_days` (config/profile.yaml,
default STALE_DAYS) is flagged in `stats` - the snapshot then describes an old
profile, and anything judging it should say so.
"""
import csv
import hashlib
import re
import zipfile
from datetime import datetime
from pathlib import Path

from profile_snapshot import make_snapshot

STALE_DAYS = 30
SOURCE_KEY = "linkedin:export"


class LinkedInScanner:
    def __init__(self, base_dir, stale_days=STALE_DAYS):
        self.base_dir = Path(base_dir)
        self.stale_days = stale_days
        self.dir = self.base_dir / "me/linkedin"
        self.export_path = self.dir / "linkedin-export.zip"
        self.pdf_path = self.dir / "profile.pdf"
        self.extract_dir = self.dir / "extracted"

    def collect(self):
        """A snapshot of the newer export, or None when there is none."""
        present = [p for p in (self.export_path, self.pdf_path) if p.exists()]
        if not present:
            print(f"   linkedin: no export - drop linkedin-export.zip or profile.pdf in {self.dir}")
            return None
        path = max(present, key=lambda p: p.stat().st_mtime)
        identity, items = (self._from_zip() if path == self.export_path else self._from_pdf())

        exported = datetime.fromtimestamp(path.stat().st_mtime)
        age = (datetime.now() - exported).days
        post_dates = [i["date"] for i in items if i["kind"] == "post" and i["date"]]
        stats = {
            "export_file": path.name,
            "export_date": exported.strftime("%Y-%m-%d"),
            "age_days": age,
            "stale": age > self.stale_days,
            "positions": sum(1 for i in items if i["kind"] == "position"),
            "skills": sum(1 for i in items if i["kind"] == "skill"),
            "posts": len(post_dates),
            "last_activity": max(post_dates) if post_dates else None,
        }
        return make_snapshot("linkedin", SOURCE_KEY, identity, items, stats)

    # ---------- zip export ----------

    def _from_zip(self):
        self.extract_dir.mkdir(parents=True, exist_ok=True)
        with zipfile.ZipFile(self.export_path) as archive:
            archive.extractall(self.extract_dir)

        profile = (self._csv("Profile.csv") or [{}])[0]
        identity = {
            "name": " ".join(filter(None, [profile.get("First Name"), profile.get("Last Name")])),
            "headline": profile.get("Headline"),
            "about": scrub(profile.get("Summary")),
            "industry": profile.get("Industry"),
            "location": profile.get("Geo Location"),
        }

        items = []
        for p in self._csv("Positions.csv"):
            title, company = p.get("Title", ""), p.get("Company Name", "")
            items.append({
                "kind": "position",
                "id": f"{company}|{title}|{p.get('Started On', '')}",
                "date": _month(p.get("Started On")),
                "title": " at ".join(filter(None, [title, company])),
                "text": scrub(p.get("Description", "")),
                "meta": {"finished": p.get("Finished On") or None},
            })
        for s in self._csv("Skills.csv"):
            if s.get("Name"):
                items.append({"kind": "skill", "id": s["Name"], "title": s["Name"]})
        for post in self._csv("Posts.csv"):
            text = scrub(post.get("ShareCommentary") or post.get("Content") or "")
            date = (post.get("Date") or "")[:10] or None
            items.append({
                "kind": "post",
                "id": post.get("ShareLink") or _digest(f"{date}|{text}"),
                "date": date,
                "text": text,
                "url": post.get("ShareLink") or post.get("SharedUrl") or None,
            })
        return identity, items

    def _csv(self, name):
        """Rows of one CSV in the export, wherever in the archive it sits."""
        matches = sorted(self.extract_dir.rglob(name))
        if not matches:
            return []
        try:
            with open(matches[0], encoding="utf-8") as handle:
                return list(csv.DictReader(handle))
        except (OSError, csv.Error, UnicodeDecodeError) as exc:
            print(f"   linkedin: could not read {name}: {exc}")
            return []

    # ---------- PDF export ----------

    def _from_pdf(self):
        from pypdf import PdfReader

        text = "\n".join(page.extract_text() or "" for page in PdfReader(self.pdf_path).pages)
        return parse_pdf_text(text)


# ---------- PDF parsing ----------

# LinkedIn's "Save to PDF" puts a sidebar first - Contact, Top Skills, Languages,
# Certifications... - then the name, the headline (wrapped over several lines),
# the location, and "Summary" or "Experience".
SIDEBAR_HEADINGS = ("Contact", "Top Skills", "Languages", "Certifications", "Honors-Awards",
                    "Publications", "Patents")
BODY_HEADINGS = ("Summary", "Experience", "Education")
PHONE_RE = re.compile(r"\+?\(?\d{3}\)?[\s.\-]?\d{3}[\s.\-]?\d{4}(\s*\((Mobile|Home|Work)\))?")
EMAIL_RE = re.compile(r"[\w.+-]+@[\w-]+\.[\w.-]+")


def scrub(text):
    """Phone numbers and email addresses out - they are not profile content."""
    return EMAIL_RE.sub("", PHONE_RE.sub("", text or ""))


def _name_shaped(line):
    words = line.split()
    return (1 < len(words) <= 4 and len(line) <= 40 and "|" not in line
            and not any(ch.isdigit() for ch in line)
            and all(w[0].isupper() for w in words if w[0].isalpha()))


def parse_pdf_text(text):
    """(identity, items) from the text of a LinkedIn profile PDF."""
    lines = [line.strip() for line in (text or "").splitlines() if line.strip()]
    body = next((n for n, line in enumerate(lines) if line in BODY_HEADINGS), None)

    name = headline = location = ""
    name_at = None
    if body is not None and body >= 2:
        location = lines[body - 1]
        for n in range(body - 2, -1, -1):
            if lines[n] in SIDEBAR_HEADINGS:
                break
            if _name_shaped(lines[n]):
                name_at = n
                break
        if name_at is not None:
            name = lines[name_at]
            headline = " ".join(lines[name_at + 1:body - 1])

    # Contact block: from "Contact" to the next sidebar heading. Never kept.
    kept, in_contact = [], False
    for n, line in enumerate(lines):
        if line == "Contact":
            in_contact = True
            continue
        if in_contact and (line in SIDEBAR_HEADINGS or n == name_at):
            in_contact = False
        if not in_contact:
            kept.append(line)
    full = scrub("\n".join(kept)).strip()

    identity = {"name": name, "headline": scrub(headline).strip(), "location": location}
    items = [{"kind": "section", "id": "full_text", "title": "Profile (PDF)", "text": full}]
    in_skills = False
    for n, line in enumerate(lines):
        if line == "Top Skills":
            in_skills = True
            continue
        if in_skills:
            if line in SIDEBAR_HEADINGS or line in BODY_HEADINGS or n == name_at:
                break
            if len(line) > 2:
                items.append({"kind": "skill", "id": line, "title": line})
    return identity, items


def _month(value):
    """LinkedIn's 'Mar 2021' -> '2021-03-01'; anything else passes through."""
    value = (value or "").strip()
    for fmt in ("%b %Y", "%Y-%m-%d", "%Y"):
        try:
            return datetime.strptime(value, fmt).strftime("%Y-%m-%d")
        except ValueError:
            continue
    return value or None


def _digest(text):
    return hashlib.sha256(text.encode("utf-8")).hexdigest()[:16]


if __name__ == "__main__":
    import json
    snap = LinkedInScanner(Path(__file__).resolve().parent.parent).collect()
    if snap:
        print(json.dumps({k: v for k, v in snap.items() if k != "items"}, indent=2))
        print(f"{len(snap['items'])} items")
