"""SQLite storage. Tokens are encrypted at rest with a key derived from BH_SECRET_KEY."""
import base64
import hashlib
import sqlite3
import time
from contextlib import contextmanager

from cryptography.fernet import Fernet

from .config import cfg

_fernet = Fernet(base64.urlsafe_b64encode(hashlib.sha256(cfg.secret_key.encode()).digest()))

SCHEMA = """
CREATE TABLE IF NOT EXISTS accounts (
  id INTEGER PRIMARY KEY,
  tesla_sub TEXT,                           -- Tesla user id; one account row per Tesla user
  base_url TEXT NOT NULL,
  access_token BLOB NOT NULL,
  refresh_token BLOB,
  expires_at INTEGER NOT NULL,
  created_at INTEGER NOT NULL
);
CREATE TABLE IF NOT EXISTS assessments (
  id INTEGER PRIMARY KEY,
  account_id INTEGER NOT NULL REFERENCES accounts(id),
  vin TEXT NOT NULL,
  name TEXT,
  ref_range_km REAL NOT NULL,
  ref_capacity_kwh REAL,
  started_at INTEGER NOT NULL,
  ends_at INTEGER NOT NULL,
  status TEXT NOT NULL DEFAULT 'running',   -- running | done | stopped | error
  note TEXT,
  result_json TEXT
);
CREATE TABLE IF NOT EXISTS telemetry (       -- Fleet Telemetry signals, keyed by VIN
  vin TEXT NOT NULL,
  ts INTEGER NOT NULL,
  field TEXT NOT NULL,
  num REAL,
  txt TEXT
);
CREATE INDEX IF NOT EXISTS telemetry_vin ON telemetry(vin, ts);
CREATE TABLE IF NOT EXISTS alerts (
  assessment_id INTEGER NOT NULL REFERENCES assessments(id),
  ts TEXT,
  name TEXT NOT NULL,
  audience TEXT,
  user_text TEXT,
  UNIQUE(assessment_id, ts, name)
);
CREATE TABLE IF NOT EXISTS samples (
  id INTEGER PRIMARY KEY,
  assessment_id INTEGER NOT NULL REFERENCES assessments(id),
  ts INTEGER NOT NULL,
  state TEXT NOT NULL,                      -- online | asleep | offline
  battery_level REAL,
  usable_battery_level REAL,
  battery_range_mi REAL,
  charge_energy_added REAL,
  charging_state TEXT,
  charger_power REAL,
  odometer_mi REAL,
  outside_temp REAL,
  car_type TEXT,
  car_version TEXT,
  trim TEXT,
  wheel_type TEXT
);
CREATE INDEX IF NOT EXISTS samples_assessment ON samples(assessment_id, ts);
"""


@contextmanager
def conn():
    c = sqlite3.connect(cfg.db_path, timeout=10)
    c.row_factory = sqlite3.Row
    c.execute("PRAGMA journal_mode=WAL")
    c.execute("PRAGMA foreign_keys=ON")
    try:
        yield c
        c.commit()
    finally:
        c.close()


def init() -> None:
    with conn() as c:
        c.executescript(SCHEMA)
        if "tesla_sub" not in {r["name"] for r in c.execute("PRAGMA table_info(accounts)")}:
            c.execute("ALTER TABLE accounts ADD COLUMN tesla_sub TEXT")     # databases from before the column
        c.execute("CREATE UNIQUE INDEX IF NOT EXISTS accounts_sub ON accounts(tesla_sub) WHERE tesla_sub IS NOT NULL")


def enc(s: str | None) -> bytes | None:
    return _fernet.encrypt(s.encode()) if s else None


def dec(b: bytes | None) -> str | None:
    return _fernet.decrypt(b).decode() if b else None


def save_tokens(account_id: int | None, base_url: str, tok: dict, tesla_sub: str | None = None) -> int:
    """Store tokens. account_id=None is a sign-in: the Tesla user's existing account is reused if there is one."""
    expires_at = int(time.time()) + int(tok.get("expires_in", 3600))
    with conn() as c:
        if account_id is None and tesla_sub:
            known = c.execute("SELECT id FROM accounts WHERE tesla_sub=?", (tesla_sub,)).fetchone()
            if known:
                account_id = known["id"]
                c.execute("UPDATE accounts SET base_url=? WHERE id=?", (base_url, account_id))
        if account_id is None:
            cur = c.execute(
                "INSERT INTO accounts(tesla_sub, base_url, access_token, refresh_token, expires_at, created_at) VALUES (?,?,?,?,?,?)",
                (tesla_sub, base_url, enc(tok["access_token"]), enc(tok.get("refresh_token")), expires_at, int(time.time())),
            )
            return cur.lastrowid
        c.execute(
            "UPDATE accounts SET access_token=?, refresh_token=COALESCE(?, refresh_token), expires_at=? WHERE id=?",
            (enc(tok["access_token"]), enc(tok.get("refresh_token")), expires_at, account_id),
        )
        return account_id


def normalize_telemetry(data: dict) -> dict[str, float | str]:
    """Telemetry values as stored: a number where the value parses as one, else text."""
    out: dict[str, float | str] = {}
    for k, v in data.items():
        if isinstance(v, dict):            # fleet-telemetry decoded record: {"doubleValue": 1.2}
            if "invalid" in v:             # {"invalid": true}: the car could not measure the signal
                continue
            v = next(iter(v.values()), None)
        if v is None or isinstance(v, (dict, list)):
            continue
        try:
            out[k] = float(v)
        except (TypeError, ValueError):
            out[k] = str(v)
    return out


def insert_telemetry(vin: str, ts: int, data: dict) -> int:
    rows = [(vin, ts, k, v if isinstance(v, float) else None, None if isinstance(v, float) else v)
            for k, v in normalize_telemetry(data).items()]
    with conn() as c:
        c.executemany("INSERT INTO telemetry(vin, ts, field, num, txt) VALUES (?,?,?,?,?)", rows)
    return len(rows)


def load_telemetry(vin: str, start: int, end: int) -> dict[str, list[tuple[int, float | str]]]:
    out: dict[str, list] = {}
    with conn() as c:
        for r in c.execute("SELECT ts, field, num, txt FROM telemetry WHERE vin=? AND ts BETWEEN ? AND ? ORDER BY ts",
                           (vin, start, end)):
            out.setdefault(r["field"], []).append((r["ts"], r["num"] if r["num"] is not None else r["txt"]))
    return out
