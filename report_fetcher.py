"""
report_fetcher.py - Backward compatibility wrapper for report generation.
All logic has been modularized into:
  - data_fetching.py
  - technicals.py
  - snapshot_store.py
  - report_builder.py
"""

from report_builder import build_report, build_basic_report, DataQualityError
