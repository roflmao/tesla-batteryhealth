import asyncio

from app.tesla import TeslaClient


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
