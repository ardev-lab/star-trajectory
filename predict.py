#!/usr/bin/env python3
"""
predict.py — the prediction half of the public calibration engine.

Selects young, still-undecided GitHub repos (created recently, star count in
[floor, target-1] so the 100-star outcome is genuinely unknown), classifies each
with classify.py, and appends a falsifiable, dated prediction to the ledger
(predictions.json). score.py later revisits matured predictions and grades them.

Why a public, self-scoring track record (not just a tool): a verifiable record
that accrues over time is reshareable and recurring, and it builds an audience
that a one-shot artifact cannot. The engine is deterministic Python on a schedule
(no LLM, no metered API), so it keeps producing verifiable content at ~zero cost.

Each candidate is also run through the sibling fake-star-audit tool; repos with a
HIGH fake-star risk verdict are excluded (a prediction built on purchased stars is
noise on the track record). This dogfoods our own authenticity tool every cycle.

Idempotent: a repo is predicted at most once (the first time it is selected).
Re-running picks up new repos and skips ones already in the ledger.

Usage:
  python3 predict.py                       # default: target 100, floor 20, limit 8
  python3 predict.py --limit 6 --floor 25
  python3 predict.py --no-audit            # skip the authenticity filter (fewer API calls)
  python3 predict.py --dry-run             # classify + print, do NOT write the ledger
"""

import argparse
import json
import os
import sys
import tempfile
import urllib.parse
import urllib.request
import urllib.error
from datetime import datetime, timezone, timedelta

import classify  # sibling module; reuse its fetch + classifier

# fake-star-audit (sibling tool), dogfooded to exclude HIGH fake-star-risk repos.
# Optional: if it cannot be imported, the authenticity filter is silently skipped.
try:
    import audit
except ImportError:
    _SIB = os.environ.get("FAKE_STAR_AUDIT_DIR", "/home/Armada/toA/fake-star-audit")
    if os.path.isdir(_SIB) and _SIB not in sys.path:
        sys.path.insert(0, _SIB)
    try:
        import audit
    except ImportError:
        audit = None

LEDGER = os.path.join(os.path.dirname(os.path.abspath(__file__)), "predictions.json")
SEARCH = "https://api.github.com/search/repositories"
MIN_HOURS_LEFT = 2.0  # need a meaningful window between prediction and deadline


def load_ledger(path=LEDGER):
    if not os.path.exists(path):
        return []
    with open(path, "r", encoding="utf-8") as f:
        try:
            data = json.load(f)
            return data if isinstance(data, list) else []
        except json.JSONDecodeError:
            return []


def save_ledger(rows, path=LEDGER):
    """Atomic write (temp + rename) so a crash can't corrupt the ledger."""
    d = os.path.dirname(path) or "."
    fd, tmp = tempfile.mkstemp(dir=d, suffix=".tmp")
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as f:
            json.dump(rows, f, indent=2, ensure_ascii=False)
        os.replace(tmp, path)
    finally:
        if os.path.exists(tmp):
            os.remove(tmp)


def search_candidates(floor, target, days_back, per_page, timeout):
    since = (datetime.now(timezone.utc) - timedelta(days=days_back)).strftime("%Y-%m-%d")
    q = "created:>=%s stars:%d..%d" % (since, floor, target - 1)
    url = SEARCH + "?" + urllib.parse.urlencode(
        {"q": q, "sort": "updated", "order": "desc", "per_page": per_page})
    _, _, data = classify.gh_get(url, timeout=timeout)
    items = (data or {}).get("items", []) if isinstance(data, dict) else []
    return [it.get("full_name") for it in items if it.get("full_name")]


def authenticity_verdict(repo, timeout):
    """Dogfood the sibling fake-star-audit tool. Returns LOW / MEDIUM / HIGH, or
    None if the tool is unavailable or errored (fail-open: a transient audit error
    must not silently drop a legitimate repo). Callers exclude HIGH."""
    if audit is None:
        return None
    try:
        return audit.audit(repo, timeout=timeout).get("risk_verdict")
    except audit.AuditError:
        return None


