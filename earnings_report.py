"""Earnings facts extracted from SEC filings and explicitly comparable estimates."""
from __future__ import annotations

from dataclasses import dataclass, field
from datetime import date, datetime, timezone
from decimal import Decimal, InvalidOperation
import logging
import os
import re
from urllib.parse import urljoin, urlsplit

from lxml import etree, html
import requests
import yfinance as yf

from sec_edgar import SecError, sec_budget, sec_get

logger = logging.getLogger(__name__)

FMP_URL = "https://financialmodelingprep.com/stable/earnings"
FMP_SOURCE = "https://site.financialmodelingprep.com/developer/docs/stable/earnings-company"
SEC_FACTS_URL = "https://data.sec.gov/api/xbrl/companyfacts/CIK{cik:010d}.json"

METRIC_LABELS = {
    "revenue": "Przychody",
    "eps": "Rozwodniony EPS",
    "operating_income": "Wynik operacyjny",
    "net_income": "Wynik netto",
    "operating_cash_flow": "Przepływy operacyjne",
    "free_cash_flow": "Wolne przepływy pieniężne",
    "operating_margin": "Marża operacyjna",
    "ebitda_margin": "Marża EBITDA",
    "ebitda": "EBITDA",
    "capex": "Nakłady inwestycyjne",
}

BASES = {"reported": "", "GAAP": "GAAP", "IFRS": "MSSF", "adjusted": "skorygowany"}
SCOPE_LABELS = {"quarter": "kwartał", "annual": "rok obrotowy", "ytd": "od początku roku"}
XBRL = {
    "us-gaap": {
        "revenue": ("RevenueFromContractWithCustomerExcludingAssessedTax", "Revenues",
                    "SalesRevenueNet", "RevenueFromContractWithCustomerIncludingAssessedTax"),
        "eps": ("EarningsPerShareDiluted", "EarningsPerShareBasicAndDiluted"),
        "operating_income": ("OperatingIncomeLoss",),
        "net_income": ("NetIncomeLoss", "ProfitLoss"),
        "operating_cash_flow": ("NetCashProvidedByUsedInOperatingActivities",),
    },
    "ifrs-full": {
        "revenue": ("Revenue",),
        "eps": ("DilutedEarningsLossPerShare",),
        "operating_income": ("ProfitLossFromOperatingActivities",),
        "net_income": ("ProfitLossAttributableToOwnersOfParent", "ProfitLoss"),
        "operating_cash_flow": ("CashFlowsFromUsedInOperatingActivities",),
    },
}
CURRENCY = re.compile(r"[A-Z]{3}")
MONTH = r"(?:January|February|March|April|May|June|July|August|September|October|November|December)"
FULL_DATE = re.compile(rf"\b({MONTH})\s+(\d{{1,2}})\s*,?\s*(20\d{{2}})\b", re.I)
MONTH_DAY = re.compile(rf"\b({MONTH})\s+(\d{{1,2}})\b", re.I)
QUARTER = re.compile(r"\b(Q[1-4]|(?:first|second|third|fourth)(?:\s+fiscal)?\s+quarter)\b", re.I)
RESULTS = re.compile(
    r"(?:financial results|results of operations|(?:unaudited |interim )"
    r"(?:condensed )?(?:consolidated )?financial statements|"
    r"consolidated statements of (?:operations|income)|earnings release)",
    re.I,
)
REPORT_PERIOD = re.compile(
    r"\b(?:quarter(?: of fiscal year 20\d{2},?)?,?|three and (?:six|nine) months|"
    r"three months|(?:fiscal )?year|twelve months|six months|nine months)"
    r"\s+end(?:ed|ing)\s+", re.I,
)
SCALES = {"thousand": Decimal(1_000), "million": Decimal(1_000_000),
          "billion": Decimal(1_000_000_000)}


@dataclass(frozen=True)
class Period:
    scope: str
    end: date


@dataclass(frozen=True)
class Measure:
    metric: str
    basis: str
    currency: str
    value: Decimal
    source: str
    previous: Decimal | None = None
    previous_end: date | None = None
    precision: Decimal = Decimal(1)


@dataclass(frozen=True)
class Guidance:
    metric: str
    basis: str
    currency: str
    scope: str
    fiscal_label: str
    end: date | None
    low: Decimal
    high: Decimal
    source: str
    qualifier: str = ""


@dataclass(frozen=True)
class Comparison:
    estimate: Decimal
    source: str
    as_of: date | None = None


@dataclass
class Earnings:
    ticker: str
    period: Period | None
    filings: list = field(default_factory=list)
    measures: dict[tuple[str, str], Measure] = field(default_factory=dict)
    guidance: list[Guidance] = field(default_factory=list)
    # Only a financial results release can establish an announcement date.
    release_date: date | None = None
    result_consensus: dict[tuple[str, str], Comparison] = field(default_factory=dict)
    guidance_consensus: dict[Guidance, Comparison] = field(default_factory=dict)
    conflicts: set[tuple[str, str]] = field(default_factory=set)


def _date(value):
    if isinstance(value, datetime):
        return value.date()
    if isinstance(value, date):
        return value
    try:
        return date.fromisoformat(value)
    except (TypeError, ValueError):
        return None


def _amount(value):
    if value is None or isinstance(value, bool):
        return None
    try:
        amount = Decimal(str(value))
        return amount if amount.is_finite() else None
    except InvalidOperation:
        return None


def _company_facts(cik):
    try:
        facts = sec_get(SEC_FACTS_URL.format(cik=int(cik))).json()["facts"]
        return facts if isinstance(facts, dict) else {}
    except (SecError, requests.RequestException, KeyError, ValueError, TypeError):
        logger.debug("SEC Company Facts niedostępne")
        return {}


def _duration_scope(start, end):
    duration = (end - start).days + 1
    if 75 <= duration <= 105:
        return "quarter"
    if 340 <= duration <= 385:
        return "annual"
    if 160 <= duration <= 195 or 250 <= duration <= 290:
        return "ytd"
    return None


