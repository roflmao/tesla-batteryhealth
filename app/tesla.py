"""Thin Tesla Fleet API client (OAuth third-party token flow + the few vehicle endpoints we need)."""
import asyncio
import base64
import json
import time
from datetime import datetime, timezone
from email.utils import parsedate_to_datetime
from urllib.parse import urlencode

import httpx

from . import db
from .config import cfg

DATA_ENDPOINTS = "charge_state;vehicle_state;vehicle_config;climate_state"


class VehicleUnavailable(Exception):
    """Vehicle asleep/offline (HTTP 408) or we are rate limited (429)."""


class TeslaError(Exception):
    """Tesla refused a request; the message is Tesla's own explanation."""


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


def alert_time(value) -> str | None:
    """An alert's time as "YYYY-MM-DD HH:MM UTC", which also sorts chronologically.

    Tesla sends RFC 2822 ("Thu, 26 Nov 2026 16:36:50 +0000"); ISO 8601 is accepted
    too. Anything else is kept as it came rather than dropped.
    """
    if not value:
        return None
    for parse in (parsedate_to_datetime, lambda s: datetime.fromisoformat(s.replace("Z", "+00:00"))):
        try:
            dt = parse(str(value))
        except (TypeError, ValueError):
            continue
        if dt.tzinfo is None:
            dt = dt.replace(tzinfo=timezone.utc)
        return dt.astimezone(timezone.utc).strftime("%Y-%m-%d %H:%M UTC")
    return str(value)


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
        """Every vehicle on the account. Tesla returns at most 100 per page."""
        base, token = await self.token_for(account_id)
        out: list[dict] = []
        for page in range(1, 51):                       # 5000 vehicles; stops a misbehaving API looping forever
            batch = await self._get(base, token, "/api/1/vehicles", page=page, per_page=100)
            out.extend(batch or [])
            if not batch or len(batch) < 100:
                break
        return out

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

    # ---- Fleet Telemetry ------------------------------------------------
    async def _call(self, method: str, url: str, token: str, **kw) -> dict:
        """A request whose refusal the user should read, e.g. "missing_key" or a proxy that is not running."""
        try:
            r = await self.http.request(method, url, headers={"Authorization": f"Bearer {token}"}, **kw)
        except httpx.HTTPError as e:
            raise TeslaError(f"{url.split('/api/')[0]} could not be reached: {e}") from e
        if r.status_code == 401:
            raise AuthExpired(r.text[:200])
        if r.status_code >= 400:
            raise TeslaError(f"{r.status_code} {r.text[:400]}")
        body = r.json()
        return body.get("response", body) if isinstance(body, dict) else {}

    async def fleet_status(self, account_id: int, vin: str) -> dict:
        """Whether this application's virtual key is on the car, and the car's firmware."""
        base, token = await self.token_for(account_id)
        return await self._call("POST", f"{base}/api/1/vehicles/fleet_status", token, json={"vins": [vin]})

    async def telemetry_config(self, account_id: int, vin: str) -> dict:
        base, token = await self.token_for(account_id)
        return await self._call("GET", f"{base}/api/1/vehicles/{vin}/fleet_telemetry_config", token)

    async def telemetry_errors(self, account_id: int, vin: str) -> dict:
        base, token = await self.token_for(account_id)
        return await self._call("GET", f"{base}/api/1/vehicles/{vin}/fleet_telemetry_errors", token)

    async def telemetry_enable(self, account_id: int, vin: str, config: dict) -> dict:
        """Send the streaming configuration through the vehicle-command proxy, which signs it with the app's key."""
        _, token = await self.token_for(account_id)
        async with httpx.AsyncClient(timeout=60, verify=cfg.command_proxy_ca or True) as proxy:
            try:
                r = await proxy.post(f"{cfg.command_proxy_url.rstrip('/')}/api/1/vehicles/fleet_telemetry_config",
                                     headers={"Authorization": f"Bearer {token}"}, json={"vins": [vin], "config": config})
            except httpx.HTTPError as e:
                raise TeslaError(f"the vehicle-command proxy could not be reached: {e}") from e
        if r.status_code >= 400:
            raise TeslaError(f"{r.status_code} {r.text[:400]}")
        body = r.json()
        return body.get("response", body) if isinstance(body, dict) else {}

    async def telemetry_disable(self, account_id: int, vin: str) -> dict:
        base, token = await self.token_for(account_id)
        return await self._call("DELETE", f"{base}/api/1/vehicles/{vin}/fleet_telemetry_config", token)

    async def wake(self, account_id: int, vin: str) -> None:
        base, token = await self.token_for(account_id)
        r = await self.http.post(f"{base}/api/1/vehicles/{vin}/wake_up", json={},   # Tesla wants a JSON content type
                                 headers={"Authorization": f"Bearer {token}"})
        if r.status_code >= 400:
            raise VehicleUnavailable(f"wake failed: {r.status_code}")
