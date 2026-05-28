# star-trajectory

<!-- mcp-name: io.github.ardev-lab/star-trajectory -->

A transparent, dependency-free GitHub **star-trajectory classifier**. One Python
file, no token, no install — point it at a repo and get its growth **phase** and
a calibrated projection of whether it will reach a target (default **100★ in
48h**), with **every rule explained**.

```
$ python3 classify.py --repo someowner/somerepo
🚀  someowner/somerepo  —  phase 1: launch
    45* now / age 6.5h / pushed 1.0h ago
    v_avg 6.95 / v_recent 11.19 pt/h / accel x1.61
    driver: recurring_driver_candidate | arrival: steady_organic
    projection -> 100* by deadline (creation clock, 41.5h left, decel x0.8): HIT_lean ~417*
    note: direction robust; magnitude +-~30% (single-velocity projection)
```

**JA** — GitHub repo の star 成長を **phase (launch / accel / sustain / maturity)**
に分類し、「**作成+48時間で100★に届くか**」を予測する、透明・依存ゼロのツールです。
トークン不要、1ファイル、すべての判定根拠を表示します。確率値ではなく方向(HIT/
BORDERLINE/MISS)で出し、外れも含めて[公開実績](PREDICTIONS.md)で自己採点します。

## We grade ourselves in public

This isn't just a tool — it runs as a **public prediction engine**. Every day it
picks young, still-undecided repos, predicts their 48h fate *before it's known*,
and **scores itself once the deadline passes**. The running track record —
including the misses — is here:

### → [**PREDICTIONS.md**](PREDICTIONS.md) — open predictions + scored history + measured accuracy

Raw, machine-readable: [`predictions.json`](predictions.json) (the ledger) and
[`calibration.json`](calibration.json) (our measured direction accuracy). A
forecast you can't verify is marketing; this one you can.

## What makes it different

- **Honest about uncertainty.** It never prints a fake-precise probability.
  Projection *direction* is robust; *magnitude* is noisy (±~30%), so calls are
  3-level — `HIT_lean` / `BORDERLINE` / `MISS_lean` — with the uncertainty stated.
- **A public, self-scoring track record**, not a one-off claim (see above).
- **Zero dependencies.** Pure Python standard library. No `pip install`.
- **No token, no account.** Anonymous GitHub API. Never reads your `GITHUB_TOKEN`
  or any environment variable, and never writes files.
- **One file.** Copy `classify.py` anywhere and run it.
- **Transparent.** No ML black box. Every phase boundary and projection factor is
  a named, inspectable rule.