def _xbrl_observations(tag, filing, metric):
    units = tag.get("units") if isinstance(tag, dict) else None
    if not isinstance(units, dict):
        return
    for unit, observations in units.items():
        currency, _, denominator = unit.partition("/")
        if not CURRENCY.fullmatch(currency) or denominator != ("shares" if metric == "eps" else ""):
            continue
        if not isinstance(observations, list):
            continue
        for row in observations:
            if not isinstance(row, dict) or row.get("accn") != filing.accession:
                continue
            start, end = _date(row.get("start")), _date(row.get("end"))
            val = _amount(row.get("val"))
            if start and end and val is not None:
                yield start, end, currency, val


def _tag_measure(tag, filing, metric, basis, period):
    rows = list(_xbrl_observations(tag, filing, metric))
    current = {
        (start, currency, value) for start, end, currency, value in rows
        if end == period.end and _duration_scope(start, end) == period.scope
    }
    if len(current) != 1:
        return None, bool(current)
    start, currency, value = current.pop()
    prior = {
        (end, amount) for prev_start, end, ccy, amount in rows
        if ccy == currency and 350 <= (period.end - end).days <= 380
        and abs((period.end - start).days - (end - prev_start).days) <= 8
    }
    previous_end, previous = prior.pop() if len(prior) == 1 else (None, None)
    precision = Decimal("0.01") if metric == "eps" else Decimal(1)
    return Measure(metric, basis, currency, value, filing.url, previous, previous_end, precision), False


def _xbrl_measures(facts, filing, period):
    measures = []
    for namespace, metrics in XBRL.items():
        basis = "IFRS" if namespace == "ifrs-full" else "GAAP"
        tags = facts.get(namespace)
        if not isinstance(tags, dict):
            continue
        for metric, names in metrics.items():
            for name in names:
                measure, conflict = _tag_measure(tags.get(name, {}), filing, metric, basis, period)
                if measure:
                    measures.append(measure)
                if measure or conflict:
                    break
    # A single accounting standard and presentation currency must anchor a period.
    anchors = {(m.basis, m.currency) for m in measures if m.metric in ("revenue", "eps")}
    if len(anchors) != 1:
        return []
    return [m for m in measures if (m.basis, m.currency) in anchors]


def _text(element):
    return " ".join(element.text_content().replace("\u200b", "").split())


def _read_html(url):
    try:
        return html.fromstring(sec_get(url).content)
    except (SecError, requests.RequestException, etree.ParserError, ValueError) as exc:
        logger.debug("Dokument SEC niedostępny (%s)", type(exc).__name__)
        return None


def _filing_documents(filing):
    """Return the primary filing plus SEC-linked exhibit 99 HTML documents."""
    archive = (f"https://www.sec.gov/Archives/edgar/data/{int(filing.cik)}/"
               f"{filing.accession.replace('-', '')}/")
    index = _read_html(f"{archive}{filing.accession}-index.htm")
    docs = []
    if filing.primary_document:
        url = urljoin(archive, filing.primary_document)
        if url.startswith(archive):
            docs.append(url)
    if index is not None:
        for row in index.xpath("//table//tr"):
            cells = row.xpath("./td")
            if len(cells) < 4 or not re.fullmatch(r"EX-99(?:\.\d+)?", _text(cells[3]), re.I):
                continue
            for href in cells[2].xpath(".//a/@href"):
                url = urljoin(archive, href)
                if url.startswith(archive) and urlsplit(url).path.lower().endswith((".htm", ".html")):
                    docs.append(url)
    for url in list(dict.fromkeys(docs)):
        document = _read_html(url)
        if document is not None:
            yield url, document


def _is_annual_20f(filing):
    archive = f"https://www.sec.gov/Archives/edgar/data/{int(filing.cik)}/{filing.accession.replace('-', '')}/"
    url = urljoin(archive, filing.primary_document or "")
    if url == archive or not url.startswith(archive):
        return False
    document = _read_html(url)
    if document is None:
        return False
    cover = _document_text(document)[:6000]
    # Form 20-F can list unchecked annual and registration cover options.
    # Requiring the fiscal year line distinguishes the annual election.
    return bool(re.search(
        r"annual report pursuant to section 13 or 15\(d\).{0,250}for the fiscal year ended",
        cover, re.I,
    ))


def _parsed_date(month, day, year):
    try:
        return datetime.strptime(f"{month} {day} {year}", "%B %d %Y").date()
    except ValueError:
        return None


def _first_date(text):
    match = FULL_DATE.search(text)
    return _parsed_date(*match.groups()) if match else None


def _period_near_release(period, filing_date):
    return period and filing_date and 0 <= (filing_date - period).days <= 185


def _described_periods(text, filing_date):
    periods = set()
    for match in REPORT_PERIOD.finditer(text):
        ending = FULL_DATE.match(text, match.end())
        if ending:
            end = _parsed_date(*ending.groups())
            if _period_near_release(end, filing_date):
                label = match.group().lower()
                scope = ("quarter" if label.startswith(("quarter", "three months")) else
                         "ytd" if label.startswith(("six months", "nine months")) else "annual")
                if label.startswith("three and"):
                    periods.add(Period("ytd", end))
                    scope = "quarter"
                periods.add(Period(scope, end))
    for match in re.finditer(
        r"\bfourth quarter and full year(?: of fiscal)? 20\d{2},? (?:which )?ended\s+", text, re.I
    ):
        end = _first_date(text[match.end():match.end() + 35])
        if _period_near_release(end, filing_date):
            periods.update((Period("quarter", end), Period("annual", end)))
    return periods


