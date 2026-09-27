"""resume.pdf is parsed once per distinct PDF, not on every context_block call.

resume.txt is reused while the PDF's contents are unchanged (so hand edits to
it survive and a PDF pypdf has to repair only warns once), and re-parsed when
the PDF changes. Freshness is by content hash, not mtime: an old PDF copied in
with its date kept must still beat a newer placeholder resume.txt.
"""
import io
import os
import sys
import tempfile
from pathlib import Path

REPO = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(REPO / "agents"))

import pypdf
from context import _resume_text

fails = []
def check(name, cond, detail=""):
    print(f"{'ok  ' if cond else 'FAIL'} {name} {detail}")
    if not cond:
        fails.append(name)


def blank_pdf(width=200):
    """A real one-page PDF; width varies the bytes so two PDFs differ."""
    writer = pypdf.PdfWriter()
    writer.add_blank_page(width=width, height=200)
    buf = io.BytesIO()
    writer.write(buf)
    return buf.getvalue()


# The reader is faked so each PDF "contains" known text and parses are counted.
parses = []
class FakeReader:
    text = "Jane Doe\nStaff Engineer"
    def __init__(self, path):
        parses.append(path)
        page = type("Page", (), {"extract_text": lambda _self: FakeReader.text})()
        self.pages = [page]

pypdf.PdfReader = FakeReader

base = Path(tempfile.mkdtemp())
me = base / "me"
me.mkdir()
pdf, txt, stamp = me / "resume.pdf", me / "resume.txt", me / ".resume.pdf.sha256"

# ---------------- only resume.txt: read as before ----------------
txt.write_text("hand-written resume")
check("txt only: read as is", _resume_text(base) == "hand-written resume")
check("txt only: nothing parsed", parses == [])

# ---------------- placeholder txt newer than the PDF ----------------
txt.write_text("PLACEHOLDER - put your resume here")
pdf.write_bytes(blank_pdf())
os.utime(pdf, (1_000_000_000, 1_000_000_000))         # an old PDF, date kept on copy
check("newer placeholder txt does not beat the PDF",
      _resume_text(base) == FakeReader.text, repr(txt.read_text()))
check("first call parses once", len(parses) == 1, str(len(parses)))
check("first call writes resume.txt", txt.read_text() == FakeReader.text)
check("first call writes the fingerprint", stamp.exists())

# ---------------- same PDF: cached, hand edits kept ----------------
txt.write_text("Jane Doe\nStaff Engineer\nhand edit")
check("same PDF: resume.txt reused", _resume_text(base).endswith("hand edit"))
check("same PDF: not parsed again", len(parses) == 1, str(len(parses)))

# ---------------- changed PDF: parsed again ----------------
pdf.write_bytes(blank_pdf(width=300))
FakeReader.text = "Jane Doe\nPrincipal Engineer"
check("changed PDF: fresh text", _resume_text(base) == FakeReader.text)
check("changed PDF: parsed again", len(parses) == 2, str(len(parses)))

# ---------------- resume.txt deleted: parsed again ----------------
txt.unlink()
check("missing resume.txt: rebuilt", _resume_text(base) == FakeReader.text and txt.exists())
check("missing resume.txt: parsed again", len(parses) == 3, str(len(parses)))

if fails:
    print(f"\n{len(fails)} failed")
    sys.exit(1)
print("\nall passed")
