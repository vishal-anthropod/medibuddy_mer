import json
import os
from pathlib import Path

import pytest

# Ensure app imports with default RECORDS_DIR (repo path)
from app import app, RECORDS_DIR


def find_records_with_qc2(base_dir: Path):
    results = []
    for d in base_dir.iterdir():
        if not d.is_dir():
            continue
        proc = d / "_processed"
        if proc.exists() and (proc / "merged_qa_report_part2.json").exists():
            results.append(d.name)
    return results


@pytest.mark.parametrize("rid", find_records_with_qc2(Path(RECORDS_DIR)))
def test_report2_returns_qc_parameters(rid):
    with app.test_client() as c:
        # Use call 1 by default; endpoint will prefer merged anyway
        rv = c.get(f"/api/records/{rid}/calls/1/report2")
        assert rv.status_code == 200, f"HTTP {rv.status_code} for rid={rid}"
        data = rv.get_json() or {}
        assert "qc_parameters" in data, f"qc_parameters missing for rid={rid}"


@pytest.mark.parametrize("rid", find_records_with_qc2(Path(RECORDS_DIR)))
def test_qcscore_present(rid):
    with app.test_client() as c:
        rv = c.get(f"/api/records/{rid}/qcscore")
        assert rv.status_code == 200
        data = rv.get_json() or {}
        # total_score may be 0 but structure should exist
        assert "total_score" in data and "max_score" in data