def _table_rows(table):
    """Use logical columns so accounting symbols and spacer cells stay in their periods."""
    spans = {}
    for row in table.xpath(".//tr"):
        cells, col = [], 0
        for start in list(spans):
            if spans[start][1] == 0:
                del spans[start]
        for element in row.xpath("./td|./th"):
            while col in spans:
                col += spans[col][0]
            span = element.get("colspan", "1")
            span = min(int(span), 100) if span.isdigit() else 1
            if span < 1:
                cells = []
                break
            down = element.get("rowspan", "1")
            if down.isdigit() and int(down) > 1:
                spans[col] = (span, min(int(down), 100))
            value = _text(element)
            if value:
                cells.append((col, col + span, value))
            col += span
        yield cells
        spans = {start: (width, count - 1) for start, (width, count) in spans.items()}


def _header_scope(label):
    if re.search(r"\b(?:six|nine) months ended\b|\b(?:two|three) (?:fiscal )?quarters ended\b",
                 label, re.I):
        return "ytd"
    if re.search(r"\b(?:three months|(?:fiscal )?quarter) ended\s*|\b[1-4](?:st|nd|rd|th) qtr\.", label, re.I):
        return "quarter"
    if re.search(r"\b(?:(?:fiscal )?years?|twelve months) ended\b", label, re.I):
        return "annual"
    return None


def _quarter_number(text):
    match = QUARTER.search(text)
    if not match:
        return None
    word = match[1].lower().split()[0]
    return int(word[1]) if word.startswith("q") else ("first", "second", "third", "fourth").index(word) + 1


def _table_columns(header, known_periods, filing_date, context):
    groups = [(left, right, re.sub(r"\s*\(\d+\)$", "", label)) for row in header for left, right, label in row]
    columns = []
    for row in header:
        for left, right, label in row:
            short_period = re.fullmatch(r"(?:F?Q([1-4])|FY)[\s-]+(?:20)?(\d{2})", label, re.I)
            if not short_period and not re.fullmatch(r"20\d{2}", label) and not FULL_DATE.fullmatch(label):
                continue
            group = [name for first, last, name in groups if first <= left < last]
            scope_group = next((name for name in group if _header_scope(name)), "")
            scope = _header_scope(scope_group)
            end = _first_date(label)
            if end is None and re.fullmatch(r"20\d{2}", label):
                day = MONTH_DAY.search(scope_group)
                if day is None:
                    days = {name for name in group if MONTH_DAY.fullmatch(name.strip(" ,"))}
                    day = MONTH_DAY.search(days.pop()) if len(days) == 1 else None
                end = _parsed_date(*day.groups(), label) if day else None
            if end is None and short_period:
                scope = "quarter" if short_period[1] else "annual"
                fiscal_year = 2000 + int(short_period[2])
                current = {p.end for p in known_periods if p.scope == scope}
                period_context = next((name for _, _, name in groups if "financial results" in name.lower()), "")
                context_quarter = _quarter_number(context)
                if "quarterly financial results" in period_context.lower() and any(
                        p.scope == "annual" and p.end in current for p in known_periods):
                    context_quarter = 4
                if len(current) == 1 and (scope == "annual" or int(short_period[1]) == context_quarter):
                    known = current.pop()
                    context_year = re.search(r"\b(?:fiscal year|FY)\s*20(\d{2})\b", context, re.I)
                    current_fy = 2000 + int(context_year[1]) if context_year else known.year
                    if fiscal_year == current_fy:
                        end = known
            if not scope or not end or end > filing_date:
                continue
            basis = ("adjusted" if any(name.upper() in ("NON-GAAP", "ADJUSTED") for name in group)
                     else None)
            columns.append((left, right, Period(scope, end), basis))
    return columns


def _cell_amount(label):
    label = label.strip().replace(",", "").replace("−", "-").replace("$", "").strip()
    if re.fullmatch(r"\(?[+\-]?\d+(?:\.\d+)?\)?", label):
        return _amount(label.replace("(", "-").replace(")", ""))
    return None


def _table_value(row, left, right):
    values = [amount for col, _, label in row if left <= col < right
              if (amount := _cell_amount(label)) is not None]
    return values[0] if len(values) == 1 else None


def _row_metric(label, parent_label):
    label = re.sub(r"\s*\([0-9]+\)\s*$", "", label.lower().strip().rstrip(":"))
    if re.fullmatch(r"(?:(?:total|net|consolidated) ){0,2}revenues?|net sales|total net sales", label):
        return "revenue", None
    if label == "diluted" and re.fullmatch(
            r"(?:net (?:income|loss)(?:\s*/\s*\((?:loss|income)\))?|(?:net )?earnings)"
            r" per (?:class [ab](?: and class [ab])? |common |ordinary )?shares?:?", parent_label):
        return "eps", None
    if (re.search(r"\bper (?:common |ordinary )?shares?\b|\beps\b", label)
            and ("diluted" in label or label == "adjusted eps")
            and not re.search(r"continuing|discontinued|segment", label)):
        basis = "adjusted" if re.search(r"adjusted|non-gaap", label) else None
        return "eps", basis
    if re.fullmatch(r"operating (?:income|loss|profit)(?:\s*\(loss\))?|income(?:\s*/\s*\(loss\))? from operations", label):
        return "operating_income", None
    if re.fullmatch(r"net (?:income|loss)(?:\s*/?\s*\(loss\))?", label):
        return "net_income", None
    if re.fullmatch(r"cash (?:flow )?from operations|(?:net )?cash (?:flow )?(?:provided by|from) operating activities", label):
        return "operating_cash_flow", None
    if label == "free cash flow":
        return "free_cash_flow", "adjusted"
    if label == "adjusted ebitda":
        return "ebitda", "adjusted"
    return None, None


