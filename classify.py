#!/usr/bin/env python3
"""
star-trajectory — transparent, rule-based GitHub star-growth trajectory classifier.

Given one repo, it classifies the repo's star-growth PHASE (1 launch / 2 accel /
3 trajectory / 4 maturity) and PROJECTS whether the repo will cross a target star
count (default 100) by a deadline (default creation + 48h). No machine learning,
no black box: every phase boundary and projection factor is an inspectable rule,
calibrated from 02_Github_bot's multi-cycle observation of real launches.

Honest by design: it never emits a fake-precise probability. Projection DIRECTION
is robust; projection MAGNITUDE is noisy (~+-30%), so a 3-level lean
(HIT_lean / BORDERLINE / MISS_lean) is reported with an explicit uncertainty note.

It reuses the fetch logic proven in its sibling tool fake-star-audit:
  - stargazers API returns OLDEST-first; the most-recent stars live on the
    Link rel="last" page (used to derive current velocity v_recent).
  - timestamps predating GitHub's starred_at tracking (~2012) are backfilled
    to a single bulk value; a backfill guard skips timing math on such data.

Design principles (see SPEC.md):
  - transparent : every rule is inspectable
  - deterministic: same input + observation time -> same output (no random)
  - conservative : conservative decel factors; never declares terminal STALL
                   from a single reading
  - anonymous    : unauthenticated GitHub API; never reads a token or any env
                   variable; never writes files (stdout JSON only)

Usage:
  python3 classify.py --repo owner/name
  python3 classify.py --repo owner/name --json
  python3 classify.py --repo owner/name --target-stars 100 --deadline-hours 48
  python3 classify.py --repo owner/name --prior "6.7,4.1,2.8"   # past v_recent readings
"""

import argparse
import json
import re
import sys
import urllib.request
import urllib.error
from datetime import datetime, timezone
from statistics import median, pstdev, mean

TOOL_VERSION = "0.1.0"
TOOL_URL = "https://github.com/ardev-lab/star-trajectory"
SIBLING_URL = "https://github.com/ardev-lab/fake-star-audit"
API = "https://api.github.com"
UA = "star-trajectory/%s (+%s)" % (TOOL_VERSION, TOOL_URL)

DEFAULT_TARGET = 100
DEFAULT_DEADLINE_H = 48.0
DEFAULT_BOUNDARY = 1.0  # pt/h sub-threshold boundary (size-dependent; --boundary to tune)

# Conservative decel factors applied to v_recent when projecting (Lesson 24d:
# a single-velocity reading over/under-projects magnitude, so we discount it).
DECEL = {1: 0.8, 2: 0.8, 3: 0.65, 4: 0.5}

REPO_RE = re.compile(r"^[A-Za-z0-9_.-]+/[A-Za-z0-9_.-]+$")


class ClassifyError(Exception):
    def __init__(self, code, message):
        self.code = code
        self.message = message
        super().__init__(message)


def parse_iso(ts):
    if not ts:
        return None
    try:
        return datetime.strptime(ts, "%Y-%m-%dT%H:%M:%SZ").replace(tzinfo=timezone.utc)
    except (ValueError, TypeError):
        return None


def is_backfilled(times):
    """True if timestamps look backfilled / low-resolution (many identical
    values). Stars predating GitHub's starred_at tracking (~2012) share a single
    bulk timestamp, which makes every timing-derived metric meaningless."""
    if len(times) < 4:
        return False
    return len(set(times)) < max(5, int(0.10 * len(times)))


def gh_get(url, accept=None, timeout=10):
    """GET a GitHub API URL anonymously. Returns (status, headers, parsed_json)."""
    headers = {"User-Agent": UA, "Accept": accept or "application/vnd.github+json"}
    req = urllib.request.Request(url, headers=headers, method="GET")
    try:
        with urllib.request.urlopen(req, timeout=timeout) as resp:
            raw = resp.read()
            hdrs = {k: v for k, v in resp.headers.items()}
            try:
                data = json.loads(raw.decode("utf-8")) if raw else None
            except (ValueError, UnicodeDecodeError) as e:
                raise ClassifyError("parse_error", "GitHub returned non-JSON: %s" % e)
            return resp.status, hdrs, data
    except urllib.error.HTTPError as e:
        if e.code == 404:
            raise ClassifyError("not_found", "Repo not found (or private).")
        if e.code == 403:
            if e.headers.get("X-RateLimit-Remaining") == "0":
                raise ClassifyError("rate_limited",
                                    "GitHub anonymous rate limit exhausted (60/h). Retry later.")
            raise ClassifyError("forbidden", "GitHub returned 403 (access forbidden).")
        if e.code == 422:
            raise ClassifyError("pagination_capped",
                                "GitHub paginated past its cap (repo too large for last-page fetch).")
        raise ClassifyError("http_%d" % e.code, "GitHub HTTP error %d." % e.code)
    except (urllib.error.URLError, TimeoutError) as e:
        raise ClassifyError("network_error", "Network failure: %s" % e)