def make_row(repo, c, authenticity=None):
    """Build a ledger row from a classify() result, computing an absolute
    deadline (predicted-now + remaining hours, on the classifier's clock)."""
    now = classify.parse_iso(c["fetch_utc"])
    hours_left = c["projection"]["hours_to_deadline"]
    deadline = now + timedelta(hours=hours_left)
    return {
        "repo": repo,
        "predicted_at_utc": c["fetch_utc"],
        "stars_at_prediction": c["metrics"]["stars"],
        "target_stars": c["projection"]["target_stars"],
        "deadline_utc": deadline.strftime("%Y-%m-%dT%H:%M:%SZ"),
        "clock_basis": c["projection"]["clock_basis"],
        "phase": c["phase"],
        "phase_label": c["phase_label"],
        "lean": c["projection"]["lean"],
        "projected_stars": c["projection"]["projected_stars_at_deadline"],
        "v_recent_pt_h": c["metrics"]["v_recent_pt_h"],
        "accel_ratio": c["metrics"]["accel_ratio"],
        "driver_vs_burst": c["driver_vs_burst"],
        "arrival_archetype": c["arrival_archetype"],
        "authenticity": authenticity,
        "tool_version": c["tool_version"],
        "status": "open",
        "outcome": None,
        "actual_stars_at_deadline": None,
        "direction_correct": None,
        "scored_at_utc": None,
    }


def main(argv=None):
    pr = argparse.ArgumentParser(description="Select young repos and log 100-star/48h predictions.")
    pr.add_argument("--target", type=int, default=classify.DEFAULT_TARGET)
    pr.add_argument("--floor", type=int, default=20, help="minimum stars to consider (default 20)")
    pr.add_argument("--limit", type=int, default=8, help="max new predictions per run (default 8)")
    pr.add_argument("--days-back", type=int, default=2, help="search window in days (default 2)")
    pr.add_argument("--per-page", type=int, default=50, help="search candidates to scan (default 50)")
    pr.add_argument("--timeout", type=int, default=10)
    pr.add_argument("--no-audit", action="store_true",
                    help="skip the fake-star-audit authenticity filter (fewer API calls)")
    pr.add_argument("--dry-run", action="store_true", help="classify + print, do not write the ledger")
    args = pr.parse_args(argv)

    rows = load_ledger()
    seen = {r["repo"] for r in rows}

    try:
        candidates = search_candidates(args.floor, args.target, args.days_back,
                                       args.per_page, args.timeout)
    except classify.ClassifyError as e:
        print("search failed (%s): %s" % (e.code, e.message), file=sys.stderr)
        return 1

    new_rows, scanned = [], 0
    for repo in candidates:
        if len(new_rows) >= args.limit:
            break
        if repo in seen:
            continue
        scanned += 1
        try:
            c = classify.classify(repo, target_stars=args.target, timeout=args.timeout)
        except classify.ClassifyError as e:
            if e.code == "rate_limited":
                print("  rate limited; stopping this run", file=sys.stderr)
                break
            print("  skip %s (%s)" % (repo, e.code), file=sys.stderr)
            continue
        # Only predict genuinely undecided cases.
        if c["phase"] is None:
            continue
        if c["metrics"]["stars"] >= args.target:
            continue
        if c["projection"]["hours_to_deadline"] <= MIN_HOURS_LEFT:
            continue
        # Dogfood fake-star-audit: a HIT_lean built on purchased stars is noise.
        verdict = None if args.no_audit else authenticity_verdict(repo, args.timeout)
        if verdict == "HIGH":
            print("  exclude %-46s (fake-star risk: HIGH)" % repo, file=sys.stderr)
            continue
        row = make_row(repo, c, authenticity=verdict)
        new_rows.append(row)
        print("  predict %-46s %-13s proj~%s* (%s, auth=%s, %.1fh left)" % (
            repo, row["lean"], row["projected_stars"],
            row["phase_label"], verdict or "n/a", c["projection"]["hours_to_deadline"]))

    print("scanned %d new candidates -> %d predictions (%d already in ledger)"
          % (scanned, len(new_rows), len(seen)))

    if args.dry_run:
        print("[dry-run] ledger NOT written")
        return 0
    if new_rows:
        save_ledger(rows + new_rows)
        print("ledger: %d total predictions -> %s" % (len(rows) + len(new_rows), LEDGER))
    return 0


if __name__ == "__main__":
    sys.exit(main())