def _presentation_units(context, rows, domestic):
    units = next((SCALES[m.group(1).lower()] for line in context
                  if (m := re.search(r"\b(thousand|million|billion)s\b", line, re.I))), None)
    joined = " ".join(context)
    explicit = re.findall(r"\b(?:USD|CAD|AUD|NZD|EUR|GBP|CHF|JPY|CNY|HKD|SEK|NOK|ILS)\b", joined)
    if re.search(r"\b(?:U\.?S\.? dollars|US\s*\$)", joined, re.I):
        explicit.append("USD")
    if "€" in joined:
        explicit.append("EUR")
    if "£" in joined:
        explicit.append("GBP")
    if len(set(explicit)) > 1:
        return None, None
    currency = next(iter(explicit), None)
    if currency is None and domestic and ("dollar" in joined.lower()
            or any(label.startswith("$") for row in rows for _, _, label in row)):
        currency = "USD"
    return units, currency


def _exhibit_measures(document, url, filing, described):
    filing_date = _date(filing.filing_date)
    if filing_date is None:
        return {}
    domestic = filing.form == "8-K"
    # Financial statements generally use GAAP; explicit IFRS takes precedence.
    prefix = _text(document)[:100000]
    default_basis = ("IFRS" if re.search(r"International Financial Reporting Standards|\bIFRS\b", prefix)
                     else "GAAP" if domestic or re.search(r"U\.?S\.? GAAP|\bGAAP\b", prefix)
                     else None)
    results = {}
    for table in document.xpath("//table[not(ancestor::table)]"):
        rows = list(_table_rows(table))
        first_metric = next((i for i, row in enumerate(rows) if row and _row_metric(row[0][2], "")[0]), None)
        if first_metric is None or not 0 < first_metric <= 14:
            continue
        prev = table.xpath("preceding::*[self::p or self::div or self::font][not(ancestor::table)]")[-8:]
        table_context = [_text(element) for element in prev]
        if any("business unit financial results" in label.lower() for row in rows[:first_metric]
               for _, _, label in row):
            continue
        context = [label for row in rows[:first_metric] for _, _, label in row] + list(reversed(table_context))
        columns = _table_columns(rows[:first_metric], described, filing_date, " ".join(table_context))
        if not columns:
            continue
        scale, currency = _presentation_units(context, rows, domestic)
        if not currency:
            continue
        parent = ""
        for row in rows[first_metric:]:
            if not row or row[0][0] != 0:
                continue
            label = row[0][2]
            metric, row_basis = _row_metric(label, parent)
            if not metric:
                if len(row) == 1:
                    parent = label.lower()
                continue
            if metric != "eps" and scale is None:
                continue
            factor = Decimal(1) if metric == "eps" else scale
            for left, right, period, column_basis in columns:
                if not _period_near_release(period.end, filing_date):
                    continue
                value = _table_value(row, left, right)
                if value is None:
                    continue
                basis = row_basis or column_basis or default_basis
                if metric == "revenue":
                    if column_basis == "adjusted":
                        continue
                    basis = default_basis
                if basis is None:
                    continue
                prior = [
                    (p.end, prev_value * factor)
                    for first, last, p, p_basis in columns
                    if p.scope == period.scope and p_basis == column_basis
                    and 350 <= (period.end - p.end).days <= 380
                    if (prev_value := _table_value(row, first, last)) is not None
                ]
                previous_end, previous = prior[0] if len(prior) == 1 else (None, None)
                precision = Decimal(1).scaleb(value.as_tuple().exponent) * factor
                results.setdefault(period, []).append(
                    Measure(metric, basis, currency, value * factor, url, previous, previous_end, precision)
                )
    return results

GUIDANCE_HEADER = re.compile(
    r"\b(?:(?:first|second|third|fourth)(?: fiscal)? quarter(?: (?:of )?fiscal year)? "
    r"(?:20\d{2} )?(?:business )?(?:guidance|outlook)|"
    r"(?:(?:full|fiscal) year |FY\s*)?20\d{2}\s*(?::\s*initial)?\s+(?:business )?(?:guidance|outlook))\b",
    re.I,
)
GUIDANCE_METRIC = re.compile(
    r"(?:(?:fourth|first|second|third) quarter\s+)?"
    r"(?P<metric>(?:consolidated )?revenue|(?:non-GAAP |adjusted )?diluted EPS|"
    r"adjusted EBITDA margin|non-GAAP operating income|capital expenditures)"
    r"\s*(?::|guidance of\b)\s*",
    re.I,
)
GUIDANCE_VALUE = re.compile(
    r"(?:(?:we (?:now )?expect(?: (?:consolidated )?revenue)?|unchanged, at)\s+)?"
    r"(?P<qualifier>at the low end of our |at the high end of our |approximately |about )?"
    r"(?P<ccy>US\$|USD\s*|\$|€|£)?(?P<low>\d+(?:\.\d+)?)"
    r"(?:\s*(?P<first_scale>thousand|million|billion))?"
    r"(?:\s*(?:-|–|—|to)\s*(?:US\$|USD\s*|\$|€|£)?(?P<high>\d+(?:\.\d+)?))?"
    r"\s*(?P<unit>thousand|million|billion|percent|%)?",
    re.I,
)


def _document_text(document):
    # Preserve boundaries around headings and table cells without inserting spaces
    # between inline fragments within a number.
    raw = " ".join(
        text.strip() for text in document.xpath("//body//text()[not(ancestor::style or ancestor::script)]")
        if text.strip()
    ).replace("\u200b", "")
    return " ".join(raw.split())


def _guidance_metric(label, unit):
    label = label.lower()
    if label.endswith("revenue"):
        return "revenue", "reported"
    if label.endswith("eps"):
        return "eps", "adjusted" if label.startswith(("non-gaap", "adjusted")) else "reported"
    if label == "adjusted ebitda margin":
        return "ebitda_margin", "adjusted"
    if label == "non-gaap operating income":
        return ("operating_margin" if unit in ("percent", "%") else "operating_income"), "adjusted"
    return "capex", "reported"