# --------------------------------------------------------------------------
# Data fetching (<= 3 API calls total)
# --------------------------------------------------------------------------

def _normalize_stars(data):
    out = []
    for item in (data or []):
        if not isinstance(item, dict):
            continue
        u = item.get("user") or {}
        out.append({"login": u.get("login"), "id": u.get("id"),
                    "starred_at": item.get("starred_at")})
    return out


def fetch_repo(owner, name, timeout):
    _, _, d = gh_get("%s/repos/%s/%s" % (API, owner, name), timeout=timeout)
    if not isinstance(d, dict):
        raise ClassifyError("unexpected", "Unexpected repo metadata shape.")
    return d


def fetch_stargazer_windows(owner, name, timeout=10):
    """Return (earliest, latest, capped).

    earliest = oldest up to 100 stargazers (used for discovery-onset detection).
    latest   = most-recent up to 30 stargazers (used to derive v_recent).
    capped   = True if the repo is too large to reach the last page.
    """
    accept = "application/vnd.github.star+json"
    url = "%s/repos/%s/%s/stargazers?per_page=100" % (API, owner, name)
    _, hdrs, data = gh_get(url, accept=accept, timeout=timeout)
    earliest = _normalize_stars(data)  # oldest-first
    link = hdrs.get("Link", "") or hdrs.get("link", "")
    m = re.search(r"[?&]page=(\d+)>;\s*rel=\"last\"", link)
    capped = False
    latest = earliest[-30:]
    if m and int(m.group(1)) > 1:
        last = int(m.group(1))
        if last > 400:  # GitHub stargazer pagination cap (~40k stars)
            capped = True
        else:
            try:
                url2 = "%s/repos/%s/%s/stargazers?per_page=100&page=%d" % (API, owner, name, last)
                _, _, data2 = gh_get(url2, accept=accept, timeout=timeout)
                latest = _normalize_stars(data2)[-30:]
            except ClassifyError as e:
                if e.code == "pagination_capped":
                    capped = True
                else:
                    raise
    return earliest, latest, capped


# --------------------------------------------------------------------------
# Metric derivation
# --------------------------------------------------------------------------

def dated_sorted(stars):
    return sorted(t for t in (parse_iso(s["starred_at"]) for s in stars) if t)


def compute_v_recent(latest):
    """v_recent = (#dated latest stars) / (span hours of the latest window).
    Returns (v_recent, n_dated, span_hours). v_recent is None when the window
    is too small, backfilled, or zero-span (cannot derive a rate)."""
    times = dated_sorted(latest)
    n = len(times)
    if n < 2:
        return None, n, None
    if is_backfilled(times):
        return None, n, None
    span_h = (times[-1] - times[0]).total_seconds() / 3600.0
    if span_h <= 0:
        return None, n, 0.0  # all in one instant: a burst, but rate is undefined
    return n / span_h, n, span_h


def detect_discovery_onset(earliest, age_hours, capped, age_days):
    """A repo can sit dormant after creation, then 'launch' when announced. In
    that case the 48h deadline should be measured from the launch (first star),
    not from creation. Detected via the age of the oldest star vs repo age.
    Only meaningful on young, non-capped repos (old repos' oldest stars are
    backfilled and the oldest page is unreachable when capped)."""
    if capped or age_days is None or age_days >= 90:
        return {"detected": False, "reason": "skipped (old/capped repo; backfill-unsafe)"}
    times = dated_sorted(earliest)
    if not times:
        return {"detected": False, "reason": "no dated stars"}
    if is_backfilled(times):
        return {"detected": False, "reason": "backfilled timestamps"}
    now = datetime.now(timezone.utc)
    first_star_age = (now - times[0]).total_seconds() / 3600.0
    dormant = age_hours - first_star_age
    detected = (first_star_age < 0.5 * age_hours) and (dormant >= 24)
    return {
        "detected": bool(detected),
        "first_star_age_hours": round(first_star_age, 1),
        "onset_age_hours": round(first_star_age, 1),
        "dormant_hours_before_onset": round(max(dormant, 0.0), 1),
    }


