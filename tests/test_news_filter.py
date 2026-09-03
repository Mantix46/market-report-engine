"""Filtrowanie newsów: świeżość i de-duplikacja."""

import os
import sys
from datetime import datetime, timedelta, timezone

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from news_fetcher import filter_articles, parse_article_datetime  # noqa: E402


def test_parse_article_datetime_formats():
    iso = parse_article_datetime("2026-09-03T08:30:00Z")
    assert iso.year == 2026 and iso.hour == 8
    compact = parse_article_datetime("2026-09-03 08:30")
    assert compact.day == 3
    unix = parse_article_datetime(1756880000)
    assert unix.tzinfo is not None
    assert parse_article_datetime("") is None


def test_filter_drops_old_and_duplicates():
    now = datetime(2026, 9, 3, 12, 0, tzinfo=timezone.utc)
    articles = [
        {"title": "Micron beats estimates", "date": "2026-09-03 10:00", "url": "a"},
        {"title": "Micron  beats   estimates!!!", "date": "2026-09-03 09:00", "url": "b"},
        {"title": "Old CPI print", "date": "2026-08-30 08:00", "url": "c"},
        {"title": "No date story", "date": "", "url": "d"},
    ]
    kept = filter_articles(articles, max_age_hours=36, max_keep=3, now=now)
    titles = [a["title"] for a in kept]
    assert titles[0] == "Micron beats estimates"
    assert "Old CPI print" not in titles
    assert "No date story" in titles
    assert len([t for t in titles if "Micron" in t]) == 1


def test_saturday_window_keeps_week_old():
    now = datetime(2026, 9, 5, 12, 0, tzinfo=timezone.utc)
    articles = [
        {"title": "Monday note", "date": (now - timedelta(days=5)).strftime("%Y-%m-%d %H:%M")},
    ]
    daily = filter_articles(articles, max_age_hours=36, now=now)
    weekly = filter_articles(articles, max_age_hours=168, now=now)
    assert daily == []
    assert weekly[0]["title"] == "Monday note"
