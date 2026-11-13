"""HTTP health endpoint for the isolated station fixture."""
from __future__ import annotations

import json
import os
import shutil
import sys
import threading
import time
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path


ROOT = Path(sys.argv[1]).resolve()
DATA = ROOT / "data"
PANEL = DATA / "yapanel"
STATE = DATA / "yapanel-state"
MODE = os.environ.get("YA_STATION_HEALTH", "healthy")
REPO = Path(os.environ.get("YA_REPO_ROOT", Path.cwd())).resolve()


def _payload(path):
    version = (PANEL / "VERSION").read_text(encoding="utf-8").strip()
    if path == "/api/panel/health":
        if MODE == "failed":
            return 200, {"ready": False, "version": version, "critical_errors": 1}
        if MODE == "unavailable":
            return 503, {"ready": False, "version": version, "critical_errors": 1}
        if version != "0.3.5":
            try:
                sys.path.insert(0, str(REPO))
                from update_agent import UpdateAgent

                agent = UpdateAgent(PANEL, state_dir=STATE)
                for journal_path in (STATE / "updater" / "transactions").glob("*/transaction.json"):
                    journal = json.loads(journal_path.read_text(encoding="utf-8"))
                    if journal.get("state") == "activated_pending_health":
                        agent.mark_healthy(journal["id"])
                        break
            except (OSError, ValueError, KeyError):
                pass
        return 200, {"ready": True, "version": version, "critical_errors": 0}
    if path == "/api/panel/version":
        return 200, {"version": version}
    if path == "/api/status":
        return 200, {"version": version, "initialized": True}
    return 404, {"error": "not found"}


def _supervisor_recovery():
    if os.environ.get("YA_STATION_SCENARIO") not in {"failed_health", "power_loss_before_handoff", "apply_error_after_activation"}:
        return
    while True:
        try:
            state = json.loads((ROOT / "emulator-state.json").read_text(encoding="utf-8"))
        except (OSError, ValueError):
            return
        if state.get("handoff_attempted"):
            try:
                sys.path.insert(0, str(REPO))
                from update_agent import UpdateAgent

                agent = UpdateAgent(PANEL, state_dir=STATE)
                for journal_path in (STATE / "updater" / "transactions").glob("*/transaction.json"):
                    journal = json.loads(journal_path.read_text(encoding="utf-8"))
                    if journal.get("state") in {"activated_pending_health", "activating", "rollback_started"}:
                        agent.rollback(journal["id"])
                        wrapper = STATE / "updater" / "start.sh.before-migration"
                        if wrapper.exists():
                            shutil.copy2(wrapper, PANEL / "start.sh")
                        status = STATE / "updater" / "status.json"
                        status.write_text(json.dumps({"state": "rolled_back", "transaction": journal["id"], "version": "0.3.5"}), encoding="utf-8")
                        return
            except (OSError, ValueError, KeyError):
                return
        time.sleep(0.1)


class Handler(BaseHTTPRequestHandler):
    def do_GET(self):
        status, payload = _payload(self.path)
        body = json.dumps(payload).encode("utf-8")
        self.send_response(status)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def log_message(self, *_args):
        return


server = ThreadingHTTPServer(("127.0.0.1", int(sys.argv[2])), Handler)
threading.Thread(target=_supervisor_recovery, daemon=True).start()
try:
    server.serve_forever()
finally:
    server.server_close()
