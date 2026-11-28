"""Fleet Telemetry: storing records, the configuration sent to the car, and the live subscriber.

Records reach the app either over HTTP (POST /ingest/telemetry) or, with
BH_TELEMETRY_ZMQ set, straight from Tesla's fleet-telemetry server, which
publishes each record on a ZMQ socket as [topic, JSON payload].
"""
import asyncio
import json
import logging
import os
import time
from datetime import datetime

from . import db, runlog, worker
from .config import cfg

log = logging.getLogger("batteryhealth.telemetry")
FIELDS_FILE = os.path.join(os.path.dirname(os.path.dirname(__file__)), "scripts", "telemetry_fields.json")


def record_time(rec: dict) -> int:
    """Unix time of a record: "ts", or fleet-telemetry's "createdAt" (RFC 3339, up to nanoseconds)."""
    if rec.get("ts") is not None:
        return int(rec["ts"])
    created = rec.get("createdAt")
    if created:
        text = str(created).replace("Z", "+00:00")
        head, dot, rest = text.partition(".")
        if dot:                                    # cut the fraction to the six digits Python accepts
            digits = len(rest) - len(rest.lstrip("0123456789"))
            text = f"{head}.{rest[:min(digits, 6)]}{rest[digits:]}"
        try:
            return int(datetime.fromisoformat(text).timestamp())
        except ValueError:
            pass
    return int(time.time())


def store(records: list) -> dict:
    """Store telemetry records that belong to an assessment and rebuild reports that late data changes.

    A record is kept when its car has a running assessment, or a finished one
    whose window contains the record and closed less than
    BH_LATE_TELEMETRY_SECONDS ago. Everything else is dropped, which also keeps
    data for cars nobody is assessing out of the database.
    """
    stored, skipped, now, late = 0, 0, int(time.time()), {}
    for rec in records:
        if not isinstance(rec, dict) or not rec.get("vin") or not rec.get("data"):
            skipped += 1
            continue
        vin, data = str(rec["vin"]), rec["data"]
        if isinstance(data, list):                 # fleet-telemetry decoded record
            data = {d.get("key"): d.get("value") for d in data if isinstance(d, dict) and d.get("key")}
        if not isinstance(data, dict):
            skipped += 1
            continue
        ts = record_time(rec)
        with db.conn() as c:
            affected = c.execute(
                "SELECT * FROM assessments WHERE vin=? AND (status='running' OR "
                "(status='done' AND ? BETWEEN started_at AND ends_at AND ends_at > ?))",
                (vin, ts, now - cfg.late_telemetry_seconds)).fetchall()
        if not affected:
            skipped += 1
            continue
        stored += db.insert_telemetry(vin, ts, data)
        for a in affected:
            runlog.record(a, "telemetry", {"ts": ts, "data": data})
            if a["status"] == "done":
                late[a["id"]] = a
    for a in late.values():                        # the car delivered buffered records after the window closed
        worker.finalize(a)
    return {"stored": stored, "skipped": skipped, "reports_rebuilt": len(late)}


def ready() -> bool:
    """Whether this deployment can send a telemetry configuration to a car."""
    return bool(cfg.telemetry_hostname and cfg.command_proxy_url)


def build_config() -> dict:
    """The configuration a car needs to start streaming to this deployment's telemetry server."""
    with open(FIELDS_FILE) as f:
        spec = json.load(f)
    config = {"hostname": cfg.telemetry_hostname, "port": cfg.telemetry_port, "fields": spec["fields"],
              "alert_types": spec.get("alert_types", ["service"]), "prefer_typed": True}
    if cfg.telemetry_ca_path:
        with open(cfg.telemetry_ca_path) as f:
            config["ca"] = f.read()
    return config


async def subscribe() -> None:
    """Receive vehicle records from fleet-telemetry's ZMQ publisher until cancelled."""
    import zmq
    import zmq.asyncio

    sock = zmq.asyncio.Context.instance().socket(zmq.SUB)
    sock.setsockopt(zmq.SUBSCRIBE, b"")
    sock.connect(cfg.telemetry_zmq)                # ZMQ reconnects on its own if the server restarts
    log.info("subscribed to fleet-telemetry at %s", cfg.telemetry_zmq)
    try:
        while True:
            parts = await sock.recv_multipart()
            if len(parts) != 2 or not parts[0].endswith(b"_V"):     # vehicle data; alerts and connectivity are other topics
                continue
            try:
                store([json.loads(parts[1])])
            except Exception:
                log.exception("could not store a telemetry record")
    except asyncio.CancelledError:
        sock.close(linger=0)
        raise
