# SPEC — star-trajectory (work_02)

(cy26 draft, SPECA 互換 lightweight tier per master 通達 2026-05-23 14:05 UTC; 実装は master+Claude が pick)

> **As-built note:** the shipped `classify.py` follows this spec; velocity thresholds and the decel / backfill handling are continuously refined against live calibration data — see [PREDICTIONS.md](PREDICTIONS.md).

## 1. 概要 (Overview)

`star-trajectory` は GitHub repo を 1 つ取り、star 成長の **phase (1-4)** を分類し、指定閾値 (default 100★) を期限内 (default 創設+48h) に超えるかを **projection** する Claude Code skill + Python helper。02_Github_bot が cycle 1-26 で calibrate した velocity band + OSC-vs-terminal-STALL + recurring-driver-vs-burst framework を transparent rule-based に codify する。

`classify.py` は anonymous GitHub REST API で `created_at` / `stargazers_count` / `pushed_at` + **最新 stargazers** (starred_at 付き) を fetch し、`v_avg` (創設来平均) と `v_recent` (最新勢い) を導出して phase と projection を JSON 出力する。

> **cy27 feedback.md L1 修正 (🔴 旧仮定 反証)**: stargazers API は **oldest-first** で返す。「page-1 = latest」は **誤り**。最新 star を取るには **Link header の `rel="last"` ページを fetch** する (work_01 audit.py の fetch ロジックを流用)。`v_recent` は **last-page (最新 ~per_page 件)** の span から導出する。
> **cy27 feedback.md L3 修正 (🔴 backfill 罠)**: `v_recent` は **age < 90d の若い repo のみ** 算出。古い repo は starred_at backfill (pre-2012 一括 timestamp + 低番 user ID) で span/gap が壊れるため、`v_recent = null` に縮退し v_avg のみで projection + warning。

design 原則: work_01 と同一 (transparent / deterministic / conservative / anonymous)。加えて **honest-uncertainty**: 数値 projection を fake-precise probability として出さず、3-level lean + 「direction robust, magnitude noisy」注記を必須にする (02 実証: 47/51 direction-correct だが proj 数値 ±30% ブレ)。

## 2. 用語定義 (Glossary)

- **v_avg**: `stargazers_count / age_hours`。創設来の平均 star velocity (pt/h)
- **v_recent**: `lastpage_star_count / lastpage_span_hours`。**最新 ~per_page 件** (Link `rel=last` ページ、oldest-first API なので最終ページが最新) の velocity = 現在の勢い proxy。安価 (repo metadata から total を見て last page index を 1 fetch) に現在 velocity を得る。**age<90d gate + backfill ガード必須** (L3)
- **arrival_archetype** (cy27 Lesson 27c): last-page の starred_at 分布を `steady_organic` (cluster 無, moderate CV) / `community_share_spike` (heavy-tail + 局所 burst 1 個 = WeChat/forum 等 1 回の amplification) / `farm_drip` (end-to-end uniform low-CV tight gaps) に分類。`community_share_spike` 検出時は **trajectory 不安定** として projection を low-confidence flag (一発 event で plateau しうる)
- **accel_ratio**: `v_recent / v_avg`。>1 = 加速、≈1 = sustain、<1 = 減速
- **phase**: Phase 1 launch (age<24h) / Phase 2 accel (accel_ratio>1.3) / Phase 3 trajectory (0.7-1.3) / Phase 4 maturity (accel_ratio<0.7)
- **OSC trough vs terminal STALL** (Lesson 25a): Phase 4 の低 velocity は bounded-oscillation の trough である場合が多く、死とは限らない。**terminal STALL 判定は ≥3 連続 sub-BOUNDARY 観測が要件** (単一 reading では断定しない)
- **recurring-driver vs burst** (Lesson 24a/22d): `pushed_at` 直近 + 高 v_recent = driver (継続 re-surfacing); static pushed + 減速 = burst→plateau-decay
- **decel factor**: projection 時に v_recent に掛ける保守係数 (Phase 1-2=0.8 / Phase 3=0.65 / Phase 4=0.5)。Lesson 24d (単一 spike velocity は magnitude を over/under-project) への対策
- **projection lean**: HIT_lean / BORDERLINE / MISS_lean の 3 値 (fractional probability を出さない)
- **BOUNDARY**: velocity の sub-threshold 境界 (default 1.0 pt/h、size 依存で調整可)

## 3. Invariants (常に成立)

