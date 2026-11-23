"""Official EDGAR document excerpts and reported insider transactions."""

from decimal import Decimal, InvalidOperation
import logging
import re
from urllib.parse import urlsplit

from lxml import etree

from earnings_report import _document_text, _filing_documents
from sec_edgar import ARCHIVES_URL, SecError, sec_budget, sec_get

logger = logging.getLogger(__name__)
DOCUMENT_LIMIT = 8
FORM4_LIMIT = 20
EXCERPT_LENGTH = 2400
ITEM_HEADING = re.compile(r"\bItem\s+\d+\.\d{2}\b", re.IGNORECASE)


def _form4_document(filing):
    filename = filing.primary_document.rsplit("/", 1)[-1]
    if not filename.lower().endswith(".xml") or urlsplit(filename).path != filename:
        return None
    archive = f"{ARCHIVES_URL}/{int(filing.cik)}/{filing.accession.replace('-', '')}"
    with sec_get(f"{archive}/{filename}") as response:
        parser = etree.XMLParser(resolve_entities=False, no_network=True)
        return etree.fromstring(response.content, parser=parser)


def _form4_transactions(document, filing):
    owners = ", ".join(document.xpath("./reportingOwner/reportingOwnerId/rptOwnerName/text()"))
    if not owners:
        return []
    trades = []
    for row in document.xpath("./nonDerivativeTable/nonDerivativeTransaction"):
        code = row.findtext("transactionCoding/transactionCode")
        direction = row.findtext("transactionAmounts/transactionAcquiredDisposedCode/value")
        if (code, direction) not in {("P", "A"), ("S", "D")}:
            continue
        date = row.findtext("transactionDate/value")
        try:
            shares = Decimal(row.findtext("transactionAmounts/transactionShares/value", ""))
        except InvalidOperation:
            continue
        if not date or not shares.is_finite() or shares <= 0:
            continue
        try:
            price = Decimal(row.findtext("transactionAmounts/transactionPricePerShare/value", ""))
            if not price.is_finite() or price <= 0:
                price = None
        except InvalidOperation:
            price = None
        plan = document.findtext("aff10b5One", "").strip().lower() in {"1", "true"}
        trades.append({"owner": owners, "side": "kupno" if code == "P" else "sprzedaż",
                       "date": date, "shares": shares, "price": price, "url": filing.url, "plan": plan,
                       "filed": filing.filing_date, "form": filing.form})
    return trades


def collect_sec_monitoring(recent_filings, weekly_filings):
    documents = {}
    insider_trades = {}
    forms4 = {filing.accession: filing for filing in [*weekly_filings, *recent_filings]
              if filing.form in {"4", "4/A"}}
    with sec_budget(25):
        for filing in sorted(forms4.values(), key=lambda row: row.filing_date, reverse=True)[:FORM4_LIMIT]:
            try:
                document = _form4_document(filing)
                if document is not None:
                    trades = insider_trades.setdefault(filing.ticker, [])
                    for trade in _form4_transactions(document, filing):
                        identity = ("owner", "side", "date", "shares", "price")
                        duplicate = any(
                            (trade["form"] == "4/A" or existing["form"] == "4/A")
                            and all(trade[key] == existing[key] for key in identity)
                            for existing in trades
                        )
                        if not duplicate:
                            trades.append(trade)
            except (SecError, etree.XMLSyntaxError, ValueError) as exc:
                logger.info("Form 4 %s: odczyt niedostępny (%s).", filing.accession, type(exc).__name__)

    events = [filing for filing in recent_filings if filing.form.split("/", 1)[0] not in {"3", "4", "5", "144"}]
    with sec_budget(25):
        for filing in sorted(events, key=lambda row: row.filing_date, reverse=True)[:DOCUMENT_LIMIT]:
            excerpts = []
            for url, document in _filing_documents(filing, limit=2, exhibits_first=filing.form in {"6-K", "6-K/A"}):
                text = _document_text(document)
                if filing.form in {"8-K", "8-K/A"} and url.endswith(filing.primary_document):
                    headings = list(ITEM_HEADING.finditer(text))
                    substantive = next((heading for heading in headings if not heading.group().endswith("9.01")), None)
                    if substantive:
                        text = text[substantive.start():]
                if text:
                    excerpts.append((url, text[:EXCERPT_LENGTH]))
            if excerpts:
                documents[filing.accession] = excerpts
            else:
                logger.info("SEC %s: brak dostępnej treści dokumentu.", filing.accession)
    logger.info("Monitoring SEC: %d dokumentów z dostępnymi wyciągami, %d transakcji P/S z Form 4.",
                len(documents), sum(map(len, insider_trades.values())))
    return documents, insider_trades
