#!/usr/bin/env python3
"""
watchdog.py — external liveness check for the star-trajectory engine.

Periodic agents are memoryless and prompts are wishes, so the engine is verified
by its deliverable: this reads status.json (written by run.py every cycle) and
alerts if the engine is stale (hasn't cycled within the window) or the last cycle
recorded errors. Run it from cron a few minutes after each engine cycle.

The alert webhook is read from $STAR_TRAJECTORY_WEBHOOK, or from the file named by
$STAR_TRAJECTORY_WEBHOOK_FILE — never hardcoded, so the secret stays out of source.
"""

import json
import os
import sys
import urllib.request
from datetime import datetime, timezone

HERE = os.path.dirname(os.path.abspath(__file__))
STATUS = os.path.join(HERE, "status.json")
# Hourly score cycle -> consider stale if no successful cycle within ~2.5h.
MAX_AGE_MIN = int(os.environ.get("STAR_TRAJECTORY_MAX_AGE_MIN", "150"))


def webhook_url():
    u = os.environ.get("STAR_TRAJECTORY_WEBHOOK")
    if u:
        return u.strip()
    f = os.environ.get("STAR_TRAJECTORY_WEBHOOK_FILE")
    if f and os.path.exists(f):
        with open(f, "r", encoding="utf-8") as fh:
            return fh.read().strip()
    return None


def alert(msg):
    print("ALERT: %s" % msg, file=sys.stderr)
    url = webhook_url()
    if not url:
        return
    data = json.dumps({"content": "⚠️ star-trajectory engine: " + msg}).encode("utf-8")
    try:
        req = urllib.request.Request(url, data=data, headers={"Content-Type": "application/json"})
        urllib.request.urlopen(req, timeout=10)
    except Exception as e:
        print("webhook delivery failed: %s" % e, file=sys.stderr)


def main():
    if not os.path.exists(STATUS):
        alert("status.json missing — engine has never run")
        return 1
    try:
        s = json.load(open(STATUS, encoding="utf-8"))
        t = datetime.strptime(s["updated_utc"], "%Y-%m-%dT%H:%M:%SZ").replace(tzinfo=timezone.utc)
    except (ValueError, KeyError, json.JSONDecodeError):
        alert("status.json unreadable or missing updated_utc")
        return 1

    age_min = (datetime.now(timezone.utc) - t).total_seconds() / 60.0
    if age_min > MAX_AGE_MIN:
        alert("stale: last cycle %.0f min ago (> %d min threshold)" % (age_min, MAX_AGE_MIN))
        return 1
    if s.get("errors"):
        alert("last %s cycle errored: %s" % (s.get("mode"), "; ".join(s["errors"])[:400]))
        return 1
    print("ok: last %s cycle %.0f min ago, no errors" % (s.get("mode"), age_min))
    return 0


if __name__ == "__main__":
    sys.exit(main())