def classify_phase(age_hours, v_recent, accel_ratio):
    """Deterministic phase. Phase 2 (accel) is evaluated before Phase 1 so an
    accelerating young repo is labelled accel rather than launch."""
    if v_recent is None or accel_ratio is None:
        return None, "indeterminate (v_recent unavailable)"
    if accel_ratio > 1.3 and age_hours >= 12:
        return 2, "accel"
    if age_hours < 24 and v_recent > 0:
        return 1, "launch"
    if 0.7 <= accel_ratio <= 1.3:
        return 3, "trajectory (sustain)"
    if accel_ratio < 0.7:
        return 4, "maturity (decelerating)"
    return 1, "launch (early accel, age<12h)"


def phase4_substate(v_recent, prior, boundary):
    """Phase 4's low velocity may be a bounded-oscillation trough, not death.
    A terminal STALL is only declared with >=3 consecutive sub-boundary readings
    (Lesson 25a); a single reading is always 'OSC trough candidate'."""
    series = list(prior or [])
    if v_recent is not None:
        series.append(v_recent)
    if len(series) >= 3 and all(x < boundary for x in series[-3:]):
        return "terminal_STALL_candidate"
    if prior:
        return "OSC_trough_candidate"
    return "OSC_trough_candidate (single reading; STALL needs >=3 consec sub-BOUNDARY)"


def driver_vs_burst(pushed_hours_ago, v_recent, accel_ratio, boundary):
    """Is current velocity sustained by active development (re-push) or is it a
    decaying burst? (Lesson 27b/28a: a single re-push has a ~1-cycle half-life;
    a true recurring driver re-pushes every cycle; a wide-gap repo can still
    ride discovery/narrative momentum.)"""
    if v_recent is None:
        return "indeterminate (no v_recent)"
    if pushed_hours_ago is None:
        return "indeterminate (no push data)"
    if pushed_hours_ago < 24 and v_recent >= boundary:
        return "recurring_driver_candidate"
    if 24 <= pushed_hours_ago < 72 and accel_ratio is not None and accel_ratio < 0.7:
        return "single_pushburst_decaying"
    if pushed_hours_ago >= 72 and v_recent >= boundary:
        return "wide_OSC_momentum"
    if (pushed_hours_ago >= 72 and accel_ratio is not None
            and accel_ratio < 0.7 and v_recent < boundary):
        return "burst_or_plateau_decay"
    return "indeterminate"


def arrival_archetype(latest):
    """Shape of the most-recent stars' arrival. A single sharing event
    (community_share_spike) makes the trajectory unstable; a uniform tight drip
    (farm_drip) is suspicious; otherwise steady_organic."""
    times = dated_sorted(latest)
    n = len(times)
    if n < 8:
        return "insufficient_data"
    if is_backfilled(times):
        return "backfilled"
    gaps = [(times[i + 1] - times[i]).total_seconds() for i in range(n - 1)]
    gaps = [g for g in gaps if g >= 0]
    if not gaps:
        return "insufficient_data"
    m = mean(gaps)
    cv = (pstdev(gaps) / m) if m > 0 else 0.0
    med = median(gaps)
    j, max_density = 0, 1
    for i in range(n):
        while (times[i] - times[j]).total_seconds() > 60:
            j += 1
        max_density = max(max_density, i - j + 1)
    if max_density / n >= 0.5:
        return "community_share_spike"
    if cv < 0.5 and med < 120:
        return "farm_drip"
    return "steady_organic"


def project(stars, v_recent, phase, hours_to_deadline):
    """projected = current stars + discounted recent velocity * remaining hours."""
    if v_recent is None or phase is None:
        return None, None
    decel = DECEL.get(phase, 0.6)
    remaining = max(hours_to_deadline, 0.0)
    projected = stars + v_recent * decel * remaining
    return projected, decel


def projection_lean(projected, target):
    if projected is None:
        return "LOW_CONFIDENCE"
    if projected >= target * 1.1:
        return "HIT_lean"
    if target * 0.9 <= projected < target * 1.1:
        return "BORDERLINE"
    return "MISS_lean"


# --------------------------------------------------------------------------
# Orchestration
# --------------------------------------------------------------------------

