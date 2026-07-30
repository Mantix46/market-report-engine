"""
email_sender.py — Moduł wysyłki raportu rynkowego na e-mail.

Konwertuje raport markdown → HTML z profesjonalnym stylem,
wysyła przez SMTP (Gmail/Outlook).
"""

import smtplib
import ssl
import markdown
from email.mime.text import MIMEText
from email.mime.multipart import MIMEMultipart
from datetime import datetime

from market_calendar import warsaw_now

import logging

logger = logging.getLogger(__name__)


# ============================================================
# Styl HTML (dark theme, profesjonalny wygląd)
# ============================================================

EMAIL_CSS = """
<style>
    body {
        font-family: 'Segoe UI', -apple-system, BlinkMacSystemFont, sans-serif;
        background-color: #0d1117;
        color: #c9d1d9;
        max-width: 800px;
        margin: 0 auto;
        padding: 24px;
        line-height: 1.6;
    }
    h1 {
        color: #58a6ff;
        border-bottom: 2px solid #21262d;
        padding-bottom: 12px;
        font-size: 24px;
    }
    h2 {
        color: #79c0ff;
        border-bottom: 1px solid #21262d;
        padding-bottom: 8px;
        margin-top: 32px;
        font-size: 20px;
    }
    h3 {
        color: #d2a8ff;
        margin-top: 24px;
        font-size: 16px;
    }
    h4 {
        color: #7ee787;
        font-size: 14px;
    }
    table {
        border-collapse: collapse;
        width: 100%;
        margin: 16px 0;
        font-size: 13px;
    }
    th {
        background-color: #161b22;
        color: #58a6ff;
        padding: 10px 12px;
        text-align: left;
        border: 1px solid #30363d;
        font-weight: 600;
    }
    td {
        padding: 8px 12px;
        border: 1px solid #30363d;
        background-color: #0d1117;
    }
    tr:nth-child(even) td {
        background-color: #161b22;
    }
    blockquote {
        border-left: 4px solid #f0883e;
        background-color: #161b22;
        padding: 12px 16px;
        margin: 16px 0;
        border-radius: 4px;
        color: #d29922;
    }
    strong {
        color: #f0f6fc;
    }
    em {
        color: #8b949e;
    }
    code {
        background-color: #161b22;
        padding: 2px 6px;
        border-radius: 4px;
        font-size: 13px;
        color: #79c0ff;
    }
    hr {
        border: none;
        border-top: 1px solid #21262d;
        margin: 24px 0;
    }
    ul, ol {
        padding-left: 24px;
    }
    li {
        margin-bottom: 4px;
    }
    a {
        color: #58a6ff;
        text-decoration: none;
    }
    a:hover {
        text-decoration: underline;
    }
    .footer {
        margin-top: 32px;
        padding-top: 16px;
        border-top: 1px solid #21262d;
        color: #484f58;
        font-size: 12px;
    }
</style>
"""


import re

def colorize_percentages(html: str) -> str:
    """Koloruje wartości procentowe: zielone dla +, czerwone dla -.
    Wartości ±0.00% zostają bez koloru (ruch neutralny)."""
    def replace_pct(match):
        value = match.group(0)
        try:
            if float(value.rstrip("%")) == 0.0:
                return value  # +0.00% / -0.00% -> neutralne, bez koloru
        except ValueError:
            return value
        if value.startswith("+"):
            return f'<span style="color: #7ee787; font-weight: 600;">{value}</span>'
        return f'<span style="color: #f85149; font-weight: 600;">{value}</span>'

    return re.sub(r'[+-]\d+(?:\.\d+)?%', replace_pct, html)


def markdown_to_html(md_content: str) -> str:
    """Konwertuje markdown raportu na stylizowany HTML email."""
    
    # Konwersja markdown → HTML
    html_body = markdown.markdown(
        md_content,
        extensions=["tables", "fenced_code", "nl2br"],
    )
    
    # Kolorowanie zmian procentowych
    html_body = colorize_percentages(html_body)
    
    # Pełny HTML document
    html = f"""<!DOCTYPE html>
<html lang="pl">
<head>
    <meta charset="UTF-8">
    <meta name="viewport" content="width=device-width, initial-scale=1.0">
    {EMAIL_CSS}
</head>
<body>
    {html_body}
    <div class="footer">
        <p>📧 Wygenerowano automatycznie przez Market Report Engine<br>
        Raport ma charakter informacyjny i nie stanowi rekomendacji inwestycyjnej.</p>
    </div>
</body>
</html>"""
    
    return html


def send_report_email(
    smtp_server: str,
    smtp_port: int,
    smtp_user: str,
    smtp_password: str,
    recipient: str,
    md_report: str,
    subject: str = None,
) -> bool:
    """
    Wysyła raport rynkowy na wskazany adres e-mail.
    
    Args:
        smtp_server: Adres serwera SMTP
        smtp_port: Port SMTP (587 dla TLS)
        smtp_user: Login SMTP (e-mail nadawcy)
        smtp_password: Hasło lub App Password
        recipient: Adres e-mail odbiorcy
        md_report: Treść raportu w markdown
    
    Returns:
        True jeśli wysłano pomyślnie, False w przeciwnym razie.
    """
    
    now = warsaw_now()
    date_str = now.strftime("%d.%m.%Y")
    if subject is None:
        subject = f"📊 Raport Rynkowy — {date_str}"
    
    # Budujemy e-mail multipart (text + html)
    msg = MIMEMultipart("alternative")
    msg["Subject"] = subject
    msg["From"] = f"Market Report <{smtp_user}>"
    msg["To"] = recipient
    
    # Wersja plain text (fallback)
    text_part = MIMEText(md_report, "plain", "utf-8")
    msg.attach(text_part)
    
    # Wersja HTML (główna)
    html_content = markdown_to_html(md_report)
    html_part = MIMEText(html_content, "html", "utf-8")
    msg.attach(html_part)
    
    try:
        context = ssl.create_default_context()
        with smtplib.SMTP(smtp_server, smtp_port) as server:
            server.ehlo()
            server.starttls(context=context)
            server.ehlo()
            server.login(smtp_user, smtp_password)
            server.sendmail(smtp_user, recipient, msg.as_string())
        
        logger.info(f"Raport wysłany na {recipient} ({date_str})")
        return True
        
    except smtplib.SMTPAuthenticationError:
        logger.error("Błąd autoryzacji SMTP. Sprawdź login/hasło/App Password.")
        print("        Dla Gmail: https://myaccount.google.com/apppasswords")
        return False
    except smtplib.SMTPException as e:
        logger.error(f"Błąd SMTP: {e}")
        return False
    except Exception as e:
        logger.error(f"Nie udało się wysłać maila: {e}")
        return False


if __name__ == "__main__":
    # Test: generuje HTML i zapisuje lokalnie
    test_md = "# Test Report\n\nTo jest **test** raportu.\n\n| Col1 | Col2 |\n|------|------|\n| A | B |"
    html = markdown_to_html(test_md)
    with open("test_email.html", "w", encoding="utf-8") as f:
        f.write(html)
    print("Zapisano test_email.html — otwórz w przeglądarce aby sprawdzić styl.")