def _exhibit_guidance(text, url, filing, result_periods, currency, calendar_year):
    headers = list(GUIDANCE_HEADER.finditer(text))
    guidance = []
    for i, header in enumerate(headers):
        label = header.group()
        quarter = _quarter_number(label)
        fiscal_year = re.search(r"20\d{2}", label)
        year = int(fiscal_year[0]) if fiscal_year else None
        scope = "quarter" if quarter else "annual"
        block = text[header.end(): headers[i + 1].start() if i + 1 < len(headers) else None][:850]
        start_boundary = re.search(r"\b(?:business outlook|guidance)\b", label, re.I)
        # A period heading should introduce a paragraph or list, rather than occur
        # in a headline describing growth or as a clause inside a sentence.
        if scope == "quarter" and not start_boundary:
            continue
        end_match = re.search(rf"\bend(?:ing|s)\s+({MONTH}\s+\d{{1,2}},?\s*20\d{{2}})", block[:250], re.I)
        end = _first_date(end_match[1]) if end_match else None
        if end is None and scope == "annual" and year and calendar_year:
            end = date(year, 12, 31)
        if end and result_periods and end <= max(period.end for period in result_periods):
            continue
        for match in GUIDANCE_METRIC.finditer(block):
            value = GUIDANCE_VALUE.match(block, match.end())
            if not value:
                continue
            unit = (value["unit"] or value["first_scale"] or "").lower()
            metric, basis = _guidance_metric(match["metric"], unit)
            ccy = {"€": "EUR", "£": "GBP", "US$": "USD", "USD": "USD"}.get(
                (value["ccy"] or "").strip().upper(), currency)
            percent = metric in ("operating_margin", "ebitda_margin")
            if percent != (unit in ("percent", "%")):
                continue
            if not percent and (not ccy or metric != "eps" and unit not in SCALES):
                continue
            factor = SCALES.get(unit, Decimal(1))
            low = Decimal(value["low"]) * SCALES.get((value["first_scale"] or "").lower(), factor)
            high = Decimal(value["high"] or value["low"]) * factor
            if low > high:
                continue
            qualifier = {"at the low end of our": "dolna granica przedziału",
                         "at the high end of our": "górna granica przedziału",
                         "approximately": "około", "about": "około"}.get(
                             (value["qualifier"] or "").strip().lower(), "")
            fiscal_label = f"Q{quarter}" if quarter else "FY"
            if year:
                fiscal_label += f" {year}"
            elif end:
                fiscal_label += f" kończący się {end.isoformat()}"
            if not year and not end:
                continue
            guidance.append(Guidance(metric, basis, "%" if percent else ccy, scope,
                                     fiscal_label, end, low, high, url, qualifier))
    return guidance


def _tabular_guidance(document, url, currency):
    if not currency:
        return []
    guidance = []
    for table in document.xpath("//table[not(ancestor::table)]"):
        rows = list(_table_rows(table))
        updated = next(((left, right, m) for row in rows[:8] for left, right, label in row
                        if (m := re.fullmatch(r"Fiscal (20\d{2}) Guidance \(Updated\)", label, re.I))), None)
        if updated:
            guidance.extend(_updated_annual_guidance(document, table, url, currency, rows, updated))
            continue
        header = next((row for row in rows[:8] if row and "outlook" in " ".join(
            label.lower() for _, _, label in row)), [])
        columns = [(left, right, "adjusted" if label.lower().startswith("non-gaap") else "GAAP")
                   for left, right, label in header
                   if re.fullmatch(r"(?:Non-)?GAAP(?:\s*\(\d+\))?\s+Outlook", label, re.I)]
        quarter = next((m for _, _, label in header if (m := re.fullmatch(
            r"F?Q([1-4])[\s-]+(?:20)?(\d{2})", label, re.I))), None)
        if not columns or not quarter:
            continue
        for row in rows:
            if not row or row[0][0] != 0:
                continue
            metric, _ = _row_metric(row[0][2], "")
            if metric not in ("revenue", "eps"):
                continue
            for left, right, basis in columns:
                value = next((label for col, _, label in row if left <= col < right), "")
                parts = re.split(r"\s*±\s*", value, maxsplit=1)
                amounts = [GUIDANCE_VALUE.fullmatch(part.strip()) for part in parts]
                if not all(amounts):
                    continue
                if any(item["high"] or item["unit"] in ("%", "percent") or
                       item["ccy"] not in ("$", "US$", "USD") for item in amounts):
                    continue
                units = [SCALES.get(item["unit"] or item["first_scale"] or "") for item in amounts]
                if (metric == "revenue" and not all(units)) or (metric == "eps" and any(units)):
                    continue
                values = [Decimal(item["low"]) * (unit or 1) for item, unit in zip(amounts, units)]
                center, margin = values if len(values) == 2 else (values[0], Decimal(0))
                if metric == "revenue":
                    basis = "reported"
                guidance.append(Guidance(
                    metric, basis, currency, "quarter", f"Q{quarter[1]} 20{quarter[2]}", None,
                    center - margin, center + margin, url, "około" if amounts[0]["qualifier"] else "",
                ))
    return list(dict.fromkeys(guidance))


def _updated_annual_guidance(document, table, url, currency, rows, updated):
    left, right, label = updated
    year = int(label[1])
    context = [_text(el) for el in table.xpath(
        "preceding::*[self::p or self::div or self::font][not(ancestor::table)]"
    )[-8:]][::-1]
    scale, _ = _presentation_units(context, rows, False)
    text = _document_text(document)
    prior_end = re.search(rf"\b(?:fiscal )?year ended ((?:{MONTH}) \d{{1,2}},? {year - 1})\b", text, re.I)
    end = _first_date(prior_end[1]) if prior_end else None
    # Calendar month ends can be carried forward across fiscal years.
    if end and (end.month, end.day) in ((3, 31), (6, 30), (9, 30), (12, 31)):
        end = end.replace(year=year)
    else:
        end = None
    guidance = []
    for row in rows:
        if not row or row[0][0] != 0:
            continue
        metric, basis = _row_metric(row[0][2], "")
        if metric not in ("revenue", "eps", "ebitda") or (metric != "eps" and scale is None):
            continue
        value = next((item for col, _, item in row if left <= col < right), "")
        match = re.fullmatch(r"\$([\d,.]+)\s*(?:-|–|—|to)\s*\$([\d,.]+)", value, re.I)
        if not match:
            continue
        low, high = (_cell_amount(item) for item in match.groups())
        if low is None or high is None or low > high:
            continue
        factor = 1 if metric == "eps" else scale
        guidance.append(Guidance(
            metric, basis or "reported", currency, "annual", f"FY {year}", end,
            low * factor, high * factor, url,
        ))
    return guidance


