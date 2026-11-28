"""Background sampler: one asyncio task polls every running assessment."""
import asyncio
import hashlib
import hmac
import json
import logging
import time

import httpx

from . import db, metrics, runlog, soh, vin as vindec
from .config import cfg
from .tesla import AuthExpired, VehicleUnavailable, parse_alerts

log = logging.getLogger("batteryhealth.worker")


def load_samples(assessment_id: int) -> list[soh.Sample]:
    with db.conn() as c:
        rows = c.execute("SELECT * FROM samples WHERE assessment_id=? ORDER BY ts", (assessment_id,)).fetchall()
    keys = soh.Sample.__dataclass_fields__.keys()
    return [soh.Sample(**{k: r[k] for k in keys}) for r in rows]


def report_id(a) -> str:
    """Stable, non-guessable reference printed on the report."""
    h = hmac.new(cfg.secret_key.encode(), f"report:{a['id']}:{a['vin']}".encode(), hashlib.sha256).hexdigest()[:12].upper()
    return f"{h[:4]} {h[4:8]} {h[8:]}"


def build_report(a) -> dict:
    """Compute the full report for an assessment row. Safe to call while it is still running."""
    samples = load_samples(a["id"])
    res = soh.compute(samples, a["ref_range_km"], a["ref_capacity_kwh"])
    tele = db.load_telemetry(a["vin"], a["started_at"], a["ends_at"])
    with db.conn() as c:
        alerts = [dict(r) for r in c.execute("SELECT * FROM alerts WHERE assessment_id=? ORDER BY ts DESC", (a["id"],))]
        cfg_row = c.execute("SELECT car_type, car_version, trim, wheel_type FROM samples WHERE assessment_id=? "
                            "AND car_type IS NOT NULL ORDER BY ts DESC LIMIT 1", (a["id"],)).fetchone()
    mets, summary = metrics.build(res, tele, alerts, a["ref_range_km"], a["ref_capacity_kwh"])
    vehicle = vindec.decode(a["vin"]) | ({k: cfg_row[k] for k in cfg_row.keys()} if cfg_row else {})
    return {"report_id": report_id(a), "vehicle": vehicle,
            "estimate": res.to_dict(), "summary": summary, "metrics": [x.to_dict() for x in mets],
            "energy": metrics.energy(tele), "bricks": metrics.brick_series(tele),
            "alerts": [{"name": x["name"], "time": x["ts"], "audience": x["audience"], "text": x["user_text"],
                        "bms": str(x["name"]).upper().startswith("BMS")} for x in alerts],
            "samples": [{"ts": s.ts, "state": s.state, "battery_level": s.battery_level,
                         "charging_state": s.charging_state} for s in samples]}


def _store(aid: int, state: str, d: dict | None = None) -> None:
    cs, vs = (d or {}).get("charge_state") or {}, (d or {}).get("vehicle_state") or {}
    vc = (d or {}).get("vehicle_config") or {}
    with db.conn() as c:
        c.execute(
            """INSERT INTO samples(assessment_id, ts, state, battery_level, usable_battery_level, battery_range_mi,
               charge_energy_added, charging_state, charger_power, odometer_mi, outside_temp, car_type, car_version, trim, wheel_type)
               VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)""",
            (aid, int(time.time()), state, cs.get("battery_level"), cs.get("usable_battery_level"),
             cs.get("battery_range"), cs.get("charge_energy_added"), cs.get("charging_state"),
             cs.get("charger_power"), vs.get("odometer"), ((d or {}).get("climate_state") or {}).get("outside_temp"),
             vc.get("car_type"), vs.get("car_version"), vc.get("trim_badging"), vc.get("wheel_type")),
        )


def alert_row(al: dict) -> dict:
    return {"ts": al.get("time"), "name": al.get("name", "?"), "audience": ",".join(al.get("audience") or []),
            "user_text": al.get("user_text")}


async def fetch_alerts(client, a) -> None:
    try:
        raw = await client.recent_alerts(a["account_id"], a["vin"])
        runlog.record(a, "alerts", raw)
        rows = [alert_row(al) for al in parse_alerts(raw)]
    except Exception as e:  # not worth failing an assessment over
        log.info("alerts unavailable for %s: %s", a["vin"], e)
        runlog.record(a, "alerts_error", str(e)[:300])
        return
    with db.conn() as c:
        for r in rows:
            c.execute("INSERT OR IGNORE INTO alerts(assessment_id, ts, name, audience, user_text) VALUES (?,?,?,?,?)",
                      (a["id"], r["ts"], r["name"], r["audience"], r["user_text"]))


def finalize(a) -> None:
    """Build the report and store it as the assessment's result. Also used to rebuild it for late telemetry."""
    rep = build_report(a)
    with db.conn() as c:
        c.execute("UPDATE assessments SET status='done', result_json=? WHERE id=?", (json.dumps(rep), a["id"]))
    runlog.record(a, "done", {"ends_at": a["ends_at"], **rep["summary"], "confidence": rep["estimate"]["confidence"]})


async def poll_one(client, a) -> None:
    now = int(time.time())
    if now >= a["ends_at"]:
        await fetch_alerts(client, a)
        finalize(a)
        return

    with db.conn() as c:
        last = c.execute("SELECT ts, charging_state, state FROM samples WHERE assessment_id=? ORDER BY ts DESC LIMIT 1",
                         (a["id"],)).fetchone()
    charging = bool(last and last["charging_state"] == "Charging")
    interval = cfg.poll_seconds if charging else cfg.idle_poll_seconds
    if last and now - last["ts"] < interval:
        return

    try:
        v = await client.vehicle(a["account_id"], a["vin"])       # no call to the car
        runlog.record(a, "vehicle", v)
        if v.get("state") != "online":
            _store(a["id"], v.get("state") or "offline")           # never wake a sleeping car from the poller
            return
        d = await client.vehicle_data(a["account_id"], a["vin"])
        runlog.record(a, "vehicle_data", d)
        _store(a["id"], "online", d)
        if cfg.mock:
            tele = client.telemetry(a["vin"])
            db.insert_telemetry(a["vin"], int(time.time()), tele)
            runlog.record(a, "telemetry", {"ts": int(time.time()), "data": tele})
    except VehicleUnavailable as e:
        runlog.record(a, "unavailable", str(e)[:300])
        _store(a["id"], "asleep")
    except httpx.HTTPError as e:
        # Tesla 5xx or a network error. Store the attempt so the next one waits for the normal
        # interval; without a sample the loop would retry the billable call every tick.
        runlog.record(a, "error", str(e)[:300])
        _store(a["id"], "offline")
    except AuthExpired as e:
        with db.conn() as c:
            c.execute("UPDATE assessments SET status='error', note=? WHERE id=?",
                      (f"Tesla authorisation expired; owner must reconnect. ({e})"[:300], a["id"]))


async def run(client) -> None:
    tick = max(1, min(cfg.poll_seconds, cfg.idle_poll_seconds, 60))
    while True:
        try:
            with db.conn() as c:
                running = c.execute("SELECT * FROM assessments WHERE status='running'").fetchall()
            for a in running:
                try:
                    await poll_one(client, a)
                except Exception:
                    log.exception("poll failed for assessment %s", a["id"])
        except Exception:
            log.exception("worker loop error")
        await asyncio.sleep(tick)
