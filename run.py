#!/usr/bin/env python3
"""
run.py — star-trajectory engine cycle runner (mechanism-first, no LLM).

Cron invokes this. It runs the deterministic prediction/scoring scripts, rerenders
the public track record, and commits+pushes ONLY when the ledger actually changed
(new predictions or newly scored ones — never on a bare timestamp bump). It writes
status.json every run so an external watchdog can verify the engine by its
deliverable rather than by trusting that it ran.

Modes:
  --mode predict   (daily)  : score matured + add new predictions + render + push
  --mode score     (hourly) : score matured + render + push (no new predictions)

`git push` uses whatever remote/credential is configured (no secret is embedded
here). On any step failure the error is recorded in status.json and the process
exits non-zero, so the watchdog will alert.
"""

import argparse
import hashlib
import json
import os
import subprocess
import sys
from datetime import datetime, timezone

HERE = os.path.dirname(os.path.abspath(__file__))
STATUS = os.path.join(HERE, "status.json")
LEDGER = os.path.join(HERE, "predictions.json")
CALIB = os.path.join(HERE, "calibration.json")
TRACKED = ["predictions.json", "calibration.json", "PREDICTIONS.md"]
PY = sys.executable


def run_step(script, *script_args):
    return subprocess.run([PY, script, *script_args], cwd=HERE,
                          capture_output=True, text=True, timeout=600)


def git(*args):
    return subprocess.run(["git", *args], cwd=HERE, capture_output=True, text=True, timeout=120)


def file_hash(path):
    if not os.path.exists(path):
        return ""
    h = hashlib.sha256()
    with open(path, "rb") as f:
        h.update(f.read())
    return h.hexdigest()


def write_status(d):
    d["updated_utc"] = datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")
    tmp = STATUS + ".tmp"
    with open(tmp, "w", encoding="utf-8") as f:
        json.dump(d, f, indent=2)
    os.replace(tmp, STATUS)


def main(argv=None):
    ap = argparse.ArgumentParser(description="star-trajectory engine cycle runner")
    ap.add_argument("--mode", choices=["predict", "score"], required=True)
    ap.add_argument("--limit", type=int, default=8, help="max new predictions (predict mode)")
    ap.add_argument("--no-push", action="store_true", help="commit but do not push")
    ap.add_argument("--no-git", action="store_true", help="skip git entirely (local dry cycle)")
    args = ap.parse_args(argv)

    status = {"mode": args.mode, "steps": [], "errors": []}
    before = (file_hash(LEDGER), file_hash(CALIB))

    def step(label, script, *a):
        r = run_step(script, *a)
        status["steps"].append("%s:%d" % (label, r.returncode))
        if r.returncode != 0:
            status["errors"].append("%s: %s" % (label, (r.stderr or r.stdout).strip()[:300]))
        return r

    # 1. Always grade matured predictions first (idempotent).
    step("score", "score.py")
    # 2. Predict mode: add new predictions for today's young repos.
    if args.mode == "predict":
        step("predict", "predict.py", "--limit", str(args.limit))
    # 3. Rerender the public track record from the ledger.
    step("render", "render.py")

    after = (file_hash(LEDGER), file_hash(CALIB))
    changed = before != after
    status["ledger_changed"] = changed

    if changed and not args.no_git:
        git("add", *TRACKED)
        staged = git("diff", "--cached", "--quiet")
        if staged.returncode != 0:  # there is something staged
            msg = "engine: %s cycle %s" % (
                args.mode, datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%MZ"))
            c = git("commit", "-m", msg)
            status["steps"].append("commit:%d" % c.returncode)
            if c.returncode != 0:
                status["errors"].append("commit: " + (c.stderr or c.stdout).strip()[:300])
            elif not args.no_push:
                p = git("push")
                status["steps"].append("push:%d" % p.returncode)
                if p.returncode != 0:
                    status["errors"].append("push: " + (p.stderr or p.stdout).strip()[:300])

    write_status(status)
    if status["errors"]:
        print("cycle had errors:", "; ".join(status["errors"]), file=sys.stderr)
        return 1
    print("cycle ok (mode=%s, ledger_changed=%s)" % (args.mode, changed))
    return 0


if __name__ == "__main__":
    sys.exit(main())
