"""Thin Tesla Fleet API client (OAuth third-party token flow + the few vehicle endpoints we need)."""
import asyncio
import base64
import json
import time
from urllib.parse import urlencode

import httpx

from . import db
from .config import cfg

DATA_ENDPOINTS = "charge_state;vehicle_state;vehicle_config;climate_state"


class VehicleUnavailable(Exception):
    """Vehicle asleep/offline (HTTP 408) or we are rate limited (429)."""


class AuthExpired(Exception):
    """Refresh token no longer valid; the owner must log in again."""


def token_subject(tok: dict) -> str | None:
    """Tesla user id ("sub") from the id_token of a token response.

    The signature is not checked, so only use this on a response received
    directly from Tesla's token endpoint, never on a token a browser handed over.
    """
    try:
        payload = tok["id_token"].split(".")[1]
        return json.loads(base64.urlsafe_b64decode(payload + "=" * (-len(payload) % 4))).get("sub") or None
    except Exception:
        return None


def parse_alerts(resp) -> list[dict]:
    """The alert list out of a recent_alerts answer, whichever of the two shapes it has."""
    return resp.get("recent_alerts", []) if isinstance(resp, dict) else (resp or [])


class TeslaClient:
    def __init__(self) -> None:
        self.http = httpx.AsyncClient(timeout=30)
        self._locks: dict[int, asyncio.Lock] = {}

    # ---- OAuth -----------------------------------------------------------
    def authorize_url(self, state: str) -> str:
        q = {
            "response_type": "code",
            "client_id": cfg.client_id,
            "redirect_uri": cfg.redirect_uri,
            "scope": cfg.scopes,
            "state": state,
            "prompt_missing_scopes": "true",
        }
        return f"{cfg.authorize_url}?{urlencode(q)}"

    async def exchange_code(self, code: str) -> dict:
        r = await self.http.post(cfg.token_url, data={
            "grant_type": "authorization_code",
            "client_id": cfg.client_id,
            "client_secret": cfg.client_secret,
            "code": code,
            "audience": cfg.fleet_base,
            "redirect_uri": cfg.redirect_uri,
        })
        r.raise_for_status()
        return r.json()

    async def _refresh(self, refresh_token: str) -> dict:
        r = await self.http.post(cfg.token_url, data={
            "grant_type": "refresh_token",
            "client_id": cfg.client_id,
            "refresh_token": refresh_token,
        })
        if r.status_code in (400, 401):
            raise AuthExpired(r.text)
        r.raise_for_status()
        return r.json()

    async def token_for(self, account_id: int) -> tuple[str, str]:
        """Return (base_url, access_token), refreshing if needed.

        Refresh tokens are single use, so refreshes are serialised per account
        and the new refresh token is persisted before the access token is used.
        """
        lock = self._locks.setdefault(account_id, asyncio.Lock())
        async with lock:
            with db.conn() as c:
                a = c.execute("SELECT * FROM accounts WHERE id=?", (account_id,)).fetchone()
            if a is None:
                raise AuthExpired("unknown account")
            if a["expires_at"] - 120 > time.time():
                return a["base_url"], db.dec(a["access_token"])
            rt = db.dec(a["refresh_token"])
            if not rt:
                raise AuthExpired("no refresh token (offline_access scope missing?)")
            tok = await self._refresh(rt)
            db.save_tokens(account_id, a["base_url"], tok)
            return a["base_url"], tok["access_token"]

    # ---- API -------------------------------------------------------------
    async def _get(self, base: str, token: str, path: str, **params) -> dict:
        r = await self.http.get(f"{base}{path}", params=params or None,
                                headers={"Authorization": f"Bearer {token}"})
        if r.status_code in (408, 429):
            raise VehicleUnavailable(f"{r.status_code} {r.text[:200]}")
        if r.status_code == 401:
            raise AuthExpired(r.text[:200])
        r.raise_for_status()
        return r.json()["response"]

    async def region_base(self, token: str) -> str:
        """Ask Tesla which regional base URL this account lives in."""
        try:
            resp = await self._get(cfg.fleet_base, token, "/api/1/users/region")
            return resp.get("fleet_api_base_url") or cfg.fleet_base
        except Exception:
            return cfg.fleet_base

    async def vehicles(self, account_id: int) -> list[dict]:
        base, token = await self.token_for(account_id)
        return await self._get(base, token, "/api/1/vehicles")

    async def vehicle(self, account_id: int, vin: str) -> dict:
        """Cheap connectivity check; does not contact the car."""
        base, token = await self.token_for(account_id)
        return await self._get(base, token, f"/api/1/vehicles/{vin}")

    async def vehicle_data(self, account_id: int, vin: str) -> dict:
        """Live call to the car. Billable. Raises VehicleUnavailable if asleep."""
        base, token = await self.token_for(account_id)
        return await self._get(base, token, f"/api/1/vehicles/{vin}/vehicle_data", endpoints=DATA_ENDPOINTS)

    async def recent_alerts(self, account_id: int, vin: str):
        """Raw answer; pass it through parse_alerts()."""
        base, token = await self.token_for(account_id)
        return await self._get(base, token, f"/api/1/vehicles/{vin}/recent_alerts")

    async def wake(self, account_id: int, vin: str) -> None:
        base, token = await self.token_for(account_id)
        r = await self.http.post(f"{base}/api/1/vehicles/{vin}/wake_up", json={},   # Tesla wants a JSON content type
                                 headers={"Authorization": f"Bearer {token}"})
        if r.status_code >= 400:
            raise VehicleUnavailable(f"wake failed: {r.status_code}")