def classify(repo_slug, target_stars=DEFAULT_TARGET, deadline_hours=DEFAULT_DEADLINE_H,
             boundary=DEFAULT_BOUNDARY, prior=None, timeout=10):
    owner, name = repo_slug.split("/", 1)
    repo = fetch_repo(owner, name, timeout)
    if repo.get("private"):
        raise ClassifyError("scope_out_of_range", "Repo is private; tool classifies public repos only.")
    earliest, latest, capped = fetch_stargazer_windows(owner, name, timeout)

    now = datetime.now(timezone.utc)
    created = parse_iso(repo.get("created_at"))
    pushed = parse_iso(repo.get("pushed_at"))
    stars = repo.get("stargazers_count", 0) or 0
    age_hours = ((now - created).total_seconds() / 3600.0) if created else None
    age_days = (age_hours / 24.0) if age_hours is not None else None
    pushed_hours_ago = ((now - pushed).total_seconds() / 3600.0) if pushed else None

    v_avg = (stars / age_hours) if (age_hours and age_hours > 0) else None
    v_recent, n_recent, span_recent = compute_v_recent(latest)
    accel_ratio = (v_recent / v_avg) if (v_recent is not None and v_avg and v_avg > 0) else None

    discovery = detect_discovery_onset(earliest, age_hours, capped, age_days) \
        if age_hours is not None else {"detected": False, "reason": "no age"}

    phase, phase_label = classify_phase(age_hours or 0.0, v_recent, accel_ratio)
    sub = phase4_substate(v_recent, prior, boundary) if phase == 4 else None
    dvb = driver_vs_burst(pushed_hours_ago, v_recent, accel_ratio, boundary)
    archetype = arrival_archetype(latest)

    # Deadline clock: re-anchor to discovery onset when a dormant-then-launch
    # pattern is detected, otherwise measure from creation.
    clock_basis = "creation"
    effective_age = age_hours if age_hours is not None else 0.0
    if discovery.get("detected"):
        clock_basis = "discovery_onset"
        effective_age = discovery["onset_age_hours"]
    hours_to_deadline = deadline_hours - effective_age

    projected, decel = project(stars, v_recent, phase, hours_to_deadline)
    lean = projection_lean(projected, target_stars)
    if archetype == "community_share_spike" and lean in ("HIT_lean", "BORDERLINE"):
        lean = "LOW_CONFIDENCE"  # one-off sharing event can plateau abruptly

    warnings = []
    if age_hours is not None and age_hours < 1:
        warnings.append("repo is <1h old; velocity signals are unstable")
    if v_recent is None:
        warnings.append("v_recent unavailable (too few dated / backfilled / zero-span latest stars); "
                        "projection degraded to LOW_CONFIDENCE")
    if capped:
        warnings.append("repo too large to reach last page; latest window fell back to oldest")
    if hours_to_deadline <= 0:
        warnings.append("deadline already passed for this clock basis; projection ~= current count")
    if clock_basis == "discovery_onset":
        warnings.append("deadline re-anchored to discovery onset (dormant %sh before launch); "
                        "creation-clock would mismeasure this launch"
                        % discovery.get("dormant_hours_before_onset"))
    if phase == 4 and not prior:
        warnings.append("phase 4 from a single reading cannot distinguish OSC trough from terminal "
                        "STALL; pass --prior with >=2 past v_recent readings to disambiguate")

    return {
        "tool_version": TOOL_VERSION,
        "tool_url": TOOL_URL,
        "repo": repo_slug,
        "fetch_utc": now.strftime("%Y-%m-%dT%H:%M:%SZ"),
        "metrics": {
            "stars": stars,
            "age_hours": round(age_hours, 1) if age_hours is not None else None,
            "pushed_hours_ago": round(pushed_hours_ago, 1) if pushed_hours_ago is not None else None,
            "v_avg_pt_h": round(v_avg, 3) if v_avg is not None else None,
            "v_recent_pt_h": round(v_recent, 3) if v_recent is not None else None,
            "accel_ratio": round(accel_ratio, 3) if accel_ratio is not None else None,
            "latest_window_count": n_recent,
            "latest_window_span_hours": round(span_recent, 2) if span_recent is not None else None,
        },
        "phase": phase,
        "phase_label": phase_label,
        "phase4_substate": sub,
        "driver_vs_burst": dvb,
        "arrival_archetype": archetype,
        "discovery_onset": discovery,
        "projection": {
            "target_stars": target_stars,
            "deadline_hours": deadline_hours,
            "clock_basis": clock_basis,
            "hours_to_deadline": round(hours_to_deadline, 1),
            "decel_factor": decel,
            "projected_stars_at_deadline": round(projected) if projected is not None else None,
            "lean": lean,
            "uncertainty_note": "direction robust; magnitude +-~30% (single-velocity projection per Lesson 24d)",
        },
        "warnings": warnings,
        "evidence_summary": build_summary(stars, phase, phase_label, dvb, archetype, lean, projected, target_stars),
    }


