# Stage 1 AI Auditor Handoff

## Purpose

This document is a factual snapshot of the Stage 1 paper-trading system that
currently exists in:

`C:\Users\Jayant\Desktop\traiding`

It is intended for an independent AI or human auditor. It separates implemented
behavior from proposed improvements. It is not a request to place real orders,
change the live strategy, or open sealed test data.

Evidence was inspected locally on 2026-07-27 at approximately 12:50 IST. No
secret file was read and no credential, token, App ID, chat ID, or private URL
is included here.

## Executive status

- The project is a real, deterministic, **paper-only** intraday simulator. It
  consumes FYERS market data, creates point-in-time features, generates
  deterministic candidates, performs risk checks, and simulates fills and
  exits.
- It is not an autonomous AI trader. Qwen, cloud models, news decisions,
  memory, automatic learning, and model-generated executable instructions are
  disabled in the live path.
- It monitors 10 NSE cash-equity stocks. NIFTY 50 and India VIX are context
  inputs and are never traded.
- It keeps two fee views over the same simulated trades: Shoonya and Zerodha.
  These are not two separate portfolios and must not be added together.
- Finalized replay evidence through 2026-07-24 is negative:
  - Shoonya view: ₹100,000.00 to ₹99,645.15, net **-₹354.85**
    (**-0.355%**).
  - Zerodha view: ₹100,000.00 to ₹99,618.78, net **-₹381.22**
    (**-0.381%**).
- The 2026-07-27 session was still active when this snapshot was taken. Its
  provisional state showed two completed losing trades and no open positions.
  It must not be treated as final evidence until post-close finalization and
  replay complete.
- No strategy has passed promotion. The historical factor lab found no evidence
  for any of six tested factors. The intraday variant lab has only one
  development and one validation session and reports
  `INSUFFICIENT_SESSIONS`.
- The project is not yet reproducibly versioned: the Git repository has no
  commit, and all project files are currently untracked. Existing run cards
  therefore record `UNBORN_OR_UNAVAILABLE` and `dirty_worktree: true`.
- Current automated validation passes: **91 Python tests**, **2 dashboard
  build/render tests**, and the **secret scanner**. One FYERS SDK deprecation
  warning remains.

## Non-negotiable boundaries

The auditor must preserve all of the following:

1. Paper trading only. No broker order placement.
2. No order WebSocket, order adapter, or route to real money.
3. No model output may become an executable trading instruction.
4. NIFTY 50 and India VIX remain context-only.
5. Rewards and lessons remain `PENDING_HUMAN_REVIEW`.
6. No strategy may self-modify from wins or losses.
7. No cloud model, news model, local LLM, or memory component may be inserted
   into the live decision path without a new, explicit, offline validation
   protocol.
8. Do not read or print `.env`. It is ignored by Git and contains local
   credentials.
9. Do not open the sealed historical test partition or change live thresholds
   while auditing.

## Implemented architecture

```text
Manual FYERS OAuth
        |
        v
Local untracked .env
        |
        v
FYERS data WebSocket (market data only)
        |
        v
FyersDataRecorder
  |-- control/status messages -> safe counter
  |-- invalid messages        -> immutable quarantine JSON
  `-- normalized ticks        -> immutable Parquet + SQLite manifests
                                      |
                                      v
                           causal one-minute bars
                                      |
                                      v
                           point-in-time features
                                      |
                                      v
                         deterministic candidate gate
                                      |
                                      v
                         deterministic risk controller
                                      |
                                      v
                   next-observable simulated fill model
                                      |
                                      v
                    stops / post-cutoff flatten / outcomes
                            |                       |
                            v                       v
                      Shoonya costs           Zerodha costs
                            \                       /
                             v                     v
                      append-only journal, Parquet artifacts,
                       replay report, state, hashed run card

Historical research is separate:
Yahoo daily snapshots -> immutable normalized daily bars -> chronological
TRAIN / EMBARGO / VALIDATION / sealed TEST -> factor lab and universe ranking.

The local control room is read-only:
state + manifests + decision journal -> local API on port 8766
                                     -> dashboard on port 3000
