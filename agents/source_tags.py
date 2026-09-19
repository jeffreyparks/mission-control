"""Where a tracked role came from.

One string per posting, stored in the tracker's `Source` column and the store's
`source` field: "Anthropic (Greenhouse)", "JobSpy (LinkedIn)", "BuiltIn",
"Manual". Live scanners set it at fetch time, where the board is known for
certain. This module is the fallback for rows that predate that - and for the
one-off backfill - which can only read the posting URL.
"""
from urllib.parse import urlparse

MANUAL = "Manual"

# host fragment -> board name. Checked as a substring of the hostname, so
# "job-boards.greenhouse.io" and "boards.greenhouse.io" both match.
BOARDS = (
    ("greenhouse.io", "Greenhouse"),
    ("lever.co", "Lever"),
    ("ashbyhq.com", "Ashby"),
    ("myworkdayjobs.com", "Workday"),
)

# host fragment -> full source label, for boards that are not company-specific.
AGGREGATORS = (
    ("builtin.com", "BuiltIn"),
    ("indeed.com", "JobSpy (Indeed)"),
    ("linkedin.com", "JobSpy (LinkedIn)"),
    ("glassdoor.com", "JobSpy (Glassdoor)"),
    ("ziprecruiter.com", "JobSpy (ZipRecruiter)"),
)


def label_for(url, org=None):
    """Best-effort source label from a posting URL.

    A company board becomes "<Org> (Greenhouse)"; an aggregator becomes its own
    name. Anything unrecognised - a careers page pasted in by hand, or no URL
    at all - is "Manual", because that is how such a row got into the tracker.
    """
    host = (urlparse(url or "").hostname or "").lower()
    if not host:
        return MANUAL
    for fragment, label in AGGREGATORS:
        if fragment in host:
            return label
    for fragment, board in BOARDS:
        if fragment in host:
            name = (org or "").strip()
            return f"{name} ({board})" if name else board
    return MANUAL
