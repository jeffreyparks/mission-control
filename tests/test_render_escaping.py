"""Every page template is autoescaped.

The pages show text scraped from job boards and public profiles, and the
dashboard that serves them has write routes on the same origin, so a posting
title must never reach the page as markup.
"""
import sys
from pathlib import Path

import pytest

REPO = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(REPO / "render"))

import build  # noqa: E402

TEMPLATES = sorted(p.name for p in (REPO / "render").glob("*.j2"))


@pytest.mark.parametrize("name", TEMPLATES)
def test_template_is_autoescaped(name):
    assert build._env().autoescape(name) is True


def test_a_rendered_value_is_escaped():
    html = build._env().get_template("_nav.html.j2").render(subtitle="<script>alert(1)</script>")
    assert "<script>alert(1)</script>" not in html
    assert "&lt;script&gt;alert(1)&lt;/script&gt;" in html
