"""Configuration, all from environment variables (prefix BH_)."""
import os
from dataclasses import dataclass, field


def _env(name: str, default: str = "") -> str:
    return os.environ.get(f"BH_{name}", default)


@dataclass(frozen=True)
class Config:
    mock: bool = field(default_factory=lambda: _env("MOCK", "0") == "1")
    db_path: str = field(default_factory=lambda: _env("DB_PATH", "batteryhealth.db"))
    secret_key: str = field(default_factory=lambda: _env("SECRET_KEY", "dev-only-change-me"))
    basic_auth: str = field(default_factory=lambda: _env("BASIC_AUTH"))  # "user:pass" or empty

    # Tesla developer application (developer.tesla.com)
    client_id: str = field(default_factory=lambda: _env("TESLA_CLIENT_ID"))
    client_secret: str = field(default_factory=lambda: _env("TESLA_CLIENT_SECRET"))
    redirect_uri: str = field(default_factory=lambda: _env("TESLA_REDIRECT_URI", "http://localhost:8000/auth/callback"))
    scopes: str = field(default_factory=lambda: _env("TESLA_SCOPES", "openid offline_access vehicle_device_data"))
    # Used as OAuth audience and as fallback base URL if /users/region fails.
    fleet_base: str = field(default_factory=lambda: _env("TESLA_FLEET_BASE", "https://fleet-api.prd.eu.vn.cloud.tesla.com"))
    authorize_url: str = "https://auth.tesla.com/oauth2/v3/authorize"
    token_url: str = "https://fleet-auth.prd.vn.cloud.tesla.com/oauth2/v3/token"
    public_key_path: str = field(default_factory=lambda: _env("PUBLIC_KEY_PATH", "keys/public-key.pem"))

    # Assessment behaviour
    window_seconds: int = field(default_factory=lambda: int(_env("WINDOW_SECONDS", str(24 * 3600))))
    poll_seconds: int = field(default_factory=lambda: int(_env("POLL_SECONDS", "900")))       # while charging
    idle_poll_seconds: int = field(default_factory=lambda: int(_env("IDLE_POLL_SECONDS", "1800")))  # while not charging
    # Raw answers from the car, one file per assessment (app/runlog.py). Empty disables.
    run_log_dir: str = field(default_factory=lambda: _env("RUN_LOG_DIR", "runs"))
    allow_wake: bool = field(default_factory=lambda: _env("ALLOW_WAKE", "0") == "1")


cfg = Config()