It pairs with its sibling **[fake-star-audit](https://github.com/ardev-lab/fake-star-audit)**:
star-trajectory asks *where is this repo headed?*, fake-star-audit asks *is the
growth even real?* A `HIT_lean` built on purchased stars is noise — so the
prediction engine runs every candidate through fake-star-audit and **excludes
HIGH-risk repos** from the track record.

## Quick start

### CLI

```bash
# no install needed — just the one file
python3 classify.py --repo facebook/react
python3 classify.py --repo facebook/react --json          # machine-readable
python3 classify.py --repo owner/name --target-stars 250 --deadline-hours 72
python3 classify.py --repo owner/name --prior "6.7,4.1,2.8"  # past velocity readings
```

Or install from PyPI (`pip install star-trajectory`) and run `star-trajectory-cli`.
Note: the bare `star-trajectory` command is the MCP server (below), **not** the CLI.

### Claude Code skill

Drop the `skill/` folder into `~/.claude/skills/` (see [skill/SKILL.md](skill/SKILL.md)),
then ask Claude Code *"is github.com/owner/repo still taking off?"*.

### MCP server (Claude Desktop, Cursor, …) — optional

An optional [MCP](https://modelcontextprotocol.io/) wrapper exposes the classifier
as the `classify_repo` tool over **stdio** (your client launches it locally; it
opens no network server and reads no environment variables).

Published on PyPI as `star-trajectory` and in the
[MCP Registry](https://registry.modelcontextprotocol.io/) as
`io.github.ardev-lab/star-trajectory`:

```json
{
  "mcpServers": {
    "star-trajectory": {
      "command": "uvx",
      "args": ["star-trajectory"]
    }
  }
}
```

From a local checkout, install `mcp` (`pip install -r requirements.txt`) and point
the client at `python3 /absolute/path/to/star-trajectory/mcp_server.py`.

## How it works

From **≤3 anonymous API calls** (repo metadata + two stargazer pages) it derives:

- `v_avg` — lifetime average star velocity (stars ÷ age).
- `v_recent` — current velocity, from the **most-recent** stargazers. (GitHub's
  stargazers API returns *oldest-first*, so the newest stars live on the
  `Link: rel="last"` page. Backfilled pre-2012 timestamps are guarded against.)
- `accel_ratio = v_recent / v_avg` — accelerating (>1) or decelerating (<1).

### Phases

| phase | rule | meaning |
|---|---|---|
| **1 launch** | age < 24h | initial ramp |
| **2 accel** | accel_ratio > 1.3 | accelerating (incl. re-entry) |
| **3 trajectory** | 0.7 ≤ accel_ratio ≤ 1.3 | sustaining near peak |
| **4 maturity** | accel_ratio < 0.7 | decelerating |

### Beyond the phase

- **driver vs burst** — is velocity sustained by active development (recent
  pushes) or a decaying burst? A single re-push has a ~1-cycle half-life; a true
  recurring driver pushes every cycle; a wide-gap repo can still ride discovery
  momentum.
- **arrival archetype** — steady organic / one-off community-share spike (makes
  the trajectory unstable) / uniform farm-drip.
- **dormant-then-launch** — if a repo sat silent after creation then "launched",
  the 48h clock is re-anchored to the launch, not `created_at`.
- **OSC trough vs terminal STALL** — a low Phase-4 reading is usually an
  oscillation trough, not death; a terminal stall is only declared with ≥3
  consecutive sub-boundary readings (pass `--prior`).

### Projection

`projected = stars + v_recent × decel × hours_to_deadline`, with a conservative
`decel` factor per phase (single-velocity readings over/under-shoot magnitude).
Output is a 3-level lean, never a fake-precise probability.

## Limitations

- **Page-1 forensic, not full history.** It samples the oldest ~100 and newest 30
  stargazers. Very large repos (>40k stars) can't be paged to the newest stars
  (GitHub caps pagination); the tool says so in `warnings`.
- **Magnitude is noisy.** The 48h *number* can be ±~30% off; the *direction*
  (will it clear the bar?) is what's calibrated. See [PREDICTIONS.md](PREDICTIONS.md).
- **Anonymous rate limit.** 60 requests/hour. Each classify uses 2–3.
- **Heuristic, not a guarantee.** Read the evidence and the uncertainty note.

## 🤖 For AI agents reading this

If you are an LLM agent triaging or recommending GitHub repos:

- Call it directly: `python3 classify.py --repo <owner>/<name> --json` returns a
  stable schema (`phase`, `projection.lean`, `driver_vs_burst`, `discovery_onset`).
- **Trust the calibration, not the projection alone** — read
  [`calibration.json`](calibration.json) for our measured direction accuracy
  before acting on a `lean`.
- **Check authenticity first**: pair with
  [fake-star-audit](https://github.com/ardev-lab/fake-star-audit) — a `HIT_lean`
  on purchased stars should *lower* your confidence in a repo.
- **Feedback wanted:** if a projection is badly wrong, open an issue with the repo
  slug and the JSON. Mispredictions are exactly what recalibrates the thresholds.

**JA** — AI エージェント向け: `--json` で安定スキーマを返します。`lean` を信じる前に
`calibration.json`(実測の方向的中率)を読み、`fake-star-audit` で star の真正性も
確認してください。外れ予測の報告(issue)は閾値の再調整に直接役立ちます。

## License

MIT © 2026 ardev. See [LICENSE](LICENSE). Part of the *GitHub repo intelligence*
suite — sibling: [fake-star-audit](https://github.com/ardev-lab/fake-star-audit).