def _is_results_document(text, filing_date):
    before_disclaimer = re.split(r"forward[- ]looking statements", text[:5000], maxsplit=1, flags=re.I)[0]
    if not RESULTS.search(before_disclaimer):
        return False
    return (bool(_described_periods(before_disclaimer, filing_date))
            or bool(re.search(r"\b(?:unaudited |interim )(?:condensed )?(?:consolidated )?financial statements\b"
                              r"|\b(?:reports?|announces?)\b.{0,120}\b(?:financial|quarterly|annual) results\b",
                              before_disclaimer, re.I)))


def _calendar_fiscal_year(text, filing_date):
    if filing_date is None:
        return False
    prior_year = filing_date.year - 1
    return bool(re.search(
        rf"(?:annual report|form 20-f|year ended).{{0,120}}year ended december 31,? {prior_year}\b",
        text, re.I
    ))


def _compatible_amount(left, right):
    return (left.currency == right.currency and
            abs(left.value - right.value) <= max(left.precision, right.precision) / 2)


def _merge_measure(earnings, measure):
    key = (measure.metric, measure.basis)
    if key in earnings.conflicts:
        return
    prior = earnings.measures.get(key)
    if prior is not None and not _compatible_amount(prior, measure):
        earnings.measures.pop(key)
        earnings.conflicts.add(key)
    elif prior is None or measure.precision < prior.precision or (
            measure.precision == prior.precision and prior.previous is None
            and measure.previous is not None and measure.value == prior.value):
        earnings.measures[key] = measure


def _exhibit_results(filing, facts):
    filing_date = _date(filing.filing_date)
    if filing_date is None:
        return []
    documents = [(url, doc, _document_text(doc)) for url, doc in _filing_documents(filing)]
    results_docs = [(url, doc, text) for url, doc, text in documents
                    if filing.form == "8-K" or _is_results_document(text, filing_date)]
    if filing.form == "6-K" and not results_docs:
        return []

    described = set()
    for _, _, text in results_docs:
        described.update(_described_periods(text[:10000], filing_date))
    by_period = {}
    for url, doc, _ in results_docs:
        for period, measures in _exhibit_measures(doc, url, filing, described).items():
            by_period.setdefault(period, []).extend(measures)
    # Prefer the latest end in the release. A table may also contain the prior
    # quarter, which is useful context but is not a second publication.
    ends = {period.end for period in by_period}
    if not ends:
        ends = {period.end for period in described}
    if not ends:
        return [Earnings(filing.ticker, None, [filing])]
    latest_end = max(ends)
    periods = {period for period in set(by_period) | described if period.end == latest_end}
    # Show cumulative figures only when the release lacks a standalone quarter
    # or annual result.
    scopes = {period.scope for period in periods}
    if scopes & {"annual", "quarter"}:
        periods = {period for period in periods if period.scope != "ytd"}
    currency_candidates = {
        m.currency for period in periods for m in by_period.get(period, []) if m.metric == "revenue"
    }
    currency = next(iter(currency_candidates)) if len(currency_candidates) == 1 else None
    calendar_year = any(_calendar_fiscal_year(text, filing_date) for _, _, text in documents)
    guidance = [
        item for url, _, text in documents
        for item in _exhibit_guidance(text, url, filing, periods, currency, calendar_year)
    ]
    guidance.extend(item for url, document, _ in documents for item in _tabular_guidance(document, url, currency))
    # A formal 6-K financial statement filed later cannot move the original
    # announcement date. The press-release wording must identify the event.
    release_dates = {
        end for _, _, text in results_docs
        if re.search(r"press release|today announced|today reported|earnings release", text[:6000], re.I)
        if (end := _first_date(text[:6000])) is not None and 0 <= (filing_date - end).days <= 7
    }
    release_date = min(release_dates) if release_dates else None
    results = []
    for period in sorted(periods, key=lambda p: (p.end, p.scope)):
        entry = Earnings(filing.ticker, period, [filing], release_date=release_date)
        for measure in _xbrl_measures(facts, filing, period) + by_period.get(period, []):
            _merge_measure(entry, measure)
        results.append(entry)
    # Show shared guidance once when the release covers the year and fourth quarter.
    results[-1].guidance = list(dict.fromkeys(guidance))
    return results


def _periodic_result(filing, facts):
    end = _date(filing.report_date)
    if end is None:
        return Earnings(filing.ticker, None, [filing])
    scope = "quarter" if filing.form == "10-Q" else "annual"
    period = Period(scope, end)
    entry = Earnings(filing.ticker, period, [filing])
    for measure in _xbrl_measures(facts, filing, period):
        _merge_measure(entry, measure)
    return entry


def _candidate(filing):
    if filing.form in ("10-Q", "10-K", "20-F", "6-K"):
        return True
    return filing.form == "8-K" and "2.02" in filing.items


def _merge_result(target, entry):
    target.filings.extend(entry.filings)
    target.guidance = list(dict.fromkeys(target.guidance + entry.guidance))
    if entry.release_date is not None and (target.release_date is None or entry.release_date < target.release_date):
        target.release_date = entry.release_date
    for measure in entry.measures.values():
        _merge_measure(target, measure)
    for key in entry.conflicts:
        target.measures.pop(key, None)
        target.conflicts.add(key)


