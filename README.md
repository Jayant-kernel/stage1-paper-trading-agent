# Stage 1 AI Paper-Trading Agent

This repository implements Revision 1.2 of the Stage 1 architecture for an
NSE cash-equity, intraday, paper-only trading experiment.

The safety boundary is absolute: this package has no live-order adapter, no
broker order socket, and no route that can place a real order. FYERS is used
only for market data. Model output is never an executable instruction.

## Current milestone

Weeks 1-3 foundation plus the first Week 4 offline paper-replay slice:

- fixed ten-stock universe plus NIFTY 50 and India VIX;
- typed configuration and event schemas;
- secret loading and redaction boundary;
- FYERS manual OAuth helper (no password, PIN, TOTP, or 2FA automation);
- FYERS data-only WebSocket recorder;
- normalized ticks with exchange and local receive timestamps;
- FYERS control/status frames counted separately from market ticks;
- append-only quarantine for malformed or out-of-universe messages;
- immutable Parquet chunks and SQLite manifests;
- thread-safe tick, control, quarantine, and Parquet-record counters;
- environment/runtime identity reporting;
- synthetic/recorded contract fixtures and tests.
- causality-safe one-minute bars and past-only market features;
- frozen deterministic candidate rules with cooldown and ranking;
- deterministic risk sizing with a 1.2 ATR stop and portfolio limits;
- local next-observable-quote paper fills, partial fills, stop exits and 15:15 flattening;
- versioned Shoonya and Zerodha intraday cost ledgers;
- after-cost outcome and reward records that remain in human-review-pending state.

Live FYERS capture requires the user's daily local OAuth authorization. Tokens
stay in the ignored `.env`; development and saved-day replay do not require them.

The reward ledger does not update model weights, prompts, rules or memory. A
positive after-cost outcome receives bounded bonus points; a negative outcome
receives explicit mistake tags. Every record remains ineligible for model memory
until a later human-reviewed, versioned, held-out comparison approves it.

Historical research is deliberately separate from the live baseline. The
historical preparation command stores each FYERS response and normalized candle
chunk immutably, creates chronological train/validation/test splits with session
embargoes, and ranks the ten trade symbols using training-period data only. Its
"research suitability" score measures coverage, liquidity, continuity and
volatility stability; it is not a promise of profit.

The research path also has an explicit, daily-only Yahoo public-chart fallback.
It needs no API key, preserves the raw HTTP response and provider symbol, and
refuses to prepare research if even one of the twelve requested symbols is
missing or under the configured row minimum. Yahoo data is never mixed into the
FYERS live recorder or presented as broker-grade intraday data.

The factor laboratory contains only six explainable factors. Before evaluation,
it modifies all future prices and volumes and proves that every earlier factor
value remains byte-for-byte equivalent within strict floating-point tolerance.
Evaluation uses deterministic shuffled controls and TRAIN/VALIDATION evidence;
TEST metrics stay sealed. A validated factor remains ineligible for live rules
until a separate human-reviewed versioned change is made.

Grok and every other paid/free cloud service remain optional and disabled. The
deterministic core, local Qwen path and official NSE/SEBI/RBI news path must work
without a cloud API.

For a live deterministic paper session, keep the FYERS recorder running and
start `scripts/run_live_paper.py`. The engine uses a per-session process lock,
starts decisions only from its actual launch timestamp, carries forward the two
paper-cost ledgers, journals new events append-only, and finalizes after the
configured shutdown. It cannot access a broker order API.

## Setup

From PowerShell in `C:\Users\Jayant\Documents\stage1-paper-agent`:

```powershell
python -m venv .venv
.\.venv\Scripts\python.exe -m pip install --require-hashes -r requirements.lock
.\.venv\Scripts\python.exe -m pip install -e . --no-deps --no-build-isolation
```

To intentionally regenerate the lock after editing `requirements.in`, use the
already locked `pip-tools` installation:

```powershell
.\.venv\Scripts\python.exe -m piptools compile --generate-hashes --allow-unsafe --strip-extras --output-file requirements.lock requirements.in
```

Verify the offline environment:

```powershell
.\.venv\Scripts\python.exe scripts\verify_environment.py
.\.venv\Scripts\python.exe scripts\scan_secrets.py
.\.venv\Scripts\python.exe -m pytest
```

## Manual FYERS authorization

1. Create an API v3 app in the FYERS API dashboard.
2. Use `http://127.0.0.1:8765/callback` as the redirect URI.
3. Copy `.env.example` to `.env` and enter the client ID and app secret
   locally. Do not share that file.
4. Generate a login URL:

```powershell
.\.venv\Scripts\python.exe scripts\authenticate_fyers.py start
```

5. Open the displayed URL, complete the normal FYERS login and 2FA yourself,
   then run:

```powershell
.\.venv\Scripts\python.exe scripts\authenticate_fyers.py finish
```

The second command securely prompts for the complete callback URL, verifies the
OAuth state, exchanges the authorization code, and updates only the untracked
`.env` file. It never prints the token.

When authorization is complete, start data-only recording:

