"""Battery health assessment tool for Tesla vehicles (Fleet API). FastAPI entry point."""
import asyncio
import base64
import hmac
import json
import os
import secrets
import time
from contextlib import asynccontextmanager
from datetime import datetime, timezone

from fastapi import FastAPI, Form, HTTPException, Request
from fastapi.responses import HTMLResponse, JSONResponse, PlainTextResponse, RedirectResponse, Response
from fastapi.templating import Jinja2Templates
from starlette.middleware.sessions import SessionMiddleware

from . import db, runlog, worker
from .config import DEV_SECRET_KEY, cfg
from .mock import MockClient
from .tesla import AuthExpired, TeslaClient

client = MockClient() if cfg.mock else TeslaClient()
templates = Jinja2Templates(directory=os.path.join(os.path.dirname(__file__), "templates"))
templates.env.filters["d"] = lambda ts: datetime.fromtimestamp(ts, timezone.utc).strftime("%d %B %Y")
templates.env.filters["dt"] = lambda ts: datetime.fromtimestamp(ts, timezone.utc).strftime("%Y-%m-%d %H:%M UTC")
INGEST_TOKEN = os.environ.get("BH_INGEST_TOKEN", "")
OPEN_PATHS = ("/healthz", "/.well-known/", "/ingest/")


@asynccontextmanager
async def lifespan(app: FastAPI):
    if not cfg.mock and (cfg.secret_key == DEV_SECRET_KEY or len(cfg.secret_key) < 16):
        raise RuntimeError("BH_SECRET_KEY must be set to a random value of at least 16 characters "
                           "(it signs sessions and encrypts stored tokens). Only BH_MOCK=1 runs without it.")
    db.init()
    task = asyncio.create_task(worker.run(client))
    yield
    task.cancel()


app = FastAPI(title="Battery health", lifespan=lifespan, docs_url=None, redoc_url=None)


@app.middleware("http")
async def basic_auth(request: Request, call_next):
    if cfg.basic_auth and not request.url.path.startswith(OPEN_PATHS):
        hdr = request.headers.get("authorization", "")
        ok = False
        if hdr.lower().startswith("basic "):
            try:
                ok = hmac.compare_digest(base64.b64decode(hdr[6:]).decode(), cfg.basic_auth)
            except Exception:
                ok = False
        if not ok:
            return Response(status_code=401, headers={"WWW-Authenticate": 'Basic realm="batteryhealth"'})
    return await call_next(request)


app.add_middleware(SessionMiddleware, secret_key=cfg.secret_key, same_site="lax",
                   https_only=cfg.redirect_uri.startswith("https://"))


def account_id(request: Request) -> int | None:
    return request.session.get("account_id")


def get_assessment(request: Request, aid: int):
    with db.conn() as c:
        a = c.execute("SELECT * FROM assessments WHERE id=? AND account_id=?", (aid, account_id(request) or -1)).fetchone()
    if a is None:
        raise HTTPException(404)
    return a


# ---- pages ---------------------------------------------------------------
@app.get("/", response_class=HTMLResponse)
async def index(request: Request):
    acc, vehicles, error = account_id(request), [], None
    assessments = []
    if acc:
        try:
            vehicles = await client.vehicles(acc)
        except AuthExpired:
            request.session.clear()
            return RedirectResponse("/", 303)
        except Exception as e:
            error = f"Could not list vehicles: {e}"
        with db.conn() as c:
            assessments = c.execute("SELECT * FROM assessments WHERE account_id=? ORDER BY id DESC", (acc,)).fetchall()
    return templates.TemplateResponse(request, "index.html", {
        "connected": bool(acc), "vehicles": vehicles, "assessments": assessments, "error": error,
        "cfg": cfg, "now": int(time.time())})


@app.get("/auth/login")
async def login(request: Request):
    state = secrets.token_urlsafe(24)
    request.session["oauth_state"] = state
    return RedirectResponse(client.authorize_url(state), 303)


@app.get("/auth/callback")
async def callback(request: Request, code: str = "", state: str = "", error: str = ""):
    expected = request.session.pop("oauth_state", None)
    if error:
        raise HTTPException(400, f"Tesla returned: {error}")
    if not code or not expected or not hmac.compare_digest(state, expected):
        raise HTTPException(400, "Invalid OAuth state")
    tok = await client.exchange_code(code)
    base = await client.region_base(tok["access_token"])
    request.session["account_id"] = db.save_tokens(None, base, tok)
    return RedirectResponse("/", 303)


@app.post("/auth/logout")
async def logout(request: Request):
    request.session.clear()
    return RedirectResponse("/", 303)


@app.post("/assessments")
async def start(request: Request, vin: str = Form(...), name: str = Form(""), ref_range_km: float = Form(...),
                ref_capacity_kwh: str = Form(""), wake: str = Form("")):
    acc = account_id(request)
    if not acc:
        raise HTTPException(401)
    if ref_range_km < 100 or ref_range_km > 1000:
        raise HTTPException(400, "Reference range must be 100-1000 km")
    if vin not in {v.get("vin") for v in await client.vehicles(acc)}:
        raise HTTPException(403, "VIN is not on this Tesla account")
    cap = float(ref_capacity_kwh.replace(",", ".")) if ref_capacity_kwh.strip() else None
    now = int(time.time())
    with db.conn() as c:
        cur = c.execute(
            "INSERT INTO assessments(account_id, vin, name, ref_range_km, ref_capacity_kwh, started_at, ends_at) VALUES (?,?,?,?,?,?,?)",
            (acc, vin, name, ref_range_km, cap, now, now + cfg.window_seconds))
        aid = cur.lastrowid
        a = c.execute("SELECT * FROM assessments WHERE id=?", (aid,)).fetchone()
    runlog.record(a, "start", {"vin": vin, "ref_range_km": ref_range_km, "ref_capacity_kwh": cap,
                               "started_at": now, "ends_at": a["ends_at"]})
    if wake and cfg.allow_wake:
        try:
            await client.wake(acc, vin)
        except Exception:
            pass
    asyncio.create_task(worker.fetch_alerts(client, a))
    return RedirectResponse(f"/assessments/{aid}", 303)