def _fmp_key():
    value = (os.getenv("FMP_API_KEY") or os.getenv("FINANCIAL_MODELING_PREP_API_KEY") or "").strip()
    return value if value not in ("your_financial_modeling_prep_api_key_here", "WKLEJ_KLUCZ_API") else ""


def _fmp_results(ticker, key):
    try:
        response = requests.get(FMP_URL, params={"symbol": ticker, "apikey": key}, timeout=8)
        if response.ok:
            payload = response.json()
            return payload if isinstance(payload, list) else []
    except (requests.RequestException, ValueError):
        # Request exception text can include the API key in the URL.
        logger.info("Konsensus wyników FMP niedostępny dla %s", ticker)
    return []


def _result_comparisons(entry, rows, *, source=FMP_SOURCE):
    if not entry.period or not entry.release_date or entry.period.scope == "ytd":
        return {}
    matching = []
    for row in rows:
        if not isinstance(row, dict) or str(row.get("symbol") or "").upper() != entry.ticker:
            continue
        if _date(row.get("date")) != entry.release_date:
            continue
        end = row.get("fiscalDateEnding") or row.get("fiscalPeriodEnd")
        if end is not None and _date(end) != entry.period.end:
            continue
        period = str(row.get("period") or row.get("fiscalPeriod") or "").upper()
        if entry.period.scope == "annual" and period not in ("FY", "ANNUAL"):
            continue
        if entry.period.scope == "quarter" and period and not re.fullmatch(r"Q[1-4]|QUARTER", period):
            continue
        matching.append(row)
    if len(matching) != 1:
        return {}
    row = matching[0]
    currency = row.get("reportedCurrency") or row.get("currency")
    result = {}
    for metric in ("revenue", "eps"):
        reported = _amount(row.get(f"{metric}Actual"))
        estimate = _amount(row.get(f"{metric}Estimated"))
        if reported is None or estimate is None or (metric == "revenue" and estimate <= 0):
            continue
        candidates = [
            (key, measure) for key, measure in entry.measures.items() if measure.metric == metric
            and measure.currency == (currency or "USD")
            and abs(measure.value - reported) <= measure.precision / 2
        ]
        if len(candidates) == 1:
            key, _ = candidates[0]
            result[key] = Comparison(estimate, source)
    return result


def _yahoo_eps_history(ticker):
    try:
        history = yf.Ticker(ticker).get_earnings_dates(limit=12)
        if history is None or history.empty:
            return []
        return [
            {
                "symbol": ticker,
                "date": published.date(),
                "period": "QUARTER",
                "epsActual": row.get("Reported EPS"),
                "epsEstimated": row.get("EPS Estimate"),
            }
            for published, row in history.iterrows() if isinstance(published, datetime)
        ]
    except Exception as exc:
        # yfinance uses its own exception hierarchy and HTTP transport.
        logger.debug("Konsensus EPS Yahoo Finance niedostępny (%s)", type(exc).__name__)
        return []


def _guidance_comparisons(entry, estimates):
    result = {}
    if entry.release_date is None:
        return result
    for guidance in entry.guidance:
        if guidance.end is None:
            continue
        matches = []
        for row in estimates:
            if (row.get("ticker") != entry.ticker or _date(row.get("period_end")) != guidance.end
                    or row.get("scope") != guidance.scope or row.get("metric") != guidance.metric
                    or row.get("basis") != guidance.basis or row.get("currency") != guidance.currency):
                continue
            source = row.get("source", "")
            if not isinstance(source, str) or not source.startswith("https://site.financialmodelingprep.com/"):
                continue
            captured = row.get("captured_at")
            if isinstance(captured, str):
                try:
                    captured = datetime.fromisoformat(captured.replace("Z", "+00:00"))
                except ValueError:
                    continue
            if not isinstance(captured, datetime) or captured.utcoffset() is None:
                continue
            # Date-only public release times cannot safely admit same-day snapshots.
            # UTC capture dates are conservative for US releases.
            captured_date = captured.astimezone(timezone.utc).date()
            if captured_date >= entry.release_date:
                continue
            as_of = _date(row.get("as_of"))
            if as_of is not None and (as_of > captured_date or as_of >= entry.release_date):
                continue
            value = _amount(row.get("value"))
            if value is not None and (guidance.metric != "revenue" or value > 0):
                matches.append((captured, as_of, value, source))
        if matches:
            newest = max(item[0] for item in matches)
            candidates = {(as_of, value, source) for captured, as_of, value, source in matches if captured == newest}
            if len(candidates) == 1:
                as_of, value, source = candidates.pop()
                result[guidance] = Comparison(value, source, as_of or newest.date())
    return result


def _collect_sec_results(filings):
    """Extract supported earnings with per-issuer Company Facts request reuse."""
    company_facts, events = {}, {}
    for filing in filings:
        if not _candidate(filing):
            continue
        # Test foreign current reports against actual disclosure text before
        # requesting Company Facts for unrelated Form 6-K filings.
        if filing.form == "6-K":
            entries = _exhibit_results(filing, {})
            if not entries:
                continue
        if filing.cik not in company_facts:
            company_facts[filing.cik] = _company_facts(filing.cik)
        facts = company_facts[filing.cik]
        if filing.form == "6-K":
            for entry in entries:
                if entry.period:
                    for measure in _xbrl_measures(facts, filing, entry.period):
                        _merge_measure(entry, measure)
        elif filing.form == "8-K":
            entries = _exhibit_results(filing, facts)
        else:
            entry = _periodic_result(filing, facts)
            if filing.form == "20-F" and not entry.measures and not _is_annual_20f(filing):
                continue
            entries = [entry]
        for entry in entries:
            key = (filing.cik, entry.period or filing.accession)
            if key in events:
                _merge_result(events[key], entry)
            else:
                events[key] = entry
    return events


