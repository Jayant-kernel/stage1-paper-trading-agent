"use client";

import { useCallback, useEffect, useMemo, useState } from "react";

type ComponentState = {
  recorder: string;
  paperEngine: string;
  fyers: string;
  bridge: string;
};

type SymbolState = {
  symbol: string;
  shortName: string;
  status: string;
  ltp: number | null;
  bid: number | null;
  ask: number | null;
  exchangeTs: string | null;
  receivedTs: string | null;
  tickAgeSec: number | null;
  latencyMs: number | null;
  latencyP50Ms: number | null;
  files: number;
};

type Ledger = {
  profile: string;
  starting: number | null;
  ending: number | null;
  pnl: number | null;
};

type Trade = {
  candidateId: string;
  symbol: string;
  direction: string;
  quantity: number;
  entryPrice: number | null;
  entryAt: string | null;
  exitPrice: number | null;
  exitAt: string | null;
  exitReason: string | null;
  status: string;
  pnl: Record<string, number>;
};

type EventItem = {
  id: string;
  type: string;
  recordedAt: string;
  payload: Record<string, unknown>;
};

type LiveTarget = {
  mode: string;
  candidateId: string | null;
  symbol: string | null;
  direction: string | null;
  score: number | null;
  candidateAt: string | null;
  riskStatus: string;
  quantity: number;
  referencePrice: number | null;
  stopPrice: number | null;
  simulatedExposure: number;
  riskAtStop: number;
  fillStatus: string;
  reasons: string[];
  explanation: string;
};

type Snapshot = {
  generatedAt: string;
  session: {
    date: string;
    status: string;
    startedAt: string | null;
    finishedAt: string | null;
    heartbeatAt: string | null;
    heartbeatAgeSec: number | null;
    baselineVersion: string;
    runCard: string | null;
    warmup: {
      active: boolean;
      completeBars: number;
      featureSnapshots: number;
      requiredBarsPerSymbol: number;
      estimatedBarsPerSymbol: number;
      estimatedMinutesRemaining: number;
      progressPct: number;
    };
  };
  market: {
    phase: string;
    isLiveWindow: boolean;
    nowIst: string;
    openAt: string;
    entryCutoffAt: string;
    flattenAt: string;
    shutdownAt: string;
  };
  universe: {
    tradableSymbols: string[];
    contextSymbols: string[];
    tradableCount: number;
    contextCount: number;
    totalCount: number;
  };
  limits: {
    startingCash: number;
    maxOpenPositions: number;
    maxRoundTripsPerDay: number;
    riskPerTradeFraction: number;
    maxPositionFraction: number;
    currentRiskBudget: number;
    currentMaxExposure: number;
  };
  components: ComponentState;
  safety: {
    paperOnly: boolean;
    liveOrderEndpointsEnabled: boolean;
    brokerOrdersPossible: boolean;
    qwenEnabled: boolean;
    cloudModelsEnabled: boolean;
    newsDecisionsEnabled: boolean;
    automaticLearningEnabled: boolean;
    outboundAlertsOnly: boolean;
  };
  metrics: {
    rawTickFiles: number;
    manifestRows: number;
    quarantined: number;
    candidates: number;
    approved: number;
    held: number;
    entryFills: number;
    completedTrades: number;
    openPositions: number;
    latestTickAgeSec: number | null;
    freshSymbols: number;
    staleSymbols: number;
  };
  ledgers: Ledger[];
  target: LiveTarget;
  pipeline: Array<{ name: string; status: string; count: number | null }>;
  symbols: SymbolState[];
  trades: Trade[];
  events: EventItem[];
  sessions: Array<{
    date: string;
    status: string;
    trades: number;
    shoonyaPnl: number | null;
    zerodhaPnl: number | null;
  }>;
};

const API_URL = "http://127.0.0.1:8766/api/snapshot";

function compactNumber(value: number | null | undefined) {
  if (value === null || value === undefined) return "—";
  return new Intl.NumberFormat("en-IN", {
    notation: value > 9999 ? "compact" : "standard",
    maximumFractionDigits: value > 9999 ? 1 : 0,
  }).format(value);
}

function money(value: number | null | undefined) {
  if (value === null || value === undefined) return "—";
  return new Intl.NumberFormat("en-IN", {
    style: "currency",
    currency: "INR",
    minimumFractionDigits: 2,
    maximumFractionDigits: 2,
  }).format(value);
}

