"""HTML maila: wrapper tabel i kolor procentów."""

import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from email_sender import colorize_percentages, markdown_to_html, wrap_tables  # noqa: E402


def test_wrap_tables_adds_scroll_container():
    html = wrap_tables("<table><tr><td>x</td></tr></table>")
    assert 'class="table-wrap"' in html
    assert html.count("<table>") == 1
    assert html.endswith("</table></div>")


def test_zero_percent_stays_uncolored():
    assert "+0.00%" in colorize_percentages("+0.00%")
    assert "<span" not in colorize_percentages("+0.00%")
    colored = colorize_percentages("+3.05%")
    assert "#7ee787" in colored


def test_markdown_to_html_wraps_tables():
    md = "| A | B |\n|---|---|\n| 1 | +1.20% |"
    html = markdown_to_html(md)
    assert 'class="table-wrap"' in html
    assert "#7ee787" in html
