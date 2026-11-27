import pytest
from fastapi.testclient import TestClient

from app import db
from app.main import app
from app.mock import VIN


@pytest.fixture()
def client():
    with TestClient(app) as c:
        with db.conn() as conn:
            conn.execute("UPDATE assessments SET status='done'")   # leftovers from earlier tests
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
        conn.execute("UPDATE assessments SET status='done'")
    assert start(client, ref_capacity_kwh="74,5").status_code == 303


def test_second_start_reuses_running_assessment(client):
    first, second = start(client), start(client)
    assert first.status_code == second.status_code == 303
    assert first.headers["location"] == second.headers["location"]
    with db.conn() as conn:
        assert conn.execute("SELECT count(*) FROM assessments WHERE status='running'").fetchone()[0] == 1


def test_new_sign_in_takes_over_the_running_assessment(client):
    first = start(client)
    with TestClient(app) as again:                     # same owner, new sign-in = new account row
        again.get("/auth/login")
        second = start(again)
        assert second.status_code == 303 and second.headers["location"] == first.headers["location"]
        assert again.get(first.headers["location"]).status_code == 200
    with db.conn() as conn:
        assert conn.execute("SELECT count(*) FROM assessments WHERE status='running'").fetchone()[0] == 1
