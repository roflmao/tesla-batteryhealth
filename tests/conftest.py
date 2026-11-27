"""Point the app at the simulator and a throwaway directory before anything imports app.config."""
import os
import tempfile

_tmp = tempfile.mkdtemp(prefix="batteryhealth-test-")
os.environ.update(BH_MOCK="1", BH_DB_PATH=os.path.join(_tmp, "test.db"), BH_RUN_LOG_DIR=os.path.join(_tmp, "runs"))