function clock(value: string | null | undefined, includeDate = false) {
  if (!value) return "—";
  const parsed = new Date(value);
  if (Number.isNaN(parsed.getTime())) return "—";
  return new Intl.DateTimeFormat("en-IN", {
    timeZone: "Asia/Kolkata",
    day: includeDate ? "2-digit" : undefined,
    month: includeDate ? "short" : undefined,
    hour: "2-digit",
    minute: "2-digit",
    second: "2-digit",
    hour12: false,
  }).format(parsed);
}

function titleCase(value: string) {
  return value.toLowerCase().replaceAll("_", " ").replace(/\b\w/g, (letter) => letter.toUpperCase());
}

function tone(value: string) {
  const normalized = value.toUpperCase();
  if (["LIVE", "READY", "FRESH", "FINALIZED", "CONNECTED_INFERRED", "PAPER_ACTIVE"].includes(normalized)) {
    return "good";
  }
  if (["WAITING", "WARMING_UP", "HISTORICAL", "SESSION_CLOSED", "CLOSED", "MARKET_CLOSED"].includes(normalized)) {
    return "quiet";
  }
  if (["STALE", "INTERRUPTED", "DISCONNECTED", "INVALID", "FAILED"].includes(normalized)) {
    return "bad";
  }
  return "warn";
}

function StatusDot({ label, value }: { label: string; value: string }) {
  return (
    <div className="status-item">
      <span className={`status-dot ${tone(value)}`} aria-hidden="true" />
      <span className="status-label">{label}</span>
      <span className="status-value">{titleCase(value)}</span>
    </div>
  );
}

function MetricCard({
  label,
  value,
  note,
  accent = "neutral",
}: {
  label: string;
  value: string;
  note: string;
  accent?: "neutral" | "green" | "amber" | "red";
}) {
  return (
    <article className={`metric-card metric-${accent}`}>
      <p>{label}</p>
      <strong>{value}</strong>
      <span>{note}</span>
    </article>
  );
}

function eventSummary(event: EventItem) {
  const payload = event.payload;
  const symbol = String(payload.symbol || "").replace("NSE:", "").replace("-EQ", "");
  if (event.type === "candidates") {
    return `${symbol} ${titleCase(String(payload.side || "candidate"))} · score ${Number(payload.score || 0).toFixed(3)}`;
  }
  if (event.type === "risk_decisions") {
    const reasons = Array.isArray(payload.reasons) ? payload.reasons.join(", ") : "";
    return `${symbol} ${String(payload.status || "DECIDED")} · ${reasons || "risk gate evaluated"}`;
  }
  if (event.type === "fills") {
    return `${symbol} ${payload.purpose || "PAPER"} ${payload.action || ""} ${payload.quantity || ""} @ ${payload.fill_price || "—"}`;
  }
  if (event.type === "outcomes") {
    return `${symbol} ${String(payload.profile || payload.cost_profile || "")} · net ${money(Number(payload.net_pnl || 0))}`;
  }
  if (event.type === "rewards") {
    return `${String(payload.label || "Review pending")} · ${String(payload.review_status || "")}`;
  }
  return symbol || "Operational event";
}