```

## Important components

### Configuration and schemas

- `config/stage1.yaml`
  - Paper mode and the fixed universe.
  - Market schedule, 6-second tick freshness, risk limits, fill assumptions,
    model-disable switches, alerts, and operational thresholds.
- `config/cost_profiles.yaml`
  - Shoonya and Zerodha intraday cost models, versioned
    `nse_equity_intraday_2026-07-22`.
- `config/evaluation_protocol.yaml`
  - General evaluation protocol, currently
    `DRAFT_NOT_FROZEN`.
- `config/intraday_variants.yaml`
  - Frozen six-variant intraday lab using July 20 as development and July 22 as
    validation; its test partition remains sealed.
- `src/stage1/schemas.py`
  - Frozen Pydantic contracts for ticks, bars, features, decisions, fills,
    outcomes, rewards, and run cards.

### Market-data boundary

- `src/stage1/adapters/fyers_market_data.py`
  - Imports the FYERS **data** WebSocket only.
  - Subscribes in `SymbolUpdate` mode.
  - Uses SDK reconnection and exposes a stalled-connection failure to the outer
    recorder loop.
- `scripts/record_fyers.py`
  - Restarts the data-only socket after an exhausted SDK retry cycle with
    configured backoff.
  - Loads credentials once at process start. A newly refreshed token therefore
    requires a recorder restart.
  - Ctrl+C closes the recorder, flushes the spool, and performs a bounded
    shutdown.
- `src/stage1/market/normalizer.py`
  - Recognizes FYERS control types such as `cn` and `ful` as status messages.
  - Accepts a market tick only when an explicit supported `symbol` exists; the
    FYERS `s` status field is not a symbol fallback.
  - Treats zero price with matching zero quantity as an unavailable quote side.
  - Rejects inconsistent zero-price/positive-quantity sides.
  - Preserves a redacted canonical raw message and distinct exchange and laptop
    receive timestamps.
- `src/stage1/storage/parquet_writer.py`
  - Writes immutable Parquet chunks in batches of 500 or after five seconds.
- `src/stage1/storage/operational_db.py`
  - Stores hash-addressed raw-tick and quarantine manifests in SQLite with WAL
    and `synchronous=FULL`.

### Causal data pipeline

- `src/stage1/market/bar_builder.py`
  - Builds one-minute bars and propagates data-quality flags.
- `src/stage1/market/features.py`
  - Produces VWAP, EMA 9/21, RSI 14, ATR, returns, volume z-score, and context
    features.
  - Uses point-in-time availability timestamps rather than future observations.
- `src/stage1/strategy/candidate_gate.py`
  - Rejects snapshots with critical flags:
    `OUT_OF_ORDER_TICK`, `STALE_TICK`, `CLOCK_SKEW`,
    `MISSING_PREVIOUS_MINUTE`, `CUMULATIVE_VOLUME_RESET`, and
    `RETURN_WINDOW_GAPPED`.
  - Emits deterministic, hash-addressed candidates.

### Paper engine and replay

- `scripts/run_live_paper.py`
  - Requires paper mode, disabled live-order endpoints, no halt file, today's
    raw-data directory, and an exclusive per-session process lock.
  - Carries forward the previous finalized balances.
  - Rebuilds bars, features, candidates, and deterministic replay during each
    cycle.
  - Writes append-only decision events and an atomic JSON session state.
  - Enters `SAFE_HOLD` when a cycle fails.
  - Stops new entries at 14:45, flattens at/after 15:15 using observations up to
    15:30, and finalizes at 15:35.
  - Replays a finalized session twice and rejects non-identical fingerprints.
- `src/stage1/paper/replay.py`
  - Implements risk approval, next-observable fills, partial fills, stops,
    flattening, cost calculation, outcomes, and review-only rewards.
- `src/stage1/paper/costs.py`
  - Applies both cost profiles to the same fills.

### Research and evaluation

- `src/stage1/adapters/fyers_history.py`
  - Has a read-only adapter for the documented FYERS History method.
- `src/stage1/adapters/yahoo_history.py`
  - Has a read-only, no-auth, daily-only Yahoo public chart fallback.
  - Yahoo data is never mixed into the live FYERS feed.
- `scripts/prepare_historical_research.py`
  - Stores raw provider responses, normalized candles, hashes, manifests, and a
    chronological walk-forward dataset.
- `src/stage1/evaluation/universe_ranker.py`
  - Ranks research suitability using training-only data quality, liquidity, and
    stability. It does not claim predictability.
- `src/stage1/evaluation/factor_lab.py`
  - Evaluates declared factors with causality checks and leaves test metrics
    sealed.
- `src/stage1/evaluation/intraday_lab.py`
  - Runs declared deterministic variants across cost profiles and execution
    delays.

### Alerts and UI

- `src/stage1/alerts/telegram.py`
  - Runtime is send-only and calls only Telegram `sendMessage`.
  - No runtime command polling or trading instructions exist.
- `scripts/setup_telegram.py`
  - During one-time setup only, calls `getMe`, `deleteWebhook`, and
    `getUpdates` to discover the private chat after `/start`.
- `scripts/run_control_room.py`
  - Exposes local `GET /api/health` and `GET /api/snapshot`.
  - Every POST returns a read-only error.
  - Sanitizes telemetry and does not expose credentials.
- `dashboard/app/page.tsx`
  - Renders component health, the 10 tradable symbols, the two context symbols,
    virtual balances, candidates, risk decisions, simulated exposure, trades,
    quality, history, and events.
  - It is a telemetry surface, not a trading control panel.

## Fixed live universe

Tradable:

1. RELIANCE
2. TCS
3. INFY
4. HDFCBANK
5. ICICIBANK
6. SBIN
7. BHARTIARTL
8. LT
9. AXISBANK
10. ITC

Context-only:

1. NIFTY 50
2. India VIX

The historical research ranking selected HDFCBANK, RELIANCE, ICICIBANK, and
INFY as the four simplest training-only research candidates. That selection did
**not** change the live 10-stock universe.

## Frozen deterministic baseline

### Schedule and preconditions

- Market open: 09:15 IST.
- Candidate evaluation begins: 09:25 IST.
- Evaluation cadence: every 3 minutes.
- New-entry cutoff: 14:45 IST.
- Force-flatten time: 15:15 IST.
- Engine finalization: 15:35 IST.
- Minimum price: ₹100.
- Maximum estimated spread: 15 basis points.
- Minimum volume z-score: 0.5.
- Per-symbol cooldown after a decision: 15 minutes.
- At most five ranked candidates per gate.

### Long candidate

All must pass:

- close above VWAP;
- EMA 9 above EMA 21;
- 5-minute return exceeds NIFTY 50's 5-minute return by more than 10 basis
  points;
- RSI 14 is between 52 and 72;
- volume and spread gates pass;
- no critical data-quality flag.

### Short candidate

The symmetric conditions apply:

- close below VWAP;
- EMA 9 below EMA 21;
- relative 5-minute return is below -10 basis points;
- RSI 14 is between 28 and 48;
- volume and spread gates pass;
- no critical data-quality flag.

### Ranking score

The implemented score is:

- 30% clipped relative momentum;
- 25% clipped volume z-score;
- 20% EMA separation scaled by ATR;
- 15% spread quality.

The declared weights total 90%, so the score's maximum is 0.9 rather than 1.0.
This is internally consistent for ranking but should be reviewed if the score is
shown as a probability or confidence.

## Risk, fills, and virtual money

- Starting virtual cash was ₹100,000 per fee view.
- Maximum open positions: 2.
- Maximum completed round trips per day: 2.
- Risk budget per trade: 0.25% of current equity.
- Maximum capital allocated per position: 25% of equity.
- Maximum same-sector positions: effectively 1.
- Daily loss stop: 0.75% of starting equity.
- Three consecutive losses block another entry.
- Stop distance: greater of 1.2 ATR or two ₹0.05 ticks.
- Quantity: lower of risk-budget sizing and capital-cap sizing.
- A risk approval does not guarantee a fill.
- The simulator searches for the next observable quote within 20 seconds.
- It rejects a fill if the quote moved more than 0.25 ATR from the estimate.
- Displayed bid/ask quantity caps fills. When depth is absent, 1% of recent
  volume is used as a conservative cap.
- A one-basis-point default slippage is applied.
- Positions exit on an observed stop crossing or are flattened after 15:15.
- Every outcome is computed separately with Shoonya and Zerodha costs.
- Rewards are bounded and always marked `PENDING_HUMAN_REVIEW`; they are not
  eligible for live memory or automatic strategy changes.

## Paper-only safety evidence

Implemented safeguards:

- `mode: paper`.
- `live_order_endpoints_enabled: false`.
- No order adapter or order WebSocket exists in `src/stage1`.
- The recorder imports only `fyers_apiv3.FyersWebsocket.data_ws`.
- The paper engine fails closed when mode or live-order configuration is wrong.
- A local `state/HALT_STAGE1` file prevents or halts the paper engine.
- The intent `model_response_hash` is the hash of the fixed
  `DETERMINISTIC_BASELINE_NO_LLM` marker, not an LLM response.
- Live Qwen, cloud, news, and memory decisions are disabled.
- The runtime Telegram client is outbound-only.
- The control-room API is read-only.
- The configured FYERS app has been operator-reported as non-trading, but the
  local runtime does not independently query broker permission metadata.

## Historical research evidence

### Available data

- Provider actually stored: `YAHOO_PUBLIC_CHART`.
- Resolution: daily only.
- Requested range: 2025-07-21 through 2026-07-21.
- Coverage: all 10 tradable stocks plus the two context indices have immutable
  Yahoo request/raw/bar artifacts.
- Prepared research dataset: 2,500 rows across the 10 tradable stocks.
- Chronological splits: TRAIN, EMBARGO, VALIDATION, and sealed TEST.
- Selected-for-simple-baseline research symbols:
  HDFCBANK, RELIANCE, ICICIBANK, and INFY.
- No stored FYERS historical candle artifacts were found. The FYERS History
  adapter exists, but previous live verification returned a broker permission
  error. The successful local research evidence is therefore Yahoo daily data,
  not broker-grade intraday history.

### Factor-lab result

The causal audit passed, live rules were not modified, and test metrics remain
unexposed. All six factors were classified `NO_EVIDENCE`:

- one-day reversal;
- 20-day volume surprise;
- 5-day momentum;
- 20-day low volatility;
- 20-day range position;
- 20-day momentum.

The sealed test contains 2,940 rows and has not been used for these reported
metrics.

### Intraday variant-lab result

- Development session: 2026-07-20.
- Validation session: 2026-07-22.
- Six frozen variants.
- Two cost profiles.
- Six artificial execution delays.
- Total comparisons: 144.
- No variant was positive on both sessions and both fee profiles at zero delay.
- Strict momentum lost less in development but produced only one completed
  trade and did not establish validation performance.
- Gate status: `INSUFFICIENT_SESSIONS`.
- Promotion allowed: false.
- Live configuration changed: false.
- Sealed intraday TEST data remains unopened.

## Session and ledger evidence

The following values come from immutable replay reports and session state.
All P&L is after the named cost model.

| Date | Evidence/status | Candidates | Approved | Completed trades | Shoonya ending / session P&L | Zerodha ending / session P&L |
|---|---:|---:|---:|---:|---:|---:|
| 2026-07-20 | Offline replay report | 14 | 3 | 2 | ₹99,830.98 / -₹169.02 | ₹99,820.41 / -₹179.59 |
| 2026-07-22 | `FINALIZED`, replay-identical run card | 1 | 1 | 1 | ₹99,792.85 / -₹38.13 | ₹99,777.51 / -₹42.89 |
| 2026-07-23 | `FINALIZED_NO_TRADES` | 0 | 0 | 0 | unchanged | unchanged |
| 2026-07-24 | `FINALIZED`, replay-identical run card | 23 | 2 | 2 | ₹99,645.15 / -₹147.71 | ₹99,618.78 / -₹158.73 |
| 2026-07-27 | **Provisional `PAPER_ACTIVE`** at snapshot | state: 8; journal: 12 | state: 4 | 2 | ₹99,538.09 / -₹107.05 provisional | ₹99,501.53 / -₹117.26 provisional |

The July 27 trades visible in the append-only journal at the snapshot:

1. INFY long, 23 shares, stopped; net -₹69.06 Shoonya / -₹74.80 Zerodha.
2. LT long, 6 shares, stopped; net -₹38.00 Shoonya / -₹42.45 Zerodha.

The state/journal candidate-count disagreement during the active July 27
session is itself an audit item. Finalized artifacts, rather than a live
heartbeat snapshot, must be the source of truth.

### Recorder coverage from SQLite manifests

| Date | First received (IST) | Last received (IST) at evidence snapshot | Rows |
|---|---:|---:|---:|
| 2026-07-20 | 09:08:51 | 15:18:03 | 296,557 |
| 2026-07-22 | 09:33:15 | 17:39:39 | 281,564 |
| 2026-07-23 | 09:06:43 | 16:24:38 | 299,863 |
| 2026-07-24 | 09:16:53 | 17:22:46 | 325,299 |
| 2026-07-27 | 09:33:28 | 12:53:55, still active | 163,000 |

Only July 20 and July 23 began before 09:15. July 20 ended before the 15:30
close. The finalized engine sessions also started later than their recorders.
These are useful engineering sessions but should not be described as a complete
set of independent full-session forward trials.

## Current validation status

Executed locally on 2026-07-27:

- Python: `91 passed`, one FYERS SDK `pkg_resources` deprecation warning.
- Dashboard: production build passed; `2 passed`.
- Secret scanner: passed; no known credential patterns in Git candidate files.

Important interpretation:

- Passing tests establishes internal behavior, not profitability.
- A hashed run card exists for July 22 and July 24.
- A run-card hash is not a cryptographic signature. There is no HMAC or private
  signing key.
- No run card exists for July 23 because the no-candidate finalization path
  returns before run-card creation.
- The system does not currently classify sessions as `VALID`, `PARTIAL`, or
  `INVALID`; it uses operational states such as `FINALIZED`,
  `FINALIZED_NO_TRADES`, `SAFE_HOLD`, and `STOPPED_EARLY`.

## Known defects, failures, and limitations

### P0: safety, integrity, and reproducibility

1. **No committed code baseline.** Git has no commit and all project files are
   untracked. Run cards cannot identify the actual code version.
2. **Run cards are hashes, not signatures.** They have no HMAC/signature, always
   set `previous_run_card_hash` to null, and are not chained.
3. **No-candidate sessions lack full proof artifacts.** July 23 has bars and
   features but no candidate artifact, replay report, or run card.
4. **Invalid exchange timestamps were persisted.** SQLite contains 47 one-row
   manifests assigned to session date `1980-01-01`, across all symbols. Their
   exchange timestamp is 1979-12-31 18:30 UTC while laptop receive timestamps
   are in July 2026. These appear to be after-hours provider snapshots and
   should be quarantined, not accepted as valid market time.
5. **Active state and journal disagree.** On July 27, the state reported 8
   candidates while the append-only journal held 12 unique candidate events.
   The reason must be explained and reconciled.
6. **No formal session validity classifier.** Missing opening coverage,
   interruptions, stale ratios, early stop, unresolved positions, and replay
   status are not combined into a final `VALID`/`PARTIAL`/`INVALID` decision.
7. **The runtime does not verify broker app permissions.** The paper boundary
   is enforced locally, but a startup preflight does not independently prove
   that order permission is absent.

### P1: live reliability and realism

1. The engine uses raw-file modification age greater than 120 seconds as its
   coarse feed-level fail-safe. It does not implement the requested rolling
   ten-minute, per-symbol stale-evaluation ratio with an immediate warning at
   10%.
2. Per-symbol feed latency is displayed by the control room, but it is not
   persisted as a complete session acceptance metric or used by a dedicated
   watchdog.
3. The recorder can restart after SDK retry exhaustion, but it loads the token
   only once. It cannot hot-reload a refreshed token.
4. Recorder and paper engine are still launched and supervised as separate
   terminal processes. There is no single durable supervisor with atomic
   startup ordering, health recovery, and end-of-day reconciliation.
5. The live engine rebuilds the full day's Parquet data, bars, features, and
   replay every cycle. This is simple and deterministic but increasingly
   expensive and makes state/journal timing harder to reason about.
6. Fill realism is intentionally simple: next observable quote, displayed or
   recent-volume liquidity cap, and fixed slippage. It has no queue-position,
   market-impact, auction, or gap model.
7. Exit logic has a stop and time flatten but no predeclared profit target,
   trailing stop, maximum holding period, or regime-dependent exit.
8. Statutory charges are frozen to the July 22 cost version and need periodic,
   source-backed review.

### P2: research and validation

1. `evaluation_protocol.yaml` is still a draft with null partition dates and
   null purge/embargo parameters, even though the separate daily research
   artifact has chronological splits.
2. The intraday variant lab has only two sessions. That is far too little to
   estimate generalization.
3. All six daily factors are `NO_EVIDENCE`; none is eligible for live rules.
4. Every currently finalized fee-view ledger is below starting capital. There
   is no evidence of positive expectancy.
5. The ranking's stability score is zero and volatility-dispersion value is one
   for every stock. The normalization and meaning of those columns should be
   audited before using the ranking to shrink the live universe.
6. Yahoo evidence is daily and public; it cannot validate one-minute intraday
   fills, spread behavior, or opening dynamics.
7. The FYERS History integration has tests and fixtures but no successful
   broker historical artifact in the current project.
8. The ranking score's weights total 0.9. This is acceptable for ordering but
   misleading if presented as calibrated confidence.

### P3: documentation and operations

1. README command examples still contain the old
   `C:\Users\Jayant\Documents\stage1-paper-agent` path instead of the canonical
   Desktop project.
2. `dashboard/README.md` is still the generic starter README and does not
   document the Stage 1 control room.
3. Dashboard status depends on local state and heartbeat interpretation. It
   should clearly distinguish “process alive,” “recorder connected,” “current
   ticks reaching engine,” “candidate scanning,” and “simulated position open.”
4. There is no concise immutable daily acceptance report that combines data
   coverage, latency, stale evaluations, interruptions, trades, ledgers, replay,
   and safety checks.

## Proposed improvements for independent audit

Everything below is a proposal. None is currently implemented or approved for
the live baseline.

### Proposed P0 work

1. Create the first intentional Git commit, preserve `.env` and runtime data as
   ignored, and make run cards record a real commit SHA and clean/dirty state.
2. Reject or quarantine impossible/zero/epoch-like exchange timestamps before
   raw-tick storage. Add exact regression fixtures for the observed 1980
   snapshots.
3. Reconcile the live state from journal event IDs and assert state/journal
   counts match at every cycle and finalization.
4. Produce the same complete artifact set for zero-trade sessions.
5. Add a deterministic session classifier:
   - `VALID`: required coverage, acceptable latency/staleness, no unresolved
     positions, clean replay, safety pass.
   - `PARTIAL`: safe but incomplete coverage/interruption/early start.
   - `INVALID`: unsafe preflight, corrupt evidence, unresolved exposure, or
     nondeterministic replay.
6. Chain run cards to the previous finalized card. If tamper-evident local
   attestation is required, add an HMAC signature with a separate local key;
   never commit the key.
7. Add a startup safety report that proves paper config, absence of order
   modules, token/app binding, quote access, active symbols, carried balances,
   process locks, and dashboard read-only behavior.

### Proposed P1 work

1. Add one local supervisor for recorder, engine, and read-only dashboard:
   recorder first, then proof of fresh ticks, then paper engine.
2. Add recorder and engine PID/lock metadata, heartbeats, restart budgets, and
   graceful end-of-day finalization.
3. Persist per-symbol exchange-to-receive latency and
   receive-to-decision-availability latency.
4. Calculate scheduled evaluation counts and rolling 10-minute fresh/stale/
   interrupted ratios per symbol. Warn and fail closed when stale evaluations
   exceed 10%; do not relax the six-second threshold during a session.
5. After reconnect, require fresh post-reconnect evidence for every required
   context input before another simulated trade.
6. Replace full-session recomputation with incremental, checkpointed bars and
   features while retaining a full independent end-of-day replay for proof.
7. Improve the dashboard with:
   - process, socket, subscription, and engine-consumption status;
   - per-symbol last tick and latency distribution;
   - next scheduled evaluation;
   - candidate rejection waterfall;
   - approved quantity, risk budget, stop distance, notional exposure;
   - fill deadline and rejection reason;
   - open paper positions and mark-to-market;
   - fee-view P&L with explicit “same trades, different costs” labeling.

### Proposed P2 research work

1. Freeze one master evaluation protocol before further tuning:
   chronological development, validation, purge, embargo, and untouched test
   dates.
2. Collect at least 20 independent full intraday sessions for engineering and
   substantially more before making statistical claims. Do not treat “10–20”
   as proof of profitability.
3. Obtain provenance-preserving historical intraday data through the documented
   FYERS History method or another licensed source. Never silently mix providers.
4. Pre-register no more than 12 variants per experiment. Candidate offline
   additions could include opening-range breakout, VWAP pullback, and
   time-based exit variants, but only with ablations and realistic costs.
5. Evaluate stability across days, volatility regimes, symbols, fee profiles,
   latency delays, and parameter neighborhoods.
6. Add multiple-testing controls, bootstrap confidence intervals, probability
   of backtest overfitting, turnover/capacity metrics, and drawdown constraints.
7. Keep the sealed test closed until one frozen candidate and its rejection
   thresholds are declared in advance.
8. Require profitable or at least non-negative evidence in both development and
   validation after all costs before any sealed-test review. This is a gate, not
   a promise that the strategy will work live.

### Proposed AI/news work

1. Keep all AI and news components in shadow mode only.
2. Timestamp each item at actual availability and test whether it adds
   incremental out-of-sample value over the deterministic baseline.
3. Never let an LLM choose quantity, bypass risk, call an order endpoint, or
   modify strategy files.
4. Use multiple reviewers only for offline critique, code review, anomaly
   explanation, and experiment design—not for majority-vote trade execution.
5. Store AI lessons as proposed hypotheses with evidence links and
   `PENDING_HUMAN_REVIEW`; do not automatically “reward” the model into changing
   its behavior.

## Requested independent-auditor output

The next auditor should:

1. Inspect the cited files rather than trusting this summary.
2. Return findings ordered by P0/P1/P2/P3, each with file and line evidence.
3. Confirm or refute every known defect above.
4. Explain the July 27 state/journal count mismatch.
5. Trace the 1980 exchange timestamps to their exact provider fields and add a
   safe quarantine design.
6. Verify there is no import path or dynamic mechanism capable of broker order
   placement.
7. Review the fill, stop, partial-fill, costs, and balance-carry-forward math.
8. Review look-ahead safety from exchange timestamp through
   decision-availability time.
9. Propose a minimal patch sequence with tests and rollback points.
10. Do not alter live thresholds, open sealed test data, access secrets, install
    a trading repository wholesale, or add an order-capable dependency.

## Evidence paths

- `README.md`
- `config/stage1.yaml`
- `config/cost_profiles.yaml`
- `config/evaluation_protocol.yaml`
- `config/intraday_variants.yaml`
- `src/stage1/adapters/fyers_market_data.py`
- `src/stage1/market/normalizer.py`
- `src/stage1/market/bar_builder.py`
- `src/stage1/market/features.py`
- `src/stage1/strategy/candidate_gate.py`
- `src/stage1/paper/replay.py`
- `src/stage1/paper/costs.py`
- `scripts/record_fyers.py`
- `scripts/run_live_paper.py`
- `scripts/run_control_room.py`
- `data/historical/prepared/research-report-2025-07-21_2026-07-21_rD-38ad7623ed051833.json`
- `data/historical/factor-research/factor-report-579f497df4447b8b.json`
- `data/evaluation/intraday-variant-lab-a6ae424eb4b6e23a.json`
- `data/decisions/date=2026-07-20/paper-replay-report-15806b85fbfbbd8a.json`
- `data/decisions/date=2026-07-22/paper-replay-report-7ed09ca8ed7a711a.json`
- `data/decisions/date=2026-07-24/paper-replay-report-fa626c0c86c74826.json`
- `data/decisions/date=2026-07-27/live-paper-events.jsonl`
- `state/live-paper-2026-07-22.json`
- `state/live-paper-2026-07-23.json`
- `state/live-paper-2026-07-24.json`
- `state/live-paper-2026-07-27.json` (active and provisional at snapshot)
- `state/stage1.sqlite3`

