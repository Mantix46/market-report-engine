from email_sender import markdown_to_html


def test_email_preserves_report_highlights_and_neutral_indicators():
    html = markdown_to_html(
        "## 1. Executive Summary\n\n"
        "- **CFTC** · **-15.04%** otwartych pozycji\n\n"
        "## 2. Benchmarki rynkowe\n\n"
        "NASDAQ +2.00% i [tekst](https://example.com/?change=-1%25)\n\n"
        "## 9. Sentyment\n\n"
        "VIX **+7.00%**\n\n"
        "## Prognozy do weryfikacji\n\n"
        '<strong style="color: #f85149;">spadek</strong> · ATR **1.50%**'
    )

    assert '<strong>-15.04%</strong>' in html
    assert '<strong>+7.00%</strong>' in html
    assert '<strong style="color: #f85149;">spadek</strong>' in html
    assert '<strong>1.50%</strong>' in html
    assert '<span style="color: #7ee787; font-weight: 600;">+2.00%</span>' in html
    assert 'href="https://example.com/?change=-1%25"' in html