def collect_earnings(filings, *, fmp_key=None, guidance_estimates=(), yahoo_fallback=True):
    with sec_budget(35):
        events = _collect_sec_results(filings)
    fmp_rows, yahoo_rows = {}, {}
    estimates = tuple(guidance_estimates)
    fmp_key = _fmp_key() if fmp_key is None else fmp_key
    for entry in events.values():
        if fmp_key and entry.period and entry.release_date and entry.measures:
            if entry.ticker not in fmp_rows:
                fmp_rows[entry.ticker] = _fmp_results(entry.ticker, fmp_key)
            entry.result_consensus = _result_comparisons(entry, fmp_rows[entry.ticker])
        if (yahoo_fallback and entry.period and entry.period.scope == "quarter"
                and entry.release_date and any(metric == "eps" for metric, _ in entry.measures)
                and not any(metric == "eps" for metric, _ in entry.result_consensus)):
            if entry.ticker not in yahoo_rows:
                yahoo_rows[entry.ticker] = _yahoo_eps_history(entry.ticker)
            source = f"https://finance.yahoo.com/quote/{entry.ticker}/analysis/"
            entry.result_consensus.update(_result_comparisons(entry, yahoo_rows[entry.ticker], source=source))
        entry.guidance_consensus = _guidance_comparisons(entry, estimates)
    return sorted(events.values(), key=lambda entry: (
        entry.ticker, entry.period.end if entry.period else date.min,
        entry.period.scope if entry.period else "",
    ))

def _fmt_decimal(value, places=2):
    return f"{value:,.{places}f}".rstrip("0").rstrip(".").replace(",", " ").replace(".", ",")


def _fmt_measure(value, metric, currency):
    if currency == "%":
        return f"{_fmt_decimal(value)}%"
    if metric == "eps":
        return f"{_fmt_decimal(value)} {currency}/akcję"
    for scale, label in ((Decimal(1_000_000_000), "mld"), (Decimal(1_000_000), "mln")):
        if abs(value) >= scale:
            return f"{_fmt_decimal(value / scale, 3)} {label} {currency}"
    return f"{_fmt_decimal(value)} {currency}"


def _signed(value, metric, currency):
    return ("+" if value > 0 else "") + _fmt_measure(value, metric, currency)


def _year_change(measure):
    if measure.previous is None or measure.previous == measure.value:
        return ""
    if measure.previous <= 0 or measure.value < 0:
        return f"zmiana r/r {_signed(measure.value - measure.previous, measure.metric, measure.currency)}"
    change = (measure.value / measure.previous - 1) * 100
    return f"{change:+.1f}% r/r".replace(".", ",")


def _basis_label(basis):
    return f" ({BASES[basis]})" if BASES[basis] else ""


def _measure_text(measure):
    value = _fmt_measure(measure.value, measure.metric, measure.currency)
    label = METRIC_LABELS[measure.metric] + _basis_label(measure.basis)
    change = _year_change(measure)
    if change:
        value += f" ({change})"
    return f"{label} {value}"


def _guidance_text(item):
    value = _fmt_measure(item.low, item.metric, item.currency)
    if item.high != item.low:
        value += " – " + _fmt_measure(item.high, item.metric, item.currency)
    if item.qualifier:
        value = f"{item.qualifier} {value}"
    return f"{METRIC_LABELS[item.metric]}{_basis_label(item.basis)}, {item.fiscal_label} • {value}"


def _comparison_note(entry, key, measure):
    estimate = entry.result_consensus.get(key)
    if estimate is None:
        return "brak konsensusu"
    delta = measure.value - estimate.estimate
    direction = "powyżej konsensusu" if delta > 0 else "poniżej konsensusu" if delta < 0 else "zgodnie z konsensusem"
    difference = f" • różnica {_signed(delta, measure.metric, measure.currency)}" if delta else ""
    return (f"{direction} • {_fmt_measure(estimate.estimate, measure.metric, measure.currency)}{difference} "
            f"([konsensus]({estimate.source}))")


def _guidance_note(entry, guidance):
    comparison = entry.guidance_consensus.get(guidance)
    if comparison is None:
        return "brak konsensusu"
    low, high = guidance.low, guidance.high
    direction = "konsensus w przedziale"
    if low > comparison.estimate and guidance.qualifier != "górna granica przedziału":
        direction = "powyżej konsensusu"
    elif high < comparison.estimate and guidance.qualifier != "dolna granica przedziału":
        direction = "poniżej konsensusu"
    elif guidance.qualifier in ("dolna granica przedziału", "górna granica przedziału"):
        direction = "porównanie kierunku b/d"
    elif low == high:
        direction = "zgodnie z konsensusem"
    consensus = _fmt_measure(comparison.estimate, guidance.metric, guidance.currency)
    return f"{direction} • {consensus} ([konsensus z {comparison.as_of}]({comparison.source}))"


def format_earnings_context(earnings: list[Earnings], tickers: list[str]) -> str:
    """Financial facts for the forecast prompt, grouped by portfolio ticker and fiscal period."""
    sections = []
    for entry in earnings:
        if entry.ticker not in tickers or not (entry.measures or entry.guidance):
            continue
        period = (
            f"{SCOPE_LABELS[entry.period.scope]} do {entry.period.end.isoformat()}"
            if entry.period else "okres b/d"
        )
        dates = ", ".join(sorted({filing.filing_date for filing in entry.filings}))
        lines = [f"{entry.ticker} • {period} • zgłoszono {dates or 'b/d'}"]
        for key, measure in entry.measures.items():
            comparison = _comparison_note(entry, key, measure) if measure.metric in ("eps", "revenue") else ""
            detail = f" • {comparison}" if comparison else ""
            lines.append(f"- {_measure_text(measure)}{detail}. [SEC]({measure.source})")
        for guidance in entry.guidance:
            lines.append(f"- Guidance • {_guidance_text(guidance)} • "
                         f"{_guidance_note(entry, guidance)}. [SEC]({guidance.source})")
        sections.append("\n".join(lines))
    return "\n\n".join(sections)
