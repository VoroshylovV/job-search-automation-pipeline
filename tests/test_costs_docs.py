"""logs/costs.csv у репозиторії має заголовок, сумісний з cost_log.COSTS_HEADER
(інакше дописані рядки роз'їдуться), і містить базову лінію."""
from __future__ import annotations

import csv
from pathlib import Path

from pipeline.cost_log import COSTS_HEADER

ROOT = Path(__file__).resolve().parent.parent


def test_committed_costs_csv_header_matches_writer_and_has_baseline():
    rows = list(csv.reader(open(ROOT / "logs" / "costs.csv", encoding="utf-8")))
    assert rows[0] == COSTS_HEADER
    base = dict(zip(COSTS_HEADER, rows[1]))
    assert (base["commit"], base["total_usd"], base["found"], base["shown"]) == ("07b44cc", "0.43", "123", "2")


def test_costs_doc_mentions_baseline_and_incident():
    text = (ROOT / "docs" / "COSTS.md").read_text(encoding="utf-8")
    assert "07b44cc" in text and "$0.43" in text and "max_tokens" in text and "$8.83" in text
