import json
import time

import pytest
import zmq
from fastapi.testclient import TestClient

from app import db, telemetry
from app.main import app
from app.mock import VIN

AUTH = {"Authorization": "Bearer test-token"}


@pytest.fixture()
def client():
    with TestClient(app) as c:
        with db.conn() as conn:
            conn.execute("UPDATE assessments SET status='stopped'")
            conn.execute("DELETE FROM telemetry")
        c.get("/auth/login")
        yield c


def running(client):
    client.post("/assessments", data={"vin": VIN, "ref_range_km": "507", "ref_capacity_kwh": "75"}, follow_redirects=False)
    with db.conn() as conn:
        return conn.execute("SELECT * FROM assessments WHERE status='running'").fetchone()


def stored(field):
    with db.conn() as conn:
        return [r["num"] for r in conn.execute("SELECT num FROM telemetry WHERE field=? ORDER BY rowid", (field,))]


def test_record_time_accepts_fleet_telemetry_timestamps():
    assert telemetry.record_time({"ts": 1700000000}) == 1700000000
    assert telemetry.record_time({"createdAt": "2026-11-28T13:10:00Z"}) == 1795871400
    assert telemetry.record_time({"createdAt": "2026-11-28T13:10:00.123456789Z"}) == 1795871400
    assert abs(telemetry.record_time({"createdAt": "garbage"}) - time.time()) < 5


def test_records_for_cars_without_an_assessment_are_dropped(client):
    a = running(client)
    res = telemetry.store([{"vin": "5YJ3UNKNOWN0000000", "ts": a["started_at"] + 1, "data": {"Soc": 50}},
                           {"vin": VIN, "ts": a["started_at"] + 1, "data": {"Soc": 51}},
                           "not a record", {"vin": VIN}])
    assert (res["stored"], res["skipped"]) == (1, 3) and stored("Soc") == [51.0]


def test_malformed_ingest_body_is_a_400(client):
    assert client.post("/ingest/telemetry", headers=AUTH, content="nope").status_code == 400
    assert client.post("/ingest/telemetry", headers=AUTH, json="text").status_code == 400
    assert client.post("/ingest/telemetry", json=[]).status_code == 401


def test_records_published_by_fleet_telemetry_are_stored(client):
    """End to end over a real ZMQ socket, in the format the server emits with transmit_decoded_records."""
    a = running(client)
    record = {"data": [{"key": "BrickVoltageMax", "value": {"doubleValue": 4.012}},
                       {"key": "Hvil", "value": {"hvilStatusValue": "HvilStatusOk"}},
                       {"key": "ModuleTempMax", "value": {"invalid": True}}],
              "createdAt": "2026-11-28T13:10:00.5Z", "vin": VIN, "isResend": False}
    record["createdAt"] = time.strftime("%Y-%m-%dT%H:%M:%S.5Z", time.gmtime(a["started_at"] + 2))
    pub = zmq.Context.instance().socket(zmq.PUB)
    pub.bind("tcp://127.0.0.1:35284")
    try:
        deadline = time.time() + 10
        while not stored("BrickVoltageMax") and time.time() < deadline:      # a subscriber joins a moment after bind
            pub.send_multipart([b"tesla_connectivity", b"{}"])
            pub.send_multipart([b"tesla_V", json.dumps(record).encode()])
            time.sleep(0.2)
    finally:
        pub.close(linger=0)
    assert stored("BrickVoltageMax")[0] == 4.012 and stored("ModuleTempMax") == []
    with db.conn() as conn:
        assert conn.execute("SELECT txt FROM telemetry WHERE field='Hvil'").fetchone()["txt"] == "HvilStatusOk"


def test_config_sent_to_the_car():
    config = telemetry.build_config()
    assert config["hostname"] == "telemetry.example.invalid" and config["port"] == 4443 and config["prefer_typed"] is True
    assert config["fields"]["BrickVoltageMax"] == {"interval_seconds": 300} and "ca" not in config


def test_enable_and_disable_from_the_setup_page(client):
    page = client.get(f"/telemetry/{VIN}")
    assert page.status_code == 200 and "Enable telemetry" in page.text and "Not sent yet" in page.text
    assert client.post(f"/telemetry/{VIN}/enable", follow_redirects=False).status_code == 303
    page = client.get(f"/telemetry/{VIN}")
    assert "The car has the configuration" in page.text and "telemetry.example.invalid" in page.text
    client.post(f"/telemetry/{VIN}/disable")
    assert "Not sent yet" in client.get(f"/telemetry/{VIN}").text


def test_setup_page_needs_a_signed_in_owner_of_the_car(client):
    assert client.get("/telemetry/5YJ3SOMEONEELSE0001").status_code == 403
    with TestClient(app) as anonymous:
        assert anonymous.get(f"/telemetry/{VIN}").status_code == 401
        assert anonymous.post(f"/telemetry/{VIN}/enable").status_code == 401