export default function Home() {
  const [snapshot, setSnapshot] = useState<Snapshot | null>(null);
  const [selectedDate, setSelectedDate] = useState("");
  const [error, setError] = useState<string | null>(null);
  const [loading, setLoading] = useState(true);
  const [eventFilter, setEventFilter] = useState("all");
  const [expandedEvent, setExpandedEvent] = useState<string | null>(null);

  const loadSnapshot = useCallback(async () => {
    try {
      const query = selectedDate ? `?date=${encodeURIComponent(selectedDate)}` : "";
      const response = await fetch(`${API_URL}${query}`, { cache: "no-store" });
      if (!response.ok) throw new Error(`Local bridge returned ${response.status}`);
      const next = (await response.json()) as Snapshot;
      setSnapshot(next);
      setError(null);
    } catch {
      setError("The private local data bridge is offline.");
    } finally {
      setLoading(false);
    }
  }, [selectedDate]);

  useEffect(() => {
    const initial = window.setTimeout(() => void loadSnapshot(), 0);
    const timer = window.setInterval(() => void loadSnapshot(), 2000);
    return () => {
      window.clearTimeout(initial);
      window.clearInterval(timer);
    };
  }, [loadSnapshot]);

  const filteredEvents = useMemo(() => {
    if (!snapshot) return [];
    return eventFilter === "all"
      ? snapshot.events
      : snapshot.events.filter((event) => event.type === eventFilter);
  }, [eventFilter, snapshot]);

  if (!snapshot && loading) {
    return (
      <main className="boot-screen">
        <div className="boot-mark"><span /><span /><span /></div>
        <p>Connecting to the local Stage 1 control room…</p>
      </main>
    );
  }

  if (!snapshot) {
    return (
      <main className="offline-screen">
        <div className="offline-panel">
          <p className="eyebrow">STAGE 1 / LOCAL CONTROL ROOM</p>
          <h1>The visual layer is ready.<br />The private bridge is offline.</h1>
          <p>Run the one control-room launcher from the canonical project. No trading credentials are shown here.</p>
          <button onClick={() => void loadSnapshot()}>Retry connection</button>
          <code>.\scripts\start_control_room.ps1</code>
        </div>
      </main>
    );
  }

  const safetyPass =
    snapshot.safety.paperOnly &&
    !snapshot.safety.liveOrderEndpointsEnabled &&
    !snapshot.safety.brokerOrdersPossible;
  const latestLatency = snapshot.symbols
    .map((symbol) => symbol.latencyMs)
    .filter((value): value is number => value !== null)
    .sort((a, b) => a - b);
  const p50Latency = latestLatency.length ? latestLatency[Math.floor(latestLatency.length / 2)] : null;
  const p95Latency = latestLatency.length
    ? latestLatency[Math.min(latestLatency.length - 1, Math.floor(latestLatency.length * 0.95))]
    : null;
  const openTrades = snapshot.trades.filter((trade) => trade.status === "OPEN");
  const latestTrades = snapshot.trades.slice(0, 2);
  const isTradingNow =
    snapshot.market.isLiveWindow &&
    snapshot.components.paperEngine === "LIVE" &&
    snapshot.components.recorder === "LIVE" &&
    snapshot.components.fyers === "CONNECTED_INFERRED";
  const engineIsOn =
    snapshot.market.isLiveWindow &&
    snapshot.components.paperEngine === "LIVE";
  const warmupActive = engineIsOn && snapshot.session.warmup.active;
  const readyToEvaluate = !warmupActive;
  const isActivelyEvaluating = isTradingNow && readyToEvaluate;
  const sessionHeadline = warmupActive
    ? "ENGINE IS WARMING UP"
    : isActivelyEvaluating
    ? "PAPER TRADING IS LIVE"
    : engineIsOn
      ? "PAPER ENGINE IS ON HOLD"
      : "NOT TRADING RIGHT NOW";
  const sessionBadge = warmupActive
    ? `${snapshot.session.warmup.estimatedBarsPerSymbol} / ${snapshot.session.warmup.requiredBarsPerSymbol} BARS EACH`
    : isActivelyEvaluating
    ? "LIVE PAPER SESSION"
    : engineIsOn
      ? "FAIL-CLOSED HOLD"
      : "SESSION INACTIVE";
  const cleanSymbol = (symbol: string) =>
    symbol.replace("NSE:", "").replace("-EQ", "").replace("-INDEX", "");

  return (
    <main className="control-room">
      <header className="topbar">
        <div className="brand">
          <div className="brand-mark" aria-hidden="true"><span /><span /><span /></div>
          <div>
            <p>STAGE 1</p>
            <h1>Paper Operations</h1>
          </div>
        </div>
        <div className="market-clock">
          <span>{snapshot.session.date}</span>
          <strong>{clock(snapshot.market.nowIst)}</strong>
          <em>IST · {titleCase(snapshot.market.phase)}</em>
        </div>
        <div className={`safety-seal ${safetyPass ? "pass" : "fail"}`}>
          <span aria-hidden="true">{safetyPass ? "◆" : "!"}</span>
          <div>
            <small>EXECUTION BOUNDARY</small>
            <strong>{safetyPass ? "PAPER ONLY" : "SAFETY FAILED"}</strong>
          </div>
        </div>
      </header>

      <section className="status-rail" aria-label="Live component status">
        <StatusDot label="Recorder" value={snapshot.components.recorder} />
        <StatusDot label="Paper engine" value={snapshot.components.paperEngine} />
        <StatusDot label="FYERS feed" value={snapshot.components.fyers} />
        <StatusDot label="Local bridge" value={snapshot.components.bridge} />
        <div className="rail-actions">
          {error && <span className="connection-warning">{error}</span>}
          <label>
            <span>Session</span>
            <select
              value={selectedDate || snapshot.session.date}
              onChange={(event) => setSelectedDate(event.target.value)}
              aria-label="Select session date"
            >
              {snapshot.sessions.map((session) => (
                <option key={session.date} value={session.date}>{session.date}</option>
              ))}
            </select>
          </label>
          <button onClick={() => void loadSnapshot()} aria-label="Refresh now">
            ↻ <span>Refresh</span>
          </button>
        </div>
      </section>

      <div className="workspace">
        <section className="activity-overview" aria-label="Current paper trading activity">
          <article className={`now-card ${isActivelyEvaluating ? "now-live" : warmupActive ? "now-warmup" : "now-closed"}`}>
            <div className="now-heading">
              <div>
                <p className="eyebrow">WHAT IS HAPPENING NOW</p>
                <h2>{sessionHeadline}</h2>
                <p className="now-explanation">
                  {warmupActive
                    ? `The feed and engine are healthy. The frozen feature pipeline requires ${snapshot.session.warmup.requiredBarsPerSymbol} complete one-minute bars for every symbol before evaluating candidates. No simulated money can be allocated during warm-up.`
                    : isActivelyEvaluating
                    ? "The deterministic engine is evaluating live FYERS data. A simulated trade appears only after every data-quality and risk check passes."
                    : engineIsOn
                      ? "The engine process is running, but current market data is not healthy enough to permit a simulated decision. Exposure remains zero until the feed recovers."
                      : `This is the saved ${snapshot.session.date} session. The market is ${titleCase(snapshot.market.phase).toLowerCase()} and there are no live broker orders.`}
                </p>
              </div>
              <span className={`large-state ${isActivelyEvaluating ? "good" : warmupActive ? "warn" : engineIsOn ? "bad" : "quiet"}`}>
                <i />
                {sessionBadge}
              </span>
            </div>

            {warmupActive && (
              <div className="warmup-progress" aria-label="Feature warm-up progress">
                <div>
                  <span>FEATURE WARM-UP</span>
                  <strong>{snapshot.session.warmup.progressPct.toFixed(1)}%</strong>
                </div>
                <div className="warmup-track" aria-hidden="true">
                  <i style={{ width: `${snapshot.session.warmup.progressPct}%` }} />
                </div>
                <small>
                  Approximately {snapshot.session.warmup.estimatedMinutesRemaining} market minutes remaining ·
                  {" "}{snapshot.session.warmup.completeBars} complete bars across {snapshot.universe.totalCount} symbols
                </small>
              </div>
            )}

            <div className="now-numbers">
              <div>
                <span>Stocks eligible to trade</span>
                <strong>{snapshot.universe.tradableCount}</strong>
                <small>not 12 simultaneous trades</small>
              </div>
              <div>
                <span>Context indicators</span>
                <strong>{snapshot.universe.contextCount}</strong>
                <small>observed, never traded</small>
              </div>
              <div className={openTrades.length ? "attention" : ""}>
                <span>Open paper positions</span>
                <strong>{openTrades.length}</strong>
                <small>maximum {snapshot.limits.maxOpenPositions} at once</small>
              </div>
              <div>
                <span>Completed today</span>
                <strong>{snapshot.metrics.completedTrades}</strong>
                <small>maximum {snapshot.limits.maxRoundTripsPerDay} round trips</small>
              </div>
            </div>

            <div className="universe-blocks">
              <div>
                <span className="block-label">10 TRADEABLE NSE STOCKS</span>
                <div className="symbol-chips">
                  {snapshot.universe.tradableSymbols.map((symbol) => (
                    <span
                      className={snapshot.target.symbol === symbol ? "active-target" : ""}
                      key={symbol}
                    >
                      {cleanSymbol(symbol)}
                    </span>
                  ))}
                </div>
              </div>
              <div className="context-block">
                <span className="block-label">CONTEXT ONLY · NEVER BOUGHT OR SOLD</span>
                <div className="symbol-chips context-chips">
                  {snapshot.universe.contextSymbols.map((symbol) => (
                    <span key={symbol}>{cleanSymbol(symbol)}</span>
                  ))}
                </div>
              </div>
            </div>
          </article>

          <aside className="funds-card">
            <div className="funds-heading">
              <div>
                <p className="eyebrow">VIRTUAL FUNDS · PAPER MONEY</p>
                <h2>Two cost views, same trades</h2>
              </div>
              <span>DO NOT ADD TOGETHER</span>
            </div>
            <p className="funds-note">
              Shoonya and Zerodha are parallel fee simulations over the same paper trades—not two separate portfolios.
            </p>
            <div className="funds-ledgers">
              {snapshot.ledgers.map((ledger) => (
                <article key={ledger.profile}>
                  <span>{titleCase(ledger.profile)} cost model</span>
                  <strong>{money(ledger.ending ?? ledger.starting)}</strong>
                  <small className={(ledger.pnl ?? 0) >= 0 ? "positive" : "negative"}>
                    Session P&amp;L {money(ledger.pnl)}
                  </small>
                  <em>Opened with {money(ledger.starting)}</em>
                </article>
              ))}
            </div>
          </aside>
        </section>

        <section className="target-overview" aria-label="Live paper target and sizing">
          <article className={`panel target-panel target-${tone(snapshot.target.mode)}`}>
            <div className="panel-heading target-heading">
              <div>
                <p className="eyebrow">LIVE TARGET AND PAPER SIZING</p>
                <h2>
                  {snapshot.target.symbol
                    ? `${snapshot.target.direction} ${cleanSymbol(snapshot.target.symbol)}`
                    : "NO STOCK TARGETED"}
                </h2>
              </div>
              <span className={`state-pill ${tone(snapshot.target.mode)}`}>
                {titleCase(snapshot.target.mode)}
              </span>
            </div>

            <div className="target-grid">
              <div className="target-primary">
                <span>Simulated exposure now</span>
                <strong>{money(snapshot.target.simulatedExposure)}</strong>
                <small>
                  {snapshot.target.quantity > 0
                    ? `${snapshot.target.quantity} shares at ${money(snapshot.target.referencePrice)}`
                    : "0 shares - no paper allocation"}
                </small>
              </div>
              <dl className="target-facts">
                <div><dt>Stock</dt><dd>{snapshot.target.symbol ? cleanSymbol(snapshot.target.symbol) : "NONE"}</dd></div>
                <div><dt>Direction</dt><dd>{snapshot.target.direction || "NONE"}</dd></div>
                <div><dt>Candidate score</dt><dd>{snapshot.target.score === null ? "-" : snapshot.target.score.toFixed(4)}</dd></div>
                <div><dt>Risk decision</dt><dd>{titleCase(snapshot.target.riskStatus)}</dd></div>
                <div><dt>Reference price</dt><dd>{money(snapshot.target.referencePrice)}</dd></div>
                <div><dt>Paper stop</dt><dd>{money(snapshot.target.stopPrice)}</dd></div>
                <div><dt>Risk at stop</dt><dd>{money(snapshot.target.riskAtStop)}</dd></div>
                <div><dt>Fill status</dt><dd>{titleCase(snapshot.target.fillStatus)}</dd></div>
              </dl>
            </div>

            <div className="target-explanation">
              <strong>{snapshot.target.explanation}</strong>
              <span>
                {snapshot.target.reasons.length
                  ? snapshot.target.reasons.map(titleCase).join(" / ")
                  : "No gate failure recorded."}
              </span>
            </div>
          </article>

          <aside className="panel sizing-rules">
            <div className="panel-heading compact-heading">
              <div>
                <p className="eyebrow">FROZEN RISK LIMITS</p>
                <h2>How much can be allocated</h2>
              </div>
            </div>
            <dl>
              <div>
                <dt>Maximum simulated exposure per position</dt>
                <dd>{money(snapshot.limits.currentMaxExposure)}</dd>
                <small>{(snapshot.limits.maxPositionFraction * 100).toFixed(2)}% of current primary paper equity</small>
              </div>
              <div>
                <dt>Maximum planned loss at stop</dt>
                <dd>{money(snapshot.limits.currentRiskBudget)}</dd>
                <small>{(snapshot.limits.riskPerTradeFraction * 100).toFixed(2)}% risk budget per paper trade</small>
              </div>
              <div>
                <dt>Concurrent paper positions</dt>
                <dd>{openTrades.length} / {snapshot.limits.maxOpenPositions}</dd>
                <small>Never broker money; simulated ledger only</small>
              </div>
            </dl>
          </aside>
        </section>

        <section className="positions-overview">
          <article className="panel open-positions-panel">
            <div className="panel-heading compact-heading">
              <div>
                <p className="eyebrow">CURRENT SIMULATED HOLDINGS</p>
                <h2>Open paper positions</h2>
              </div>
              <span className="count-badge">{openTrades.length}</span>
            </div>
            {openTrades.length === 0 ? (
              <div className="clear-position-state">
                <span>0</span>
                <div>
                  <strong>No stock is currently held</strong>
                  <p>The agent has no open paper position and no real-money position.</p>
                </div>
              </div>
            ) : (
              <div className="open-position-list">
                {openTrades.map((trade) => (
                  <article key={trade.candidateId}>
                    <span className={trade.direction === "LONG" ? "long" : "short"}>{trade.direction}</span>
                    <strong>{cleanSymbol(trade.symbol)}</strong>
                    <em>{trade.quantity} shares</em>
                    <small>Entry {trade.entryPrice ?? "—"}</small>
                  </article>
                ))}
              </div>
            )}
          </article>

          <article className="panel recent-trades-panel">
            <div className="panel-heading compact-heading">
              <div>
                <p className="eyebrow">EXACT PAPER TRADES</p>
                <h2>Most recent simulated trades</h2>
              </div>
              <span>{snapshot.trades.length} total</span>
            </div>
            <div className="recent-trade-list">
              {latestTrades.length === 0 && (
                <p className="empty-state compact-empty">No simulated trade passed every gate.</p>
              )}
              {latestTrades.map((trade) => (
                <article key={trade.candidateId}>
                  <div>
                    <span className={trade.direction === "LONG" ? "long" : "short"}>{trade.direction}</span>
                    <strong>{cleanSymbol(trade.symbol)}</strong>
                    <em>{trade.status}</em>
                  </div>
                  <p>{trade.quantity} shares · {trade.entryPrice ?? "—"} → {trade.exitPrice ?? "OPEN"}</p>
                  <footer>
                    <span>{trade.exitReason || "Position open"}</span>
                    {Object.entries(trade.pnl).map(([profile, pnl]) => (
                      <span key={profile}>{titleCase(profile)} <b className={pnl >= 0 ? "positive" : "negative"}>{money(pnl)}</b></span>
                    ))}
                  </footer>
                </article>
              ))}
            </div>
          </article>
        </section>

        <section className="hero-grid">
          <article className="panel pipeline-panel">
            <div className="panel-heading">
              <div>
                <p className="eyebrow">LIVE WORKFLOW</p>
                <h2>Decision pipeline</h2>
              </div>
              <span className={`state-pill ${tone(snapshot.session.status)}`}>
                {titleCase(snapshot.session.status)}
              </span>
            </div>
            <div className="pipeline">
              {snapshot.pipeline.map((step, index) => (
                <div className="pipeline-segment" key={step.name}>
                  <div className={`pipeline-node node-${tone(step.status)}`}>
                    <span>{String(index + 1).padStart(2, "0")}</span>
                    <strong>{step.name}</strong>
                    <small>{step.count === null ? titleCase(step.status) : compactNumber(step.count)}</small>
                  </div>
                  {index < snapshot.pipeline.length - 1 && <i aria-hidden="true">→</i>}
                </div>
              ))}
            </div>
            <div className="pipeline-footer">
              <div>
                <span>Frozen baseline</span>
                <strong>{snapshot.session.baselineVersion}</strong>
              </div>
              <div>
                <span>Latest engine heartbeat</span>
                <strong>{clock(snapshot.session.heartbeatAt, true)}</strong>
              </div>
              <div>
                <span>Immutable run card</span>
                <strong>{snapshot.session.runCard ? "Generated" : "Pending finalization"}</strong>
              </div>
            </div>
          </article>

          <aside className="panel safety-panel">
            <div className="panel-heading">
              <div>
                <p className="eyebrow">FAIL-CLOSED CONTROLS</p>
                <h2>Safety boundary</h2>
              </div>
              <span className="shield-glyph" aria-hidden="true">◇</span>
            </div>
            <div className="safety-list">
              {[
                ["Paper-only mode", snapshot.safety.paperOnly],
                ["Broker orders impossible", !snapshot.safety.brokerOrdersPossible],
                ["Live order endpoints off", !snapshot.safety.liveOrderEndpointsEnabled],
                ["Qwen decisions off", !snapshot.safety.qwenEnabled],
                ["Cloud models off", !snapshot.safety.cloudModelsEnabled],
                ["News decisions off", !snapshot.safety.newsDecisionsEnabled],
                ["Automatic learning off", !snapshot.safety.automaticLearningEnabled],
                ["Telegram outbound-only", snapshot.safety.outboundAlertsOnly],
              ].map(([label, passed]) => (
                <div key={String(label)}>
                  <span className={passed ? "check" : "cross"}>{passed ? "✓" : "×"}</span>
                  <p>{label}</p>
                  <strong>{passed ? "LOCKED" : "FAILED"}</strong>
                </div>
              ))}
            </div>
          </aside>
        </section>

        <section className="metrics-grid" aria-label="Session metrics">
          <MetricCard label="Raw market records" value={compactNumber(snapshot.metrics.manifestRows)} note={`${compactNumber(snapshot.metrics.rawTickFiles)} immutable files`} />
          <MetricCard label="Candidates" value={compactNumber(snapshot.metrics.candidates)} note={`${snapshot.metrics.held} held by risk gates`} accent="amber" />
          <MetricCard label="Risk approvals" value={compactNumber(snapshot.metrics.approved)} note={`${snapshot.metrics.entryFills} simulated entries`} accent="green" />
          <MetricCard label="Completed trades" value={compactNumber(snapshot.metrics.completedTrades)} note={`${snapshot.metrics.openPositions} positions currently open`} accent={snapshot.metrics.openPositions ? "amber" : "neutral"} />
          <MetricCard label="Data quarantine" value={compactNumber(snapshot.metrics.quarantined)} note="Preserved for review, never discarded" accent={snapshot.metrics.quarantined ? "amber" : "green"} />
          <MetricCard
            label="Feed latency"
            value={p50Latency === null ? "—" : `${p50Latency.toFixed(0)} ms`}
            note={p95Latency === null ? "No live measurement" : `P50 latest · P95 ${p95Latency.toFixed(0)} ms`}
            accent={p95Latency !== null && p95Latency > 6000 ? "red" : "green"}
          />
        </section>

        <section className="middle-grid">
          <article className="panel symbols-panel">
            <div className="panel-heading">
              <div>
                <p className="eyebrow">MARKET DATA DETAIL</p>
                <h2>Prices for 10 stocks + 2 context indicators</h2>
              </div>
              <div className="freshness-key">
                <span><i className="good" />{snapshot.metrics.freshSymbols} fresh</span>
                <span><i className="bad" />{snapshot.metrics.staleSymbols} stale</span>
              </div>
            </div>
            <div className="symbol-grid">
              {snapshot.symbols.map((symbol) => (
                <article className={`symbol-card symbol-${tone(symbol.status)}`} key={symbol.symbol}>
                  <div>
                    <span className={`mini-dot ${tone(symbol.status)}`} />
                    <strong>{symbol.shortName}</strong>
                    <em>{titleCase(symbol.status)}</em>
                  </div>
                  <p>{symbol.ltp === null ? "—" : symbol.ltp.toLocaleString("en-IN", { minimumFractionDigits: 2 })}</p>
                  <dl>
                    <div><dt>Tick age</dt><dd>{symbol.status === "CLOSED" || symbol.tickAgeSec === null ? "Closed" : `${symbol.tickAgeSec.toFixed(2)}s`}</dd></div>
                    <div><dt>Latency</dt><dd>{symbol.latencyMs === null ? "—" : `${symbol.latencyMs.toFixed(0)}ms`}</dd></div>
                    <div><dt>Exchange</dt><dd>{clock(symbol.exchangeTs)}</dd></div>
                    <div><dt>Received</dt><dd>{clock(symbol.receivedTs)}</dd></div>
                  </dl>
                </article>
              ))}
            </div>
          </article>

          <aside className="panel ledgers-panel">
            <div className="panel-heading">
              <div>
                <p className="eyebrow">VIRTUAL CAPITAL</p>
                <h2>Paper ledgers</h2>
              </div>
            </div>
            <div className="ledger-stack">
              {snapshot.ledgers.map((ledger) => {
                const pnl = ledger.pnl ?? 0;
                const movement = ledger.starting ? Math.min(Math.abs(pnl / ledger.starting) * 100 * 12, 100) : 0;
                return (
                  <article key={ledger.profile}>
                    <div className="ledger-head">
                      <span>{titleCase(ledger.profile)}</span>
                      <strong className={pnl >= 0 ? "positive" : "negative"}>{money(ledger.pnl)}</strong>
                    </div>
                    <div className="equity-value">{money(ledger.ending ?? ledger.starting)}</div>
                    <div className="equity-track">
                      <span className={pnl >= 0 ? "positive-bg" : "negative-bg"} style={{ width: `${Math.max(movement, 3)}%` }} />
                    </div>
                    <dl>
                      <div><dt>Opening</dt><dd>{money(ledger.starting)}</dd></div>
                      <div><dt>Current / close</dt><dd>{money(ledger.ending)}</dd></div>
                    </dl>
                  </article>
                );
              })}
            </div>
            <div className="session-strip">
              <span>Entry cutoff <strong>{clock(snapshot.market.entryCutoffAt)}</strong></span>
              <span>Flatten <strong>{clock(snapshot.market.flattenAt)}</strong></span>
              <span>Finalize <strong>{clock(snapshot.market.shutdownAt)}</strong></span>
            </div>
          </aside>
        </section>

        <section className="bottom-grid">
          <article className="panel events-panel">
            <div className="panel-heading events-heading">
              <div>
                <p className="eyebrow">POINT-IN-TIME JOURNAL</p>
                <h2>Decision stream</h2>
              </div>
              <div className="filter-tabs" role="tablist" aria-label="Filter decision events">
                {[
                  ["all", "All"],
                  ["candidates", "Candidates"],
                  ["risk_decisions", "Risk"],
                  ["fills", "Fills"],
                  ["outcomes", "Outcomes"],
                ].map(([value, label]) => (
                  <button
                    key={value}
                    className={eventFilter === value ? "active" : ""}
                    onClick={() => setEventFilter(value)}
                    role="tab"
                    aria-selected={eventFilter === value}
                  >
                    {label}
                  </button>
                ))}
              </div>
            </div>
            <div className="event-list">
              {filteredEvents.length === 0 && <p className="empty-state">No events in this view.</p>}
              {filteredEvents.map((event) => (
                <button
                  className={`event-row event-${event.type}`}
                  key={`${event.type}-${event.id}`}
                  onClick={() => setExpandedEvent(expandedEvent === event.id ? null : event.id)}
                >
                  <span className="event-time">{clock(event.recordedAt)}</span>
                  <span className="event-type">{titleCase(event.type)}</span>
                  <strong>{eventSummary(event)}</strong>
                  <span className="event-expand">{expandedEvent === event.id ? "−" : "+"}</span>
                  {expandedEvent === event.id && (
                    <pre>{JSON.stringify(event.payload, null, 2)}</pre>
                  )}
                </button>
              ))}
            </div>
          </article>

          <aside className="panel trades-panel">
            <div className="panel-heading">
              <div>
                <p className="eyebrow">SIMULATED EXECUTION</p>
                <h2>Paper trades</h2>
              </div>
              <span className="count-badge">{snapshot.trades.length}</span>
            </div>
            <div className="trade-list">
              {snapshot.trades.length === 0 && (
                <div className="empty-trades">
                  <span>○</span>
                  <strong>No valid paper fill</strong>
                  <p>The engine will never force a trade merely to show activity.</p>
                </div>
              )}
              {snapshot.trades.map((trade) => (
                <article key={trade.candidateId}>
                  <div className="trade-title">
                    <span className={trade.direction === "LONG" ? "long" : "short"}>{trade.direction}</span>
                    <strong>{trade.symbol.replace("NSE:", "").replace("-EQ", "")}</strong>
                    <em>{trade.status}</em>
                  </div>
                  <div className="trade-prices">
                    <span><small>ENTRY</small>{trade.entryPrice ?? "—"}</span>
                    <i>→</i>
                    <span><small>EXIT</small>{trade.exitPrice ?? "OPEN"}</span>
                  </div>
                  <div className="trade-pnls">
                    {Object.entries(trade.pnl).map(([profile, pnl]) => (
                      <span key={profile}>{titleCase(profile)} <strong className={pnl >= 0 ? "positive" : "negative"}>{money(pnl)}</strong></span>
                    ))}
                  </div>
                  <footer>
                    <span>{trade.quantity} shares</span>
                    <span>{trade.exitReason || "Position open"}</span>
                    <span>{clock(trade.entryAt)}</span>
                  </footer>
                </article>
              ))}
            </div>
          </aside>
        </section>
      </div>

      <footer className="app-footer">
        <span>Read-only local telemetry · refreshes every 2 seconds</span>
        <span>Generated {clock(snapshot.generatedAt, true)}</span>
        <span>Credentials and broker commands are never exposed</span>
      </footer>
    </main>
  );
}
