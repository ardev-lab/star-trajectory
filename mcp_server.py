#!/usr/bin/env python3
"""
MCP (Model Context Protocol) server for star-trajectory — OPTIONAL.

The core tool (classify.py) is zero-dependency and runs standalone. This wrapper
is only needed if you want to call the classifier from an MCP client (Claude
Desktop, Cursor, etc.). It requires the `mcp` package:  pip install mcp

It runs over stdio: the MCP client launches it as a subprocess on the user's own
machine. It does not open a network server, read environment variables, or write
files of its own — it simply exposes classify.py's logic as the `classify_repo`
tool.

Run (usually launched by the MCP client, not by hand):
    python3 mcp_server.py
"""

from mcp.server.fastmcp import FastMCP

from classify import classify, ClassifyError, REPO_RE

mcp = FastMCP("star-trajectory")


@mcp.tool()
def classify_repo(repo: str, target_stars: int = 100, deadline_hours: float = 48.0,
                  timeout: int = 10) -> dict:
    """Classify a GitHub repo's star-growth phase and project whether it will
    reach a target star count by a deadline.

    Returns a deterministic, rule-based read: the growth phase (1 launch /
    2 accel / 3 trajectory / 4 maturity), a 3-level projection lean
    (HIT_lean / BORDERLINE / MISS_lean — never a fake-precise probability,
    because projection magnitude is noisy while direction is robust), plus
    driver-vs-burst, arrival archetype, and dormant-then-launch detection. Uses
    the anonymous GitHub API only (no token). Heuristic, not a guarantee — read
    the evidence and the uncertainty note.

    Args:
        repo: GitHub repository as "owner/name" (e.g. "facebook/react").
        target_stars: projection target star count (default 100).
        deadline_hours: deadline hours from the clock basis (default 48).
        timeout: per-request HTTP timeout in seconds (default 10).

    Returns:
        A dict with keys: phase, phase_label, projection (lean, projected_stars_
        at_deadline, uncertainty_note, ...), driver_vs_burst, arrival_archetype,
        discovery_onset, metrics, warnings. On failure: error, message.
    """
    if not isinstance(repo, str) or not REPO_RE.match(repo):
        return {"error": "invalid_input",
                "message": "repo must be 'owner/name' (alphanumerics, '.', '_', '-')"}
    try:
        return classify(repo, target_stars=int(target_stars),
                        deadline_hours=float(deadline_hours), timeout=int(timeout))
    except ClassifyError as e:
        return {"error": e.code, "message": e.message}
    except Exception as e:  # never crash the MCP server on an unexpected error
        return {"error": "internal_error", "message": str(e)}


def main():
    mcp.run()  # stdio transport by default


if __name__ == "__main__":
    main()