1. tool は **anonymous GitHub API のみ使用**、token 不要 (privacy by design)
2. 同一 input (owner/repo + 観察時刻 + optional prior) に対し deterministic output (rule-based, no random)
3. 出力 JSON は always valid UTF-8 + valid schema (§5)
4. phase は **1/2/3/4 の 4 値のみ**; projection lean は **HIT_lean/BORDERLINE/MISS_lean の 3 値のみ**
5. **数値 projection には必ず uncertainty 注記を併記** (magnitude は ±30% 程度ブレる、direction が信頼できる旨); fake-precise な % 確率を出さない
6. 単一 invocation では **terminal STALL を断定しない** (≥3 連続 sub-BOUNDARY 要件を warning で明示)
7. **rate limit aware**: 1 invocation で ≤ 3 API calls (repo metadata + page-1 stargazers + optional rate_limit check)
8. **stale data declaration**: response に fetch UTC timestamp を含む
9. **scope limit**: public repo のみ; private は graceful error
10. tool は file write しない (stdout JSON only); skill orchestrator が persistence 担当

## 4. Preconditions (入力 / 環境前提)

1. **input**: `--repo owner/name` (必須) + optional `--target-stars N` (default 100) + optional `--deadline-hours H` (default 48, 創設からの) + optional `--prior "v1,v2,..."` (過去 cycle の v_recent 列、OSC/STALL trend 判定用)
2. **runtime**: Python 3.10+ on PATH
3. **dependencies**: stdlib のみ (`urllib.request`, `json`, `argparse`, `re`, `sys`, `statistics`, `datetime`) — no pip
4. **network**: outbound HTTPS to api.github.com
5. **anonymous mode**: no GITHUB_TOKEN; tool MUST NOT read env for tokens (token leak 防止)
6. **repo visibility**: public 必須; private は "scope_out_of_range" error
7. **repo age**: age < 1h は v_recent 不安定として warning 付き出力 (projection は low-confidence flag)
8. **latest-stargazer fetch** (cy27 L1/L3 修正): `Accept: application/vnd.github.star+json` + **Link `rel=last` ページ** (oldest-first API) で最新 starred_at 取得; **age<90d gate** (古い repo は backfill 破損 → v_recent=null 縮退); starred_at 取得不可 or backfill 検出 (unique timestamp 極少) 時も v_recent=null + v_avg のみ縮退 projection + warning
9. **input validation**: owner/name は `^[a-zA-Z0-9_.-]+$`; --target-stars/-hours は正の数値
10. **Claude Code skill mode**: orchestrator が `python classify.py --repo owner/name --json` を invoke し parse

## 5. Postconditions (出力 / 状態保証)

1. **JSON schema** (stable v1):
   ```json
   {
     "tool_version": "0.1.0",
     "repo": "owner/name",
     "fetch_utc": "2026-05-25T05:38:00Z",
     "metrics": {
       "stars": 56, "age_hours": 17.5, "pushed_hours_ago": 17.4,
       "v_avg_pt_h": 3.2, "v_recent_pt_h": 2.1, "accel_ratio": 0.66,
       "page1_count": 56, "page1_span_hours": 15.9
     },
     "phase": 4,
     "phase_label": "maturity (decelerating)",
     "phase4_substate": "OSC_trough_candidate",
     "driver_vs_burst": "burst_or_plateau (pushed static)",
     "projection": {
       "target_stars": 100, "deadline_hours_from_creation": 48,
       "hours_to_deadline": 30.5, "decel_factor": 0.5,
       "projected_stars_at_deadline": 88,
       "lean": "BORDERLINE",
       "uncertainty_note": "direction robust; magnitude ±~30% (single-velocity projection per Lesson 24d)"
     },
     "warnings": [],
     "evidence_summary": "..."
   }
   ```

2. **phase 判定 logic** (deterministic):
   - Phase 1: `age_hours < 24 AND v_recent > 0`
   - Phase 2: `accel_ratio > 1.3 AND age_hours >= 12`
   - Phase 3: `0.7 <= accel_ratio <= 1.3`
   - Phase 4: `accel_ratio < 0.7`
   (境界の優先順位: Phase 2 条件を Phase 1 より優先評価し、加速中の若い repo を accel に分類)

