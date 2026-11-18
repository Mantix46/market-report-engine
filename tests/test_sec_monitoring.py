from dataclasses import replace
from unittest.mock import Mock

from lxml import etree, html

from sec_edgar import ARCHIVES_URL, Filing, SecError
import sec_monitoring


def test_monitoring_uses_edgar_excerpts_and_deduplicates_form4(monkeypatch):
    archive = f"{ARCHIVES_URL}/123/000000012326000001"
    filing = Filing("MU", "123", "0000000123-26-000001", "4", "2026-11-17", "", "", (),
                    "xslF345X06/primarydocument.xml", f"{archive}/index.htm")
    event = replace(filing, accession="0000000123-26-000002", form="6-K", primary_document="filing.htm")
    xml = etree.fromstring(b"""<ownershipDocument>
      <aff10b5One>1</aff10b5One><reportingOwner><reportingOwnerId><rptOwnerName>Example Person</rptOwnerName>
      </reportingOwnerId></reportingOwner><nonDerivativeTable>
      <nonDerivativeTransaction><transactionDate><value>2026-11-16</value></transactionDate>
      <transactionCoding><transactionCode>S</transactionCode></transactionCoding><transactionAmounts>
      <transactionShares><value>100</value></transactionShares><transactionPricePerShare><value>42.75</value>
      </transactionPricePerShare><transactionAcquiredDisposedCode><value>D</value></transactionAcquiredDisposedCode>
      </transactionAmounts></nonDerivativeTransaction>
      </nonDerivativeTable></ownershipDocument>""")
    table = xml.find("nonDerivativeTable")
    grant = etree.fromstring(etree.tostring(table[0]))
    grant.find("transactionCoding/transactionCode").text = "A"
    table.append(grant)

    mock_xml = Mock(return_value=xml)
    monkeypatch.setattr(sec_monitoring, "_form4_document", mock_xml)
    mock_html = Mock(return_value=[(event.url, html.fromstring("<html><body>Open a factory</body></html>"))])
    monkeypatch.setattr(sec_monitoring, "_filing_documents", mock_html)

    docs, trades = sec_monitoring.collect_sec_monitoring([filing, event], [filing])

    mock_xml.assert_called_once_with(filing)
    mock_html.assert_called_once_with(event, limit=2, exhibits_first=True)
    assert docs == {event.accession: [(event.url, "Open a factory")]}
    trade, = trades["MU"]
    assert (trade["side"], trade["shares"], trade["plan"], trade["url"]) == ("sprzedaż", 100, True, filing.url)

    amendment = replace(filing, form="4/A", accession="0000000123-26-000003", filing_date="2026-11-18")
    _, trades = sec_monitoring.collect_sec_monitoring([], [filing, amendment])
    assert [trade["form"] for trade in trades["MU"]] == ["4/A"]


def test_8k_excerpt_starts_at_substantive_item(monkeypatch):
    url = f"{ARCHIVES_URL}/123/000000012326000004/current.htm"
    filing = Filing("MU", "123", "0000000123-26-000004", "8-K", "2026-11-18", "", "",
                    ("2.02",), "current.htm", url)
    document = html.fromstring("<html><body>" + "Cover " * 500 + "Item 2.02 Quarterly results</body></html>")
    monkeypatch.setattr(sec_monitoring, "_filing_documents", Mock(return_value=[(url, document)]))
    documents, _ = sec_monitoring.collect_sec_monitoring([filing], [])
    assert documents[filing.accession] == [(url, "Item 2.02 Quarterly results")]


def test_unavailable_sec_form4_keeps_report_renderable(monkeypatch):
    filing = Filing("MU", "123", "0000000123-26-000001", "4", "2026-11-17", "", "", (),
                    "primarydocument.xml", f"{ARCHIVES_URL}/index.htm")
    monkeypatch.setattr(sec_monitoring, "_form4_document", Mock(side_effect=SecError("HTTP 503")))

    assert sec_monitoring.collect_sec_monitoring([filing], [filing]) == ({}, {})
