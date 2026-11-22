#!/usr/bin/env python3
"""Small local administration CLI for D2; no third terminal or HTTP needed.

The Mininet process and controller are on the same Ubuntu VM and share the
project directory. A revisioned, atomically replaced JSON file is the
controller's local management interface (NOT the data plane). The controller
reports observed status via a second JSON file.

    python3 sdn_admin.py adaptive
    python3 sdn_admin.py static
    python3 sdn_admin.py status
"""
import argparse
import json
import os
import tempfile
import time

ROOT = os.path.dirname(os.path.abspath(__file__))
RUNTIME = os.path.join(ROOT, "runtime")
MODE_FILE = os.path.join(RUNTIME, "policy.json")
STATUS_FILE = os.path.join(RUNTIME, "controller_status.json")
MODES = ("adaptive", "static")


def atomic_json(path, data):
    os.makedirs(os.path.dirname(path), exist_ok=True)
    fd, tmp = tempfile.mkstemp(prefix=".update-", dir=os.path.dirname(path))
    try:
        with os.fdopen(fd, "w") as out:
            json.dump(data, out, indent=2)
            out.write("\n")
        os.replace(tmp, path)
    finally:
        if os.path.exists(tmp):
            os.unlink(tmp)


def set_mode(mode):
    if mode not in MODES:
        raise ValueError("mode must be adaptive or static")
    revision = time.time_ns()
    atomic_json(MODE_FILE, {"mode": mode, "revision": revision,
                            "updated_at": time.time()})
    return revision


def status():
    try:
        with open(STATUS_FILE) as f:
            return json.load(f)
    except (OSError, json.JSONDecodeError):
        return None


def wait_for_mode(mode, revision, timeout=8):
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        s = status()
        if (s and s.get("mode") == mode and s.get("revision") == revision
                and time.time() - s.get("timestamp", 0) < 4):
            return True
        time.sleep(0.15)
    return False


def main():
    ap = argparse.ArgumentParser(description="Inspect/change SDN selection policy")
    ap.add_argument("action", choices=("adaptive", "static", "status"))
    a = ap.parse_args()
    if a.action == "status":
        s = status()
        if s is None:
            print("No controller status yet. Start the controller first.")
            raise SystemExit(1)
        print(json.dumps(s, indent=2))
        if time.time() - s.get("timestamp", 0) > 5:
            print("WARNING: status is stale; controller may not be running")
            raise SystemExit(1)
    else:
        revision = set_mode(a.action)
        if wait_for_mode(a.action, revision):
            print("Controller confirmed policy=%s" % a.action)
        else:
            print("Policy file updated, but controller has not acknowledged it."
                  " Is the controller running?")
            raise SystemExit(1)


if __name__ == "__main__":
    main()