3. **phase4_substate**: prior が与えられた場合のみ確定判定。`--prior` 列 + 現 v_recent で、最後の ≥3 値が全て < BOUNDARY → `terminal_STALL_candidate`; それ以外で低値 → `OSC_trough_candidate`; prior 無し → `OSC_trough_candidate (single reading; STALL needs >=3 consec sub-BOUNDARY)`

4. **driver_vs_burst** (cy27 Lesson 27b + **cy28 Lesson 28a refined — re-push burst has a ~1-cycle (≈12h) half-life**): re-push は SUFFICIENT, NOT NECESSARY、かつ **単発 re-push の効果は ~1 cycle で減衰する**。判定には push-recency だけでなく **push-frequency-over-N-cycles** を見る (optional `--prior-pushes` で過去 push 時刻列を渡す):
   - `pushed_hours_ago < 24 AND v_recent >= BOUNDARY` → `recurring_driver_candidate` (再加速中)。**ただし caveat: 単発 re-push なら次 cycle で減衰しうる** (cy28 実証 codex-shim: cy27 に re-push→6.67、その後 re-push せず → 12.8h で 2.76 に半減)。真の recurring-driver は **毎 cycle re-push** している (cy28 実証 open-gsd 9.35/re-push 2.3h前 + kimi 11.7/re-push 0.3h前 = 継続 push で高 velocity 維持)
   - `pushed_hours_ago` が前 interval のみ (≈12-24h) かつ accel 減速傾向 → `single_pushburst_decaying` (単発 push spike の減衰相; cy28 NEW)
   - `pushed_hours_ago >= 72 AND v_recent >= BOUNDARY` → `wide_OSC_momentum` (static でも discovery/narrative 慣性で持続; cy28 実証 9arm 2252★ static 149h yet 7.15→12.1 = 慣性 AMPLIFYING)
   - `pushed_hours_ago >= 72 AND accel_ratio < 0.7 AND v_recent < BOUNDARY` → `burst_or_plateau_decay` (**terminal decay は static-push AND discovery-exhaustion の両方が必要**; cy28 実証 ABP static 10.3d + 0.33 = 1st EARNED terminal-STALL, corecrypto static 3.6d + 0.57)
   - else `indeterminate`
   - **rule of thumb (Lesson 28a)**: `pushed_this_interval → 高 velocity 持続見込み; pushed_last_interval_only → ~半減見込み (decaying); static_many_cycles AND narrow → terminal_decay; static_but_wide_OSC → discovery-momentum exception`

4b. **dormant_then_launch / discovery-clock** (cy28 Lesson 28b NEW): `created_at` と **discovery-onset** (star が flat→burst へ転じた時刻) を区別する。last-page (最新 star) の最古 starred_at age `first_star_age_hours` を見て、`first_star_age_hours << age_hours` (例: age 52.8h だが first_star 6.9h前) なら **dormant_then_launch** = 創設後しばらく無 star → 告知/launch event で burst。この場合:
   - phase/projection の **48h 期限は created_at でなく discovery-onset (≈first_star time) に anchor し直す** (creation-clock は launch を mismeasure; cy28 実証 openbrief: 創設 52.8h で 48h-since-creation だと near-MISS だが、launch から 6.9h で 99★ climbing 14/h の just-launched winner)
   - 出力に `discovery_onset: {detected, onset_age_hours, dormant_hours_before_onset}` を含め、`clock_basis: "discovery_onset"` を明示 (created_at clock との差を warning)
   - 逆に **flat-0 のまま (launch-onset 未検出)** = silent-push, never launched (cy28 work_01: 0★ flat) → projection は "no launch event detected" low-confidence

5. **projection lean**: `projected >= target × 1.1` → HIT_lean; `target × 0.9 <= projected < target × 1.1` → BORDERLINE; `< target × 0.9` → MISS_lean。**必ず uncertainty_note 併記** (Invariant 5)

6. **decel_factor**: Phase 1/2 = 0.8, Phase 3 = 0.65, Phase 4 = 0.5 (Lesson 24d calibration)

7. **evidence_summary** ≤ 500 chars English; **fetch_utc** ISO 8601 UTC second 精度

8. **error envelope**: `{"error":"code","message":"..."}` のみ (他 top-level key なし)

9. **idempotent**: 同 repo ±10 min で identical output (network error 除く)

10. **all keys present** (null where applicable); projection が縮退時 (v_recent null) は `projection.lean = "LOW_CONFIDENCE"` + warning

## 6. Assumptions (外部依存)

