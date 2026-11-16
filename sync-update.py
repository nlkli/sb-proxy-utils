#!/usr/bin/env python3
import json
import os
import subprocess
import logging
from datetime import datetime, timezone
import time

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(message)s",
)

FILE = "sync-time.json"
CONFIG_UPDATE_TIMEOUT = 60 * 60 * 12   # seconds
SELECT_TIMEOUT = 60 * 11          # seconds

CONFIG_UPDATE_CMD = "sudo ./update-config.py && ./write-config.sh && ./restart-sb.sh"
SELECT_CMD = "./best-selector.py"

DEFAULTS = {
    "last_config_update": datetime.now(timezone.utc).isoformat(),
    "last_select": datetime.now(timezone.utc).isoformat(),
}


def load():
    if not os.path.exists(FILE):
        logging.info("Config file not found, creating with defaults")
        with open(FILE, "w") as f:
            json.dump(DEFAULTS, f, indent=2)
        return dict(DEFAULTS), False
    with open(FILE) as f:
        return json.load(f), True


def save(data):
    with open(FILE, "w") as f:
        json.dump(data, f, indent=2)


def elapsed(iso_ts):
    then = datetime.fromisoformat(iso_ts)
    if then.tzinfo is None:
        then = then.replace(tzinfo=timezone.utc)
    return (datetime.now(timezone.utc) - then).total_seconds()


def run(cmd):
    logging.info("Running: %s", cmd)
    try:
        subprocess.run(cmd, shell=True, check=True)
        logging.info("OK: %s", cmd)
        return True
    except subprocess.CalledProcessError as e:
        logging.error("Command failed (exit %s): %s", e.returncode, cmd)
    except Exception as e:
        logging.error("Unexpected error running '%s': %s", cmd, e)
    return False


def run_once():
    try:
        data, exists = load()
    except Exception as e:
        logging.error("Failed to load config: %s", e)
        return

    try:
        if not exists or elapsed(data["last_config_update"]) > CONFIG_UPDATE_TIMEOUT:
            if run(CONFIG_UPDATE_CMD):
                data["last_config_update"] = datetime.now(timezone.utc).isoformat()
            else:
                logging.warning("Config update failed, timestamp not updated")

        if not exists or elapsed(data["last_select"]) > SELECT_TIMEOUT:
            if run(SELECT_CMD):
                data["last_select"] = datetime.now(timezone.utc).isoformat()
            else:
                logging.warning("Select failed, timestamp not updated")

        save(data)
    except Exception as e:
        logging.error("Unexpected error: %s", e)

def main():
    while True:
        run_once()
        time.sleep(120)

if __name__ == "__main__":
    main()
