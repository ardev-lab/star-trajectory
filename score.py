#!/usr/bin/env python3
"""
score.py — the scoring half of the public calibration engine.

Scans the ledger for predictions whose deadline has passed, fetches the repo's
current star count, grades the outcome (HIT if it reached the target, else MISS),
records whether the prediction's direction was correct, and recomputes the public
calibration (overall direction accuracy + breakdowns by lean and by phase).

Mechanism, not memory: this is a deterministic pass over the ledger meant to run
on a schedule. It does not rely on any agent "remembering" to score — the ledger
is the single source of truth and every matured prediction is graded exactly once.

Scoring happens at/shortly after the deadline, so the star count read is the
deadline count plus a small post-deadline drift (slightly favorable to HIT). This
is disclosed in the calibration output rather than hidden.

Usage:
  python3 score.py                # grade all matured open predictions, write calibration
  python3 score.py --dry-run      # show what would be graded, write nothing
"""

import argparse
import json
import os
import sys
from datetime import datetime, timezone

import classify
from predict import load_ledger, save_ledger, LEDGER

CALIB = os.path.join(os.path.dirname(os.path.abspath(__file__)), "calibration.json")
DIRECTIONAL_LEANS = ("HIT_lean", "MISS_lean")


def grade_direction(lean, outcome):
    """True/False for a directional call (HIT_lean/MISS_lean); None for a
    non-directional lean (BORDERLINE / LOW_CONFIDENCE) which makes no claim."""
    if lean not in DIRECTIONAL_LEANS or outcome not in ("HIT", "MISS"):
        return None
    predicted_hit = (lean == "HIT_lean")
    actual_hit = (outcome == "HIT")
    return predicted_hit == actual_hit


def score_row(row, timeout):
    """Fetch current stars and grade. Mutates and returns the row."""
    owner, name = row["repo"].split("/", 1)
    try:
        repo = classify.fetch_repo(owner, name, timeout)
    except classify.ClassifyError as e:
        if e.code == "not_found":
            row["status"] = "scored"
            row["outcome"] = "repo_gone"
            row["direction_correct"] = None
            row["scored_at_utc"] = datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")
            return row
        raise
    actual = repo.get("stargazers_count", 0) or 0
    outcome = "HIT" if actual >= row["target_stars"] else "MISS"
    row["status"] = "scored"
    row["outcome"] = outcome
    row["actual_stars_at_deadline"] = actual
    row["direction_correct"] = grade_direction(row["lean"], outcome)
    row["scored_at_utc"] = datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")
    return row


def compute_calibration(rows):
    scored = [r for r in rows if r["status"] == "scored"]
    graded = [r for r in scored if r["outcome"] in ("HIT", "MISS")]
    directional = [r for r in graded if r["lean"] in DIRECTIONAL_LEANS]
    n_correct = sum(1 for r in directional if r["direction_correct"] is True)
    n_dir = len(directional)

    by_lean = {}
    for lean in ("HIT_lean", "BORDERLINE", "MISS_lean", "LOW_CONFIDENCE"):
        grp = [r for r in graded if r["lean"] == lean]
        by_lean[lean] = {
            "n": len(grp),
            "actual_hit": sum(1 for r in grp if r["outcome"] == "HIT"),
            "actual_miss": sum(1 for r in grp if r["outcome"] == "MISS"),
        }

    by_phase = {}
    for ph in (1, 2, 3, 4):
        grp = [r for r in directional if r["phase"] == ph]
        cor = sum(1 for r in grp if r["direction_correct"] is True)
        by_phase[str(ph)] = {
            "n_directional": len(grp),
            "n_correct": cor,
            "accuracy": round(cor / len(grp), 3) if grp else None,
        }

    base_hit = sum(1 for r in graded if r["outcome"] == "HIT")
    return {
        "generated_utc": datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ"),
        "totals": {
            "predictions": len(rows),
            "open": sum(1 for r in rows if r["status"] == "open"),
            "scored": len(scored),
            "graded": len(graded),
            "repo_gone": sum(1 for r in scored if r["outcome"] == "repo_gone"),
        },
        "direction_accuracy": {
            "n_directional_calls": n_dir,
            "n_correct": n_correct,
            "accuracy": round(n_correct / n_dir, 3) if n_dir else None,
            "note": "directional calls = HIT_lean + MISS_lean only; BORDERLINE / "
                    "LOW_CONFIDENCE make no directional claim and are excluded.",
        },
        "base_rate_hit": round(base_hit / len(graded), 3) if graded else None,
        "by_lean": by_lean,
        "by_phase": by_phase,
        "scoring_note": "scored at/after each deadline; star count is the deadline "
                        "count plus small post-deadline drift (slightly favorable to HIT).",
    }


def _calibration_changed(new, path):
    """True if the calibration stats differ from what's on disk, ignoring the
    generated_utc timestamp — so a no-op run doesn't churn the file/git tree."""
    if not os.path.exists(path):
        return True
    try:
        old = json.load(open(path, encoding="utf-8"))
    except (ValueError, OSError):
        return True
    strip = lambda d: {k: v for k, v in d.items() if k != "generated_utc"}
    return strip(new) != strip(old)


def main(argv=None):
    pr = argparse.ArgumentParser(description="Grade matured predictions and recompute calibration.")
    pr.add_argument("--timeout", type=int, default=10)
    pr.add_argument("--dry-run", action="store_true", help="show what would be graded; write nothing")
    args = pr.parse_args(argv)

    rows = load_ledger()
    if not rows:
        print("ledger empty; nothing to score")
        return 0

    now = datetime.now(timezone.utc)
    due = [r for r in rows if r["status"] == "open"
           and classify.parse_iso(r["deadline_utc"]) is not None
           and classify.parse_iso(r["deadline_utc"]) <= now]
    print("ledger %d rows; %d open; %d due for scoring" % (
        len(rows), sum(1 for r in rows if r["status"] == "open"), len(due)))

    graded = 0
    for r in due:
        if args.dry_run:
            print("  would score %-46s (deadline %s, predicted %s)" % (
                r["repo"], r["deadline_utc"], r["lean"]))
            continue
        try:
            score_row(r, args.timeout)
            graded += 1
            mark = {True: "correct", False: "WRONG", None: "no-call"}[r["direction_correct"]]
            print("  scored %-46s %-9s -> %s (%s*, %s)" % (
                r["repo"], r["lean"], r["outcome"],
                r["actual_stars_at_deadline"], mark))
        except classify.ClassifyError as e:
            print("  score failed %s (%s)" % (r["repo"], e.code), file=sys.stderr)

    calib = compute_calibration(rows)
    if args.dry_run:
        print("[dry-run] calibration NOT written; would be:")
        print(json.dumps(calib["direction_accuracy"], indent=2))
        return 0

    if graded:
        save_ledger(rows)
    if _calibration_changed(calib, CALIB):
        with open(CALIB, "w", encoding="utf-8") as f:
            json.dump(calib, f, indent=2, ensure_ascii=False)
    acc = calib["direction_accuracy"]
    print("calibration: %s/%s directional correct (%s) -> %s" % (
        acc["n_correct"], acc["n_directional_calls"], acc["accuracy"], CALIB))
    return 0


if __name__ == "__main__":
    sys.exit(main())
