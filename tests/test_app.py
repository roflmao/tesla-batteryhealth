import asyncio

import httpx
import pytest
from fastapi.testclient import TestClient

from app import db, main, tesla, worker
from app.main import app
from app.mock import VIN


@pytest.fixture()
def client():
    with TestClient(app) as c:
        with db.conn() as conn:
            conn.execute("UPDATE assessments SET status='stopped'")   # leftovers from earlier tests
        assert c.get("/auth/login").status_code == 200             # mock sign-in, lands on /
        yield c


def start(c, **form):
    return c.post("/assessments", data={"vin": VIN, "ref_range_km": "507", **form}, follow_redirects=False)


@pytest.mark.parametrize("cap", ["0", "-75", "inf", "nan", "abc", "9", "1000"])
def test_invalid_capacity_rejected(client, cap):
    assert start(client, ref_capacity_kwh=cap).status_code == 400


@pytest.mark.parametrize("rng", ["nan", "inf", "50"])
def test_invalid_range_rejected(client, rng):
    assert start(client, ref_range_km=rng).status_code == 400


def test_capacity_optional_and_comma_accepted(client):
    assert start(client, ref_capacity_kwh="").status_code == 303
    with db.conn() as conn:
        conn.execute("UPDATE assessments SET status='stopped'")
    assert start(client, ref_capacity_kwh="74,5").status_code == 303


def test_second_start_reuses_running_assessment(client):
    first, second = start(client), start(client)
    assert first.status_code == second.status_code == 303
    assert first.headers["location"] == second.headers["location"]
    with db.conn() as conn:
        assert conn.execute("SELECT count(*) FROM assessments WHERE status='running'").fetchone()[0] == 1


def test_other_reference_values_while_running_are_refused(client):
    assert start(client, ref_capacity_kwh="75").status_code == 303
    assert start(client, ref_capacity_kwh="60").status_code == 409
    assert start(client, ref_range_km="450", ref_capacity_kwh="75").status_code == 409


def test_signing_in_again_keeps_the_same_account(client):
    first = start(client)
    with TestClient(app) as again:
        again.get("/auth/login")
        assert again.get(first.headers["location"]).status_code == 200
        assert start(again).headers["location"] == first.headers["location"]
    with db.conn() as conn:
        assert conn.execute("SELECT count(*) FROM accounts WHERE tesla_sub='mock-mock'").fetchone()[0] == 1


def test_another_tesla_user_gets_their_own_assessment(client, monkeypatch):
    first = start(client)
    exchange = main.client.exchange_code
    monkeypatch.setattr(main.client, "exchange_code", lambda code: exchange("someone-else"))
    with TestClient(app) as other:
        other.get("/auth/login")
        assert other.get(first.headers["location"]).status_code == 404
        second = start(other)
        assert second.status_code == 303 and second.headers["location"] != first.headers["location"]
    assert client.get(second.headers["location"]).status_code == 404
    assert client.get(first.headers["location"]).status_code == 200


def test_token_subject_tolerates_missing_or_broken_id_token():
    assert tesla.token_subject({}) is None
    assert tesla.token_subject({"id_token": "not-a-jwt"}) is None


def test_late_telemetry_inside_the_window_rebuilds_the_report(client):
    url = start(client, ref_capacity_kwh="75").headers["location"]
    with db.conn() as conn:
        a = conn.execute("SELECT * FROM assessments WHERE status='running'").fetchone()
        conn.execute("DELETE FROM telemetry")          # simulator readings left by earlier tests
    worker.finalize(a)
    before = client.get(url + "/report.json").json()["summary"]["soh_pct"]

    def ingest(ts):
        return client.post("/ingest/telemetry", headers={"Authorization": "Bearer test-token"},
                           json={"vin": VIN, "ts": ts, "data": {"NominalFullPackEnergyKwh": 60.0}}).json()

    assert ingest(a["ends_at"] + 5)["reports_rebuilt"] == 0          # after the window: not this report's data
    assert ingest(a["started_at"] + 5)["reports_rebuilt"] == 1
    assert before != 80.0 and client.get(url + "/report.json").json()["summary"]["soh_pct"] == 80.0


def test_transient_tesla_failure_is_stored_so_polling_backs_off(client):
    start(client)
    with db.conn() as conn:
        a = conn.execute("SELECT * FROM assessments WHERE status='running'").fetchone()
        conn.execute("DELETE FROM samples WHERE assessment_id=?", (a["id"],))

    class Down:
        calls = 0

        async def vehicle(self, account_id, vin):
            Down.calls += 1
            raise httpx.ConnectError("tesla is down")

    asyncio.run(worker.poll_one(Down(), a))
    asyncio.run(worker.poll_one(Down(), a))          # next tick: must wait for the interval, not call again
    assert Down.calls == 1
    with db.conn() as conn:
        assert [r["state"] for r in conn.execute("SELECT state FROM samples WHERE assessment_id=?", (a["id"],))] == ["offline"]
