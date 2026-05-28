---
name: star-trajectory
description: Classify a GitHub repository's star-growth phase and project whether it will reach a target (default 100 stars) by a deadline (default launch + 48h). Transparent, dependency-free, rule-based. Use when the user asks where a repo's stars are headed, whether a trending/new repo will keep growing or has plateaued, or "will this repo hit N stars".
---

This skill classifies a GitHub repository's star-growth trajectory and projects a
target/deadline outcome. It wraps `classify.py`, a single-file, zero-dependency,
anonymous-API tool (no token, no file writes).

## When to use

Trigger when the user asks anything like:
- "is `owner/repo` still taking off, or has it plateaued?"
- "will this new repo hit 100 stars in its first 48 hours?"
- "what growth phase is this trending repo in?"

## How to run

`classify.py` lives at the repository root (one directory above this skill). Run:

```bash
python3 /path/to/star-trajectory/classify.py --repo <owner>/<name> --json
```

- Requires only Python 3.10+ and outbound HTTPS to `api.github.com`.
- Uses the anonymous GitHub API (60 req/h). Each call costs 2–3 requests.
- Optional: `--target-stars N`, `--deadline-hours H`, `--prior "v1,v2"` (past
  velocity readings to tell an oscillation trough from a terminal stall).

## How to interpret the JSON

Key fields:
- `phase` (1–4) + `phase_label`: launch / accel / trajectory (sustain) / maturity.
- `projection.lean`: `HIT_lean` / `BORDERLINE` / `MISS_lean` / `LOW_CONFIDENCE`.
  Always cite `projection.uncertainty_note` — direction is robust, magnitude is
  noisy (±~30%).
- `driver_vs_burst`: whether velocity is sustained by active development or is a
  decaying burst.
- `discovery_onset`: if a repo sat dormant then "launched", the deadline clock is
  re-anchored to launch, not creation.
- `metrics`: stars, age, v_avg, v_recent, accel_ratio.

## How to respond to the user

Give a short, plain-language read, then the *reason*:

> **Likely yes (HIT_lean).** It's in an accel phase — the most recent stars are
> arriving ~1.6× faster than its lifetime average, and there was a push an hour
> ago. Projected ~417★ by the 48h mark. (Direction is reliable; the exact number
> is noisy.)

Always state it's a heuristic projection, not a guarantee. If `LOW_CONFIDENCE`,
say why (e.g. too few recent dated stars, or a one-off sharing spike).

## Pair with authenticity

A projected HIT built on *purchased* stars is meaningless. If the growth looks
suspicious, also run the sibling tool **fake-star-audit** to check whether the
stars are real.

## On errors

- `rate_limited`: the anonymous 60/h budget is spent; retry within an hour.
- `not_found`: repo is private or doesn't exist.

This skill only analyses public repositories using public data.
