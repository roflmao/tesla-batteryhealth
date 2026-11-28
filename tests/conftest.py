"""Point the app at the simulator and a throwaway directory before anything imports app.config."""
import os
import tempfile

_tmp = tempfile.mkdtemp(prefix="batteryhealth-test-")
os.environ.update(BH_MOCK="1", BH_INGEST_TOKEN="test-token", BH_TELEMETRY_ZMQ="tcp://127.0.0.1:35284",
                  BH_TELEMETRY_HOSTNAME="telemetry.example.invalid", BH_TELEMETRY_PORT="4443",
                  BH_COMMAND_PROXY_URL="https://vehicle-command.invalid:4443", BH_DB_PATH=os.path.join(_tmp, "test.db"), BH_RUN_LOG_DIR=os.path.join(_tmp, "runs"))
