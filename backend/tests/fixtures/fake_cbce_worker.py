"""Standalone fake crawler worker used by process-ownership tests."""

from __future__ import annotations

import json
import subprocess
import sys
import time
from datetime import UTC, datetime

VERSION = "cbce.worker.v1"


def emit(kind: str, sequence: int, identity: dict[str, object] | None = None, **payload: object) -> None:
    identity = identity or {}
    message = {
        "protocol_version": VERSION,
        "kind": kind,
        "sequence": sequence,
        "run_id": identity.get("run_id"),
        "source_run_id": identity.get("source_run_id"),
        "source_id": identity.get("source_id"),
        "provider_id": identity.get("provider_id"),
        "operation": identity.get("operation"),
        "timestamp": datetime.now(UTC).isoformat().replace("+00:00", "Z"),
        "payload": payload,
    }
    sys.stdout.write(json.dumps(message, separators=(",", ":")) + "\n")
    sys.stdout.flush()


emit("ready", 0)
start = json.loads(sys.stdin.readline())
identity = {
    key: start[key]
    for key in ("run_id", "source_run_id", "source_id", "provider_id", "operation")
}
scenario = start.get("payload", {}).get("scenario", "success")

if scenario == "invalid_stdout":
    sys.stdout.write("this is not json\n")
    sys.stdout.flush()
    time.sleep(60)
elif scenario == "success":
    sys.stderr.write(
        "Authorization: Bearer abcdefghijklmnop\n"
        "GET https://user:pass@example.test/data?access_token=worker-secret\n"
    )
    sys.stderr.flush()
    emit("run_started", 1, identity)
    emit("heartbeat", 2, identity)
    emit("item", 3, identity, external_id="BV1", title="safe")
    emit("complete", 4, identity, items=1)
elif scenario == "duplicate":
    emit("run_started", 1, identity)
    emit("heartbeat", 1, identity)
    emit("complete", 2, identity, items=0)
elif scenario == "error":
    sys.stderr.write("Cookie: session=worker-secret\n")
    sys.stderr.flush()
    emit("error", 1, identity, code="TRANSPORT", message="safe failure")
elif scenario == "wait_cancel":
    emit("run_started", 1, identity)
    control = json.loads(sys.stdin.readline())
    terminal = "cancelled" if control.get("kind") == "cancel" else "complete"
    emit(terminal, 2, identity)
elif scenario == "ignore_cancel_with_child":
    child = subprocess.Popen([sys.executable, "-c", "import time; time.sleep(60)"])
    emit("run_started", 1, identity)
    emit("item", 2, identity, external_id="child", child_pid=child.pid)
    time.sleep(60)
else:
    emit("error", 1, identity, code="BAD_SCENARIO", message="unknown scenario")
