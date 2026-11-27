"""Replays every recorded run in tests/fixtures (see app/runlog.py and app/replay.py).

To add a real car: run an assessment, then
    python -m app.replay runs/<file>.jsonl --scrub tests/fixtures/<name>.jsonl
"""
import glob
import json
import os

import pytest

from app import replay

FIXTURES = sorted(glob.glob(os.path.join(os.path.dirname(__file__), "fixtures", "*.jsonl")))


@pytest.mark.parametrize("path", FIXTURES, ids=[os.path.basename(p) for p in FIXTURES])
def test_fixture_replays(path):
    rep = replay.replay(path)
    soh = rep["summary"]["soh_pct"]
    if rep["estimate"]["samples_online"]:
        assert soh is not None and 40 < soh < 115
    # The figure the live run reported must still come out of today's code.
    if rep["recorded"] and rep["recorded"].get("soh_pct") is not None:
        assert soh == pytest.approx(rep["recorded"]["soh_pct"], abs=0.1)
    assert all(m["value"] for m in rep["metrics"])


def test_mock_run_recovers_simulated_pack():
    rep = replay.replay(os.path.join(os.path.dirname(__file__), "fixtures", "mock-model3.jsonl"))
    e = rep["estimate"]
    assert e["capacity_kwh"] == pytest.approx(68.4, abs=1.5)      # mock.TRUE_CAPACITY_KWH
    assert e["soh_range_pct"] == pytest.approx(289 * 1.609344 / 507 * 100, abs=1.0)
    assert e["confidence"] == "high"
    assert {m["key"]: m["status"] for m in rep["metrics"]}["bms_faults"] == "watch"
    assert not [m for m in rep["metrics"] if m["status"] == "na"]


def test_scrub_removes_identifiers(tmp_path):
    src, dst = tmp_path / "run.jsonl", tmp_path / "out.jsonl"
    vin = "5YJ3E7EB1NF123456"
    src.write_text("\n".join(json.dumps(x) for x in [
        {"t": 1, "kind": "start", "data": {"vin": vin, "ref_range_km": 500, "ref_capacity_kwh": None, "started_at": 1, "ends_at": 9}},
        {"t": 2, "kind": "vehicle", "data": {"vin": vin, "state": "online", "id": 99, "user_id": 7, "display_name": "My car"}},
        {"t": 3, "kind": "vehicle_data", "data": {"vin": vin, "vehicle_state": {"vehicle_name": "My car", "odometer": 10.0},
                                                 "charge_state": {"battery_level": 80, "battery_range": 240.0}}},
    ]))
    replay.scrub(str(src), str(dst))
    text = dst.read_text()
    assert "123456" not in text and "My car" not in text and "user_id" not in text
    assert "5YJ3E7EB1NF000000" in text
    assert replay.replay(str(dst))["estimate"]["samples_online"] == 1