1. **GitHub REST API stability**: `/repos/{o}/{n}`, `/repos/{o}/{n}/stargazers` schema backwards-compatible
2. **~~page-1 = latest 30~~ → 反証済 (cy27 feedback L1)**: stargazers API は **oldest-first**。最新は **Link `rel=last` ページ**。star+json header で starred_at 付与。**age<90d の repo のみ v_recent 算出**、古い repo は backfill (pre-2012 一括 timestamp) で破損するため v_recent=null 縮退 (L3)
3. **v_recent proxy 妥当性**: 最新 30 star の span が現在 velocity を近似する (高 velocity repo では span 短く敏感、低 velocity では span が wide = sliding-window 効果 Lesson 20a を内包)
4. **phase 境界 (1.3 / 0.7)**: cy26 初期値、registry data で back-test して refine 想定 (design §7)
5. **decel factor**: 02 registry の proj-vs-actual (P51 over / P55 under, DIRECTION 5/5 correct) 由来の保守値; magnitude は ±30% 程度ブレる前提
6. **anonymous rate limit** ≥ 60 req/h core 継続 (GitHub が anonymous 廃止しない前提)
7. **pushed_at の意味**: 直近 push が active development の proxy (driver 判定); fork-rename-relaunch 等の特殊 case は誤判定しうる (out-of-scope)
8. **size 非依存の BOUNDARY**: default 1.0 pt/h は中小 repo 想定; 1000★+ repo では velocity 絶対値が大きく BOUNDARY 調整余地 (--boundary option 将来)
9. **timezone**: 全 timestamp UTC
10. **Claude orchestrator**: skill mode で subprocess invoke + JSON parse 可能

## 7. Threat model (STRIDE)

- **Spoofing**: N/A (public read-only API, no auth surface)
- **Tampering**: input owner/name regex validation で URL-injection 防止; output stdout JSON のみ (file write なし); env 読取禁止 (token leak 防止)
- **Repudiation**: N/A (read-only public-data 分析)
- **Information Disclosure**: public data のみ (stargazer login は public); disk への cache/log 禁止; env token 読取禁止
- **Denial of Service**: ≤3 API calls/invocation; HTTP timeout 10s; full star-history fetch しない (page-1 のみ = bounded)
- **Elevation of Privilege**: N/A (no privileged op, no file write by tool)

## 8. CWE Top 25 関連性

- **CWE-77 Command Injection**: owner/name を `^[a-zA-Z0-9_.-]+$` validation 後に URL 合成; subprocess を input から起動しない
- **CWE-918 SSRF**: fetch は `https://api.github.com/...` template のみ (user-controlled host なし)
- **CWE-200 Information Exposure**: private data を出力しない; env token 読取禁止
- **CWE-400 Resource Consumption**: 10s timeout + ≤3 calls + page-1 のみ = bounded
- **CWE-502 Deserialization**: GitHub-trusted JSON のみ (no pickle/yaml.unsafe_load)
- **CWE-755 Improper Exception Handling**: graceful error envelope; rate-limit-exceeded を明示
- **CWE-798 Hard-coded Credentials**: NONE (anonymous by design)
- **CWE-1188 Insecure Default**: default は安全 (anonymous, 保守 decel, single-reading では STALL 断定しない, projection に uncertainty 必須)
- **CWE-682 Incorrect Calculation**: projection の magnitude 不確実性を明示 (over-claim 防止) = honest-uncertainty invariant

## 9. Out-of-scope (cy26 design)

- **multi-page back-fetch / full star-history**: page-1 only (v_recent proxy で代替); 重い時系列は star-history に委ねる
- **fake-star authenticity 判定**: work_01 (fake-star-audit) の領域。本 work は trajectory のみ (bundle suite として併用想定)
- **fork-rename-relaunch / narrative-recycle 検出**: open-gsd 型 recurring driver の機序解析は out-of-scope (driver/burst の binary 判定のみ)
- **non-GitHub platform** (GitLab/Bitbucket)
- **size-adaptive BOUNDARY auto-tuning**: cy26 は固定 default、将来 option

## 10. SPECA pipeline 適合性

§3 Invariants (10) + §4 Preconditions (10) + §5 Postconditions (10) + §6 Assumptions (10) + STRIDE §7 + CWE §8 = ~40 typed property (lightweight tier)。cy27+ で master が手動 SPECA 実行想定。02 は research 進行 (registry の proj-vs-actual data 蓄積) で phase 境界 / decel factor を継続 refine。
