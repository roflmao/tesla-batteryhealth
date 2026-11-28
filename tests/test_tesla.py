import asyncio

from app import worker
from app.tesla import TeslaClient, alert_time


def test_vehicle_list_follows_every_page():
    client, pages = TeslaClient(), []

    async def token_for(account_id):
        return "https://example.invalid", "token"

    async def get(base, token, path, **params):
        pages.append(params["page"])
        n = {1: 100, 2: 100, 3: 7}[params["page"]]
        return [{"vin": f"VIN{params['page']}-{i}"} for i in range(n)]

    client.token_for, client._get = token_for, get
    assert len(asyncio.run(client.vehicles(1))) == 207 and pages == [1, 2, 3]


def test_alert_times_are_normalised():
    assert alert_time("Thu, 26 Nov 2026 16:36:50 +0000") == "2026-11-26 16:36 UTC"     # what Tesla sends
    assert alert_time("2026-11-27T07:12:00Z") == "2026-11-27 07:12 UTC"
    assert alert_time("Sat, 28 Nov 2026 13:45:13 +0100") == "2026-11-28 12:45 UTC"
    assert alert_time(None) is None and alert_time("soon") == "soon"


def test_alerts_are_listed_newest_first_without_duplicates():
    rows = [{"ts": "Thu, 26 Nov 2026 16:36:50 +0000", "name": "UI_a183"},
            {"ts": "Thu, 19 Nov 2026 23:53:29 +0000", "name": "PCS_a090"},
            {"ts": "Sat, 28 Nov 2026 12:45:13 +0000", "name": "UI_a213"},
            {"ts": "2026-11-28 12:45 UTC", "name": "UI_a213"}]            # same alert stored again after an upgrade
    assert [r["name"] for r in worker.clean_alerts(rows)] == ["UI_a213", "UI_a183", "PCS_a090"]
