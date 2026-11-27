"""Run log: every raw answer from the car during an assessment, one JSON object per line.

One file per assessment in BH_RUN_LOG_DIR. The files are the input for
app/replay.py and, once scrubbed, for the fixtures in tests/fixtures.
Line format: {"t": <unix time>, "kind": <see below>, "data": ...}

  start         reference values and window entered for the assessment
  vehicle       answer from GET /vehicles/{vin} (connectivity check)
  vehicle_data  answer from GET /vehicles/{vin}/vehicle_data
  unavailable   the car did not answer (asleep, offline or rate limited)
  alerts        answer from GET /vehicles/{vin}/recent_alerts
  alerts_error  why the alerts could not be fetched
  telemetry     one Fleet Telemetry record: {"ts": ..., "data": {field: value}}
  done          what the live run concluded, for comparison when replaying
"""
import json
import logging
import os
import time
from datetime import datetime, timezone

from .config import cfg

log = logging.getLogger("batteryhealth.runlog")


def path_for(a) -> str:
    stamp = datetime.fromtimestamp(a["started_at"], timezone.utc).strftime("%Y%m%dT%H%M%SZ")
    return os.path.join(cfg.run_log_dir, f"{stamp}-{a['vin']}-{a['id']}.jsonl")


def record(a, kind: str, data) -> None:
    """Append one event. Never raises: a full disk must not stop an assessment."""
    if not cfg.run_log_dir:
        return
    try:
        os.makedirs(cfg.run_log_dir, exist_ok=True)
        with open(path_for(a), "a") as f:
            f.write(json.dumps({"t": int(time.time()), "kind": kind, "data": data}, default=str) + "\n")
    except OSError as e:
        log.warning("could not write run log for assessment %s: %s", a["id"], e)