```powershell
.\.venv\Scripts\python.exe scripts\record_fyers.py
```

Raw normalized events are written as new immutable files under
`data/raw/ticks/`; restarts never overwrite prior chunks.
The recorder stays running, retains SDK reconnection behavior, and flushes its
pending Parquet batch during clean shutdown. Press `Ctrl+C` once to stop it,
then wait up to 15 seconds for `recorder_stopped` and the PowerShell prompt.
Structured progress events report `received_ticks`, `control_messages`,
`quarantined_messages`, and `written_parquet_records`.
Parquet serialization uses the locked Polars engine so it remains compatible
with Windows environments that enforce signed native-module policy.

The receive-latency freshness limit is six seconds. This was frozen for future
sessions after the July 20-23 calibration showed normal FYERS delivery moving
from a 2.7-second median to roughly 5.0 seconds; genuine multi-second and feed
outage gaps remain flagged. A day used to choose this limit is diagnostic and
is not eligible for strategy promotion.

After-hours FYERS snapshots may represent an unavailable quote side as price
zero and quantity zero. The normalizer retains those values in the immutable
raw message while exposing the normalized price and quantity as `None`.
Zero price with positive quantity remains inconsistent and is quarantined.

Prepare one year of one-minute historical research data after daily FYERS
authorization. The default end date is yesterday, so incomplete live data is
never mixed into research:

```powershell
.\.venv\Scripts\python.exe scripts\prepare_historical_research.py
```

The command is resumable. Verified chunks are reused without another API call,
all 12 symbols are retained, and a training-only ranking marks a small starting
subset for later backtesting. It does not alter the frozen live candidate rules
or place any order.

If FYERS Historical is unavailable, prepare one year of daily research from the
free public fallback without changing `.env` or requesting any trading
permission:

```powershell
.\.venv\Scripts\python.exe scripts\prepare_historical_research.py --source yahoo --resolution D
```

Use `--source auto --resolution D` to try FYERS first and visibly fall back to
Yahoo. The output names the effective provider; a fallback is never silent.

After a prepared dataset exists, run the causal factor audit and strict
TRAIN/VALIDATION comparison:

```powershell
.\.venv\Scripts\python.exe scripts\run_factor_research.py
```

This writes immutable factor inputs, summaries and a report while keeping all
TEST performance hidden. It does not train a local model, alter the live
baseline, or make any real/paper order by itself.

Compare the frozen intraday baseline, two conservative filters, an early-only
window, and two required component ablations on the explicitly declared saved
sessions:

```powershell
.\.venv\Scripts\python.exe scripts\run_intraday_variant_lab.py
```

The protocol in `config/intraday_variants.yaml` fixes the session roles,
content-addressed inputs, six variants, both broker cost profiles, and all six
artificial delays before evaluation. The report remains `INSUFFICIENT_SESSIONS`
and cannot promote a variant or change the live configuration while only one
development and one validation day exist. The TEST window remains sealed.

Validate any completed recording date against its immutable manifests, frozen
symbol universe, quote invariants, and full-session time window:

```powershell
.\.venv\Scripts\python.exe scripts\validate_recording.py 2026-07-20
```

The report distinguishes an intact but incomplete `PARTIAL_SESSION` from a
complete `PASS` and an integrity `FAIL`.

If the live paper engine could not finalize because the feed was stale at
shutdown, recover immutable bars/features and a no-trade result offline:

```powershell
.\.venv\Scripts\python.exe scripts\run_live_paper.py --session-date YYYY-MM-DD --finalize-only
```

## Outbound-only Telegram setup

Run the local PowerShell setup script after sending `/start` in the bot's
private chat:

```powershell
& "C:\Users\Jayant\Documents\stage1-paper-agent\scripts\setup_telegram.ps1"
```

The script prompts for the bot token with hidden input, removes webhook
delivery without dropping the pending `/start`, detects exactly one private
chat, sends `Stage 1 paper agent alerts connected.`, and atomically stores the
token and chat ID in the ignored `.env` file. The runtime Telegram client is
send-only: it has no polling, webhook, message handler, or command handler.

The adapter contract was verified on 16 July 2026 against the
[official FYERS authentication flow](https://support.fyers.in/portal/en/kb/articles/how-does-the-authentication-and-login-flow-work-for-user-apps-on-fyers),
[official data-WebSocket guidance](https://support.fyers.in/portal/en/kb/articles/how-can-i-use-the-data-websocket-in-api-v3-to-access-real-time-data),
and `fyers-apiv3` 3.1.14.

## Non-negotiable controls

- `mode: paper` and `live_order_endpoints_enabled: false` are schema invariants.
- Missing, invalid, or unrecognized messages are rejected or quarantined.
- Exchange and receive timestamps are always distinct stored fields.
- Secrets are represented by `SecretStr`, redacted from diagnostic text, and
  excluded from configuration models sent downstream.
- The recorder imports only the FYERS data WebSocket client.
- Qwen, FinBERT, news, debate, memory, and cloud challengers remain disabled
  until their architecture gates are reached.
