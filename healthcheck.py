"""Liveness check for Orchard's "Command" health check type.

Configure in Orchard as a "Command" health check running:
    python healthcheck.py
"""

import os
import sys
import time

HEARTBEAT_PATH = os.environ.get("HEARTBEAT_PATH", "/tmp/bento_heartbeat")
MAX_AGE_SECONDS = int(os.environ.get("HEARTBEAT_MAX_AGE", "60"))

try:
    age = time.time() - os.path.getmtime(HEARTBEAT_PATH)
except OSError:
    print(f"heartbeat file {HEARTBEAT_PATH} missing")
    sys.exit(1)

if age > MAX_AGE_SECONDS:
    print(f"heartbeat stale: {age:.0f}s old (max {MAX_AGE_SECONDS}s)")
    sys.exit(1)

print(f"heartbeat ok: {age:.0f}s old")
sys.exit(0)
