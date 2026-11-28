"""Configuration, all from environment variables (prefix BH_)."""
import os
from dataclasses import dataclass, field


def _env(name: str, default: str = "") -> str:
    return os.environ.get(f"BH_{name}", default)


DEV_SECRET_KEY = "dev-only-change-me"   # public; only accepted in mock mode


@dataclass(frozen=True)
class Config:
    mock: bool = field(default_factory=lambda: _env("MOCK", "0") == "1")
    db_path: str = field(default_factory=lambda: _env("DB_PATH", "batteryhealth.db"))
    secret_key: str = field(default_factory=lambda: _env("SECRET_KEY", DEV_SECRET_KEY))
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
    # Fleet Telemetry buffered in the car can arrive after the window closed; within this long
    # after the end, records timestamped inside the window rebuild the finished report.
    late_telemetry_seconds: int = field(default_factory=lambda: int(_env("LATE_TELEMETRY_SECONDS", str(6 * 3600))))
    # Fleet Telemetry (optional). The app reads records from fleet-telemetry's ZMQ publisher and
    # sends the car its streaming configuration through Tesla's vehicle-command proxy.
    telemetry_zmq: str = field(default_factory=lambda: _env("TELEMETRY_ZMQ"))                # tcp://fleet-telemetry:5284
    telemetry_hostname: str = field(default_factory=lambda: _env("TELEMETRY_HOSTNAME"))      # where the car connects
    telemetry_port: int = field(default_factory=lambda: int(_env("TELEMETRY_PORT", "443")))
    telemetry_ca_path: str = field(default_factory=lambda: _env("TELEMETRY_CA_PATH"))        # chain the car checks the server against
    command_proxy_url: str = field(default_factory=lambda: _env("COMMAND_PROXY_URL"))        # https://vehicle-command:4443
    command_proxy_ca: str = field(default_factory=lambda: _env("COMMAND_PROXY_CA"))          # the proxy's own TLS certificate
    allow_wake: bool = field(default_factory=lambda: _env("ALLOW_WAKE", "0") == "1")


cfg = Config()
