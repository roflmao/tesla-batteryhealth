"""Recompute a report from a run log (see app/runlog.py), without Tesla or the database.

  python -m app.replay runs/20261127T213300Z-5YJ3...-1.jsonl
  python -m app.replay runs/<file>.jsonl --scrub tests/fixtures/model3-2022.jsonl

--scrub writes a copy that is safe to commit: the VIN's serial number is zeroed
and account identifiers, tokens and the car's name are removed.
"""
import json
import sys

from . import db, metrics, soh, tesla, worker

SCRUB_KEYS = {"id", "id_s", "user_id", "vehicle_id", "tokens", "vehicle_name", "display_name", "name_s"}


def load(path: str) -> list[dict]:
    with open(path) as f:
        return [json.loads(line) for line in f if line.strip()]


def replay(path: str) -> dict:
    start, recorded = {}, None
    samples: list[soh.Sample] = []
    tele: dict[str, list] = {}
    alerts: list[dict] = []
    for ev in load(path):
        t, kind, d = ev["t"], ev["kind"], ev.get("data")
        if kind == "start":
            start = d
        elif kind == "vehicle" and d.get("state") != "online":
            samples.append(soh.Sample(ts=t, state=d.get("state") or "offline"))
        elif kind == "unavailable":
            samples.append(soh.Sample(ts=t, state="asleep"))
        elif kind == "error":
            samples.append(soh.Sample(ts=t, state="offline"))
        elif kind == "vehicle_data":
            samples.append(soh.Sample.from_vehicle_data(t, d))
        elif kind == "telemetry":
            for k, v in db.normalize_telemetry(d["data"]).items():
                tele.setdefault(k, []).append((int(d["ts"]), v))
        elif kind == "alerts":
            alerts.extend(map(worker.alert_row, tesla.parse_alerts(d)))
        elif kind == "done":
            recorded = d
    lo, hi = start.get("started_at", 0), (recorded or {}).get("ends_at", float("inf"))
    tele = {k: sorted(x for x in v if lo <= x[0] <= hi) for k, v in tele.items()}
    tele = {k: v for k, v in tele.items() if v}
    alert_rows = worker.clean_alerts(alerts)
    ref_range, ref_cap = start.get("ref_range_km"), start.get("ref_capacity_kwh")
    if not ref_range:
        raise ValueError(f"{path}: no 'start' event with ref_range_km")
    res = soh.compute(samples, ref_range, ref_cap)
    mets, summary = metrics.build(res, tele, alert_rows, ref_range, ref_cap)
    return {"start": start, "estimate": res.to_dict(), "summary": summary,
            "metrics": [m.to_dict() for m in mets], "energy": metrics.energy(tele),
            "bricks": metrics.brick_series(tele), "recorded": recorded}


def _strip(x):
    if isinstance(x, dict):
        return {k: _strip(v) for k, v in x.items() if k not in SCRUB_KEYS}
    return [_strip(v) for v in x] if isinstance(x, list) else x


def scrub(src: str, dst: str) -> None:
    events = load(src)
    vin = next((e["data"].get("vin") for e in events if e["kind"] == "start"), None)
    with open(dst, "w") as f:
        for ev in events:
            line = json.dumps(_strip(ev))
            if vin and len(vin) == 17:
                line = line.replace(vin, vin[:11] + "000000")
            f.write(line + "\n")


def main(argv: list[str]) -> None:
    if len(argv) not in (1, 3) or (len(argv) == 3 and argv[1] != "--scrub"):
        sys.exit(__doc__)
    if len(argv) == 3:
        scrub(argv[0], argv[2])
        print(f"wrote {argv[2]}")
        return
    rep = replay(argv[0])
    e = rep["estimate"]
    print(f"samples: {e['samples_online']} online of {e['samples_total']}, confidence {e['confidence']}")
    for m in rep["metrics"]:
        print(f"  {m['label']:<28} {m['value']:<26} {m['status']}")
    for n in e["notes"]:
        print(f"  note: {n}")
    if rep["recorded"]:
        print(f"live run reported SoH {rep['recorded'].get('soh_pct')} %, replay gives {rep['summary']['soh_pct']} %")


if __name__ == "__main__":
    main(sys.argv[1:])