def build_summary(stars, phase, phase_label, dvb, archetype, lean, projected, target):
    if phase is None:
        return "phase indeterminate (insufficient recent velocity data); %d stars now" % stars
    proj = ("proj ~%d" % round(projected)) if projected is not None else "proj n/a"
    return "phase %d %s; %s; arrival=%s; %s vs %d* target (%s)" % (
        phase, phase_label, dvb, archetype, proj, target, lean)


# --------------------------------------------------------------------------
# Human report
# --------------------------------------------------------------------------

def human_report(r):
    icon = {1: "🚀", 2: "📈", 3: "➡️", 4: "🔻"}.get(r["phase"], "❔")
    m, p = r["metrics"], r["projection"]
    out = []
    out.append("%s  %s  —  phase %s: %s" % (
        icon, r["repo"], r["phase"] if r["phase"] is not None else "?", r["phase_label"]))
    out.append("    %s* now / age %sh / pushed %sh ago" % (
        m["stars"], m["age_hours"], m["pushed_hours_ago"]))
    out.append("    v_avg %s / v_recent %s pt/h / accel x%s" % (
        m["v_avg_pt_h"], m["v_recent_pt_h"], m["accel_ratio"]))
    out.append("    driver: %s | arrival: %s" % (r["driver_vs_burst"], r["arrival_archetype"]))
    if r["phase4_substate"]:
        out.append("    phase-4 substate: %s" % r["phase4_substate"])
    if r["discovery_onset"].get("detected"):
        d = r["discovery_onset"]
        out.append("    discovery onset: launched %sh ago after %sh dormant" % (
            d["first_star_age_hours"], d["dormant_hours_before_onset"]))
    out.append("    projection -> %s* by deadline (%s clock, %sh left, decel x%s): %s ~%s*" % (
        p["target_stars"], p["clock_basis"], p["hours_to_deadline"], p["decel_factor"],
        p["lean"], p["projected_stars_at_deadline"]))
    out.append("    note: %s" % p["uncertainty_note"])
    for w in r["warnings"]:
        out.append("    ! %s" % w)
    out.append("    — classified by star-trajectory · %s" % TOOL_URL)
    out.append("      sibling tool (is the growth real?): fake-star-audit · %s" % SIBLING_URL)
    return "\n".join(out)


def main(argv=None):
    pr = argparse.ArgumentParser(description="Transparent rule-based GitHub star-trajectory classifier.")
    pr.add_argument("--repo", required=True, metavar="owner/name",
                    help="GitHub repo to classify, e.g. owner/name")
    pr.add_argument("--target-stars", type=float, default=DEFAULT_TARGET,
                    help="projection target star count (default 100)")
    pr.add_argument("--deadline-hours", type=float, default=DEFAULT_DEADLINE_H,
                    help="deadline hours from the clock basis (default 48)")
    pr.add_argument("--boundary", type=float, default=DEFAULT_BOUNDARY,
                    help="sub-threshold velocity boundary pt/h (default 1.0)")
    pr.add_argument("--prior", default=None,
                    help="comma-separated past v_recent readings (oldest..newest) for OSC/STALL trend")
    pr.add_argument("--json", action="store_true", help="emit raw JSON instead of human report")
    pr.add_argument("--timeout", type=int, default=10, help="per-request timeout seconds (default 10)")
    args = pr.parse_args(argv)

    if not REPO_RE.match(args.repo):
        err = {"error": "invalid_input", "message": "repo must be 'owner/name' (alnum . _ - only)"}
        print(json.dumps(err) if args.json else "error: %s" % err["message"], file=sys.stderr)
        return 2
    if args.target_stars <= 0 or args.deadline_hours <= 0 or args.boundary <= 0:
        err = {"error": "invalid_input", "message": "target-stars, deadline-hours, boundary must be > 0"}
        print(json.dumps(err) if args.json else "error: %s" % err["message"], file=sys.stderr)
        return 2

    prior = None
    if args.prior:
        try:
            prior = [float(x) for x in args.prior.split(",") if x.strip() != ""]
        except ValueError:
            err = {"error": "invalid_input", "message": "--prior must be comma-separated numbers"}
            print(json.dumps(err) if args.json else "error: %s" % err["message"], file=sys.stderr)
            return 2

    try:
        result = classify(args.repo, target_stars=args.target_stars,
                          deadline_hours=args.deadline_hours, boundary=args.boundary,
                          prior=prior, timeout=args.timeout)
    except ClassifyError as e:
        err = {"error": e.code, "message": e.message}
        print(json.dumps(err) if args.json else "error (%s): %s" % (e.code, e.message), file=sys.stderr)
        return 1
    print(json.dumps(result, indent=2, ensure_ascii=False) if args.json else human_report(result))
    return 0


if __name__ == "__main__":
    sys.exit(main())