def _report(a) -> dict:
    return json.loads(a["result_json"]) if a["status"] == "done" and a["result_json"] else worker.build_report(a)


def _chart(samples: list[dict], start: int, end: int, w: int = 640, h: int = 140) -> dict:
    pts = [(s["ts"], s["battery_level"], s["charging_state"]) for s in samples if s["battery_level"] is not None]
    span = max(end - start, 1)
    xy = [(round((t - start) / span * w, 1), round(h - lvl / 100 * h, 1), cs == "Charging") for t, lvl, cs in pts]
    return {"w": w, "h": h, "line": " ".join(f"{x},{y}" for x, y, _ in xy), "dots": xy}


def _brick_chart(rows: list, start: int, end: int, w: int = 640, h: int = 140) -> dict | None:
    if len(rows) < 2:
        return None
    lo, hi = min(r[2] for r in rows) - 0.02, max(r[1] for r in rows) + 0.02
    span, rng = max(end - start, 1), max(hi - lo, 0.01)
    fx = lambda t: round((t - start) / span * w, 1)
    fy = lambda v: round(h - (v - lo) / rng * h, 1)
    return {"w": w, "h": h, "lo": round(lo, 2), "hi": round(hi, 2),
            "max": " ".join(f"{fx(t)},{fy(a)}" for t, a, _ in rows),
            "min": " ".join(f"{fx(t)},{fy(b)}" for t, _, b in rows)}


@app.get("/assessments/{aid}", response_class=HTMLResponse)
async def report(request: Request, aid: int):
    a = get_assessment(request, aid)
    rep = _report(a)
    mk = {m["key"]: m for m in rep["metrics"]}
    groups: dict[str, list] = {}
    for m in rep["metrics"]:
        groups.setdefault(m["group"], []).append(m)
    return templates.TemplateResponse(request, "report.html", {
        "a": a, "rep": rep, "mk": mk, "groups": groups, "valid_until": a["ends_at"] + 30 * 86400,
        "bricks": _brick_chart(rep.get("bricks") or [], a["started_at"], a["ends_at"]), "chart": _chart(rep["samples"], a["started_at"], a["ends_at"]),
        "now": int(time.time())})


@app.get("/assessments/{aid}/report.json")
async def report_json(request: Request, aid: int):
    a = get_assessment(request, aid)
    return JSONResponse({"id": a["id"], "vin": a["vin"], "status": a["status"],
                         "started_at": a["started_at"], "ends_at": a["ends_at"], **_report(a)})


@app.post("/assessments/{aid}/finish")
async def finish(request: Request, aid: int):
    """End the window now and freeze the report."""
    a = get_assessment(request, aid)
    if a["status"] == "running":
        with db.conn() as c:
            c.execute("UPDATE assessments SET ends_at=? WHERE id=?", (int(time.time()), aid))
    return RedirectResponse(f"/assessments/{aid}", 303)


# ---- machine endpoints ---------------------------------------------------
@app.post("/ingest/telemetry")
async def ingest(request: Request):
    """Accepts Fleet Telemetry records, one object or a list.

    Flat:     {"vin": "...", "ts": 1764230400, "data": {"BrickVoltageMax": 4.01, ...}}
    Decoded:  {"vin": "...", "createdAt": "2026-11-27T08:00:00Z",
               "data": [{"key": "BrickVoltageMax", "value": {"doubleValue": 4.01}}, ...]}
    """
    if not INGEST_TOKEN or not hmac.compare_digest(request.headers.get("authorization", ""), f"Bearer {INGEST_TOKEN}"):
        raise HTTPException(401)
    body = await request.json()
    n = 0
    for rec in body if isinstance(body, list) else [body]:
        vin, data = rec.get("vin"), rec.get("data")
        if not vin or not data:
            continue
        if isinstance(data, list):
            data = {d.get("key"): d.get("value") for d in data if d.get("key")}
        ts = rec.get("ts")
        if ts is None and rec.get("createdAt"):
            ts = datetime.fromisoformat(rec["createdAt"].replace("Z", "+00:00")).timestamp()
        ts = int(ts or time.time())
        n += db.insert_telemetry(vin, ts, data)
        with db.conn() as c:
            running = c.execute("SELECT * FROM assessments WHERE vin=? AND status='running'", (vin,)).fetchall()
        for a in running:
            runlog.record(a, "telemetry", {"ts": ts, "data": data})
    return {"stored": n}


@app.get("/.well-known/appspecific/com.tesla.3p.public-key.pem")
async def public_key():
    """Tesla fetches this during partner registration and virtual key pairing."""
    try:
        with open(cfg.public_key_path) as f:
            return PlainTextResponse(f.read(), media_type="application/x-pem-file")
    except FileNotFoundError:
        raise HTTPException(404)


@app.get("/healthz")
async def healthz():
    return {"ok": True, "mock": cfg.mock}
