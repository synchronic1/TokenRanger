# TokenRanger Metrics — Centralized Token Consumption Measurement

Centralized metrics collector on CT 203 that measures token consumption
across both OpenClaw instances (pvet630 + r430a), with time-windowed
reporting, Prometheus export, and Grafana dashboards.

---

## Problem Statement

TokenRanger compresses context and logs char-level stats, but there is no way to:

1. **Measure actual token savings** — chars ≠ tokens; we need token estimates
2. **Track over time** — logs are ephemeral; no persistent time-series
3. **Correlate compression with API spend** — no link between TokenRanger
   reduction and the cloud LLM tokens actually billed
4. **Pull on-demand reports** — no CLI or endpoint for "show me last 24h usage"
5. **Compare nodes** — no unified view across pvet630 and r430a

OpenClaw already tracks API token usage internally (`session-cost-usage.ts`,
gateway `usage.status` / `sessions.usage.timeseries` endpoints) but this data
is not exposed alongside TokenRanger compression metrics.

---

## Infrastructure

| Host | IP | Role |
|------|-----|------|
| **CT 203** | 192.168.1.203 | Metrics collector (NEW), Prometheus (:9090), Grafana (:3001), Nginx (:80) |
| **pvet630** | 192.168.1.242 | OpenClaw + TokenRanger (3x GPU), emits metrics to CT 203 |
| **r430a** | 192.168.1.240 | OpenClaw + TokenRanger (CPU→GPU offload), emits metrics to CT 203 |

**Why CT 203**: Already hosts the monitoring stack. Single DB for both nodes.
Metrics survive node restarts/reimages. Zero write overhead on the compression
hot path — plugins emit fire-and-forget over LAN (sub-1ms RTT).

---

## Architecture

```
   pvet630 (192.168.1.242)                   r430a (192.168.1.240)
  ┌────────────────────────┐                ┌────────────────────────┐
  │  OpenClaw Gateway       │                │  OpenClaw Gateway       │
  │  ┌──────────────────┐  │                │  ┌──────────────────┐  │
  │  │ TokenRanger Hook  │  │                │  │ TokenRanger Hook  │  │
  │  │ before_agent_start│  │                │  │ before_agent_start│  │
  │  └────────┬─────────┘  │                │  └────────┬─────────┘  │
  │           │             │                │           │             │
  │  ┌────────▼─────────┐  │                │  ┌────────▼─────────┐  │
  │  │ TokenRanger Svc   │  │                │  │ TokenRanger Svc   │  │
  │  │ :8100 (compress)  │  │                │  │ :8100 (compress)  │  │
  │  └────────┬─────────┘  │                │  └────────┬─────────┘  │
  │           │             │                │           │             │
  │  ┌────────▼─────────┐  │                │  ┌────────▼─────────┐  │
  │  │ POST /emit        │──┼───────┐       │  │ POST /emit        │──┼──┐
  │  │ (fire & forget)   │  │       │       │  │ (fire & forget)   │  │  │
  │  └──────────────────┘  │       │       │  └──────────────────┘  │  │
  │                         │       │       │                         │  │
  │  OpenClaw Gateway :3000 │       │       │  OpenClaw Gateway :3000 │  │
  │  (usage.status,         │       │       │  (usage.status,         │  │
  │   sessions.usage.*)     │       │       │   sessions.usage.*)     │  │
  └────────────────────────┘       │       └────────────────────────┘  │
                                    │                                    │
                 ┌──────────────────▼────────────────────────────────────▼──┐
                 │                                                          │
                 │  CT 203 (192.168.1.203) — Metrics Collector              │
                 │                                                          │
                 │  ┌──────────────────────────────────────────────────┐   │
                 │  │  tokenranger-metrics  (FastAPI :8101)             │   │
                 │  │                                                    │   │
                 │  │  POST /emit           ← receives compression      │   │
                 │  │                         events from both nodes     │   │
                 │  │                                                    │   │
                 │  │  GET  /summary        → aggregated report (JSON)  │   │
                 │  │  GET  /events         → raw event list            │   │
                 │  │  GET  /export         → CSV download              │   │
                 │  │  GET  /compare        → node-vs-node comparison   │   │
                 │  │  GET  /metrics        → Prometheus text format    │   │
                 │  │                                                    │   │
                 │  │  ┌────────────────────────────────────────┐      │   │
                 │  │  │  SQLite (metrics.db)                    │      │   │
                 │  │  │  - compression_events (both nodes)      │      │   │
                 │  │  │  - api_usage_snapshots (polled)         │      │   │
                 │  │  │  - 30-day retention, auto-prune         │      │   │
                 │  │  └────────────────────────────────────────┘      │   │
                 │  │                                                    │   │
                 │  │  Background Tasks:                                │   │
                 │  │  - Usage poller: polls both gateways every 5min  │   │
                 │  │  - Pruner: daily cleanup of expired rows          │   │
                 │  └──────────────────────────────────────────────────┘   │
                 │                                                          │
                 │  ┌──────────────┐  ┌──────────────────────────────┐    │
                 │  │ Prometheus    │  │ Grafana                      │    │
                 │  │ :9090         │  │ :3001                        │    │
                 │  │ scrapes :8101 │  │ TokenRanger dashboard        │    │
                 │  │ /metrics      │  │ (tokens, latency, savings)   │    │
                 │  └──────────────┘  └──────────────────────────────┘    │
                 │                                                          │
                 └──────────────────────────────────────────────────────────┘
```

---

## Data Model

### Compression Event (per-invocation, received from both nodes)

```sql
CREATE TABLE compression_events (
    id          INTEGER PRIMARY KEY AUTOINCREMENT,
    timestamp   TEXT NOT NULL,           -- ISO 8601
    node        TEXT NOT NULL,           -- pvet630 | r430a
    session_id  TEXT,                    -- OpenClaw session key
    user_turn   INTEGER,                -- turn number in conversation

    -- Character metrics (from TokenRanger /compress response)
    original_chars      INTEGER NOT NULL,
    compressed_chars    INTEGER NOT NULL,
    char_reduction_pct  REAL NOT NULL,

    -- Token estimates (chars / 4 heuristic, or tiktoken if available)
    original_tokens_est     INTEGER NOT NULL,
    compressed_tokens_est   INTEGER NOT NULL,
    tokens_saved_est        INTEGER NOT NULL,

    -- Compression metadata
    compute_class   TEXT NOT NULL,       -- gpu_full, cpu_only, etc.
    model_used      TEXT NOT NULL,       -- mistral:7b-instruct, phi3.5
    strategy        TEXT NOT NULL,       -- full, light, passthrough
    latency_ms      REAL NOT NULL,
    code_blocks_stripped INTEGER DEFAULT 0,
    skipped         BOOLEAN DEFAULT 0,   -- true if turn 1 skip / trivial
    skip_reason     TEXT                 -- turn_1, trivial_input, disabled
);

CREATE INDEX idx_events_timestamp ON compression_events(timestamp);
CREATE INDEX idx_events_session ON compression_events(session_id);
CREATE INDEX idx_events_node ON compression_events(node);
CREATE INDEX idx_events_node_time ON compression_events(node, timestamp);
```

### API Token Usage (polled from both OpenClaw gateways)

```sql
CREATE TABLE api_usage_snapshots (
    id          INTEGER PRIMARY KEY AUTOINCREMENT,
    timestamp   TEXT NOT NULL,
    node        TEXT NOT NULL,           -- pvet630 | r430a
    session_id  TEXT,

    -- From OpenClaw usage normalization
    input_tokens    INTEGER,
    output_tokens   INTEGER,
    total_tokens    INTEGER,
    cache_read      INTEGER DEFAULT 0,
    cache_write     INTEGER DEFAULT 0,
    model           TEXT,
    provider        TEXT
);

CREATE INDEX idx_usage_timestamp ON api_usage_snapshots(timestamp);
CREATE INDEX idx_usage_node ON api_usage_snapshots(node);
CREATE INDEX idx_usage_node_time ON api_usage_snapshots(node, timestamp);
```

### Node Registry (tracked nodes + their gateway URLs)

```sql
CREATE TABLE nodes (
    node_id     TEXT PRIMARY KEY,        -- pvet630, r430a
    ip          TEXT NOT NULL,
    gateway_url TEXT NOT NULL,            -- http://192.168.1.242:3000
    tokenranger_url TEXT NOT NULL,        -- http://192.168.1.242:8100
    last_seen   TEXT,                     -- last /emit received
    status      TEXT DEFAULT 'unknown'    -- healthy, degraded, offline
);

-- Pre-seed
INSERT INTO nodes VALUES ('pvet630', '192.168.1.242',
    'http://192.168.1.242:3000', 'http://192.168.1.242:8100', NULL, 'unknown');
INSERT INTO nodes VALUES ('r430a', '192.168.1.240',
    'http://192.168.1.240:3000', 'http://192.168.1.240:8100', NULL, 'unknown');
```

---

## Implementation Phases

### Phase 1: Centralized Metrics Collector (CT 203)

**New project**: `tokenranger-metrics/` deployed on CT 203

Standalone FastAPI service — separate from the per-node TokenRanger compression
services. Runs on `:8101` to avoid collision if TokenRanger compression is ever
installed locally on CT 203.

**Files**:
```
tokenranger-metrics/
├── main.py              # FastAPI app (:8101)
├── metrics_store.py     # SQLite store + query engine
├── usage_poller.py      # Background task: polls both gateways
├── config.py            # Pydantic Settings (env vars)
├── requirements.txt     # fastapi, uvicorn, httpx
└── deploy/
    └── systemd/
        └── tokenranger-metrics.service
```

**`main.py` endpoints**:

```python
from fastapi import FastAPI
from metrics_store import MetricsStore
from usage_poller import UsagePoller

app = FastAPI(title="TokenRanger Metrics Collector")
store = MetricsStore()

@app.post("/emit")
async def emit_event(event: CompressionEventIn):
    """Receives fire-and-forget compression events from both nodes."""
    store.record_compression(event)
    return {"ok": True}

@app.get("/summary")
async def get_summary(hours: int = 24, node: str | None = None):
    """Aggregated metrics for the given time window."""
    return store.summary(hours=hours, node=node)

@app.get("/events")
async def get_events(since: str | None = None, until: str | None = None,
                     node: str | None = None, limit: int = 100):
    """Raw compression events, newest first."""
    return store.query(since=since, until=until, node=node, limit=limit)

@app.get("/compare")
async def compare_nodes(hours: int = 24):
    """Side-by-side comparison of all tracked nodes."""
    return store.compare_nodes(hours=hours)

@app.get("/export")
async def export_csv(hours: int = 24, node: str | None = None):
    """CSV download of compression events."""
    return store.export_csv(hours=hours, node=node)

@app.get("/health")
async def health():
    """Collector health + node connectivity status."""
    return {
        "status": "ok",
        "db_size_bytes": store.db_size(),
        "nodes": store.node_statuses(),
        "event_count_24h": store.event_count(hours=24),
    }
```

**`metrics_store.py` — summary query**:

```python
def summary(self, hours: int = 24, node: str | None = None) -> dict:
    since = datetime.utcnow() - timedelta(hours=hours)
    rows = self._query_events(since=since, node=node)

    total_original_chars = sum(r.original_chars for r in rows)
    total_compressed_chars = sum(r.compressed_chars for r in rows)
    total_original_tokens = sum(r.original_tokens_est for r in rows)
    total_compressed_tokens = sum(r.compressed_tokens_est for r in rows)
    latencies = sorted(r.latency_ms for r in rows if not r.skipped)

    skipped = [r for r in rows if r.skipped]
    compressed = [r for r in rows if not r.skipped]

    return {
        "window_hours": hours,
        "node_filter": node,
        "total_events": len(rows),
        "compressions": len(compressed),
        "skipped": len(skipped),
        "characters": {
            "input": total_original_chars,
            "output": total_compressed_chars,
            "saved": total_original_chars - total_compressed_chars,
            "reduction_pct": round(
                (1 - total_compressed_chars / max(total_original_chars, 1)) * 100, 1
            ),
        },
        "tokens_estimated": {
            "input": total_original_tokens,
            "compressed": total_compressed_tokens,
            "saved": total_original_tokens - total_compressed_tokens,
        },
        "latency_ms": {
            "mean": round(sum(latencies) / max(len(latencies), 1), 0),
            "p50": latencies[len(latencies) // 2] if latencies else 0,
            "p95": latencies[int(len(latencies) * 0.95)] if latencies else 0,
            "p99": latencies[int(len(latencies) * 0.99)] if latencies else 0,
        },
        "by_strategy": self._group_by(compressed, "strategy"),
        "by_node": self._group_by(compressed, "node"),
        "by_compute_class": self._group_by(compressed, "compute_class"),
    }
```

**`usage_poller.py` — background task**:

```python
class UsagePoller:
    """Polls OpenClaw gateway usage endpoints on both nodes every 5 minutes."""

    def __init__(self, store: MetricsStore, nodes: list[NodeConfig]):
        self.store = store
        self.nodes = nodes
        self.interval = 300  # 5 minutes

    async def poll_loop(self):
        while True:
            for node in self.nodes:
                try:
                    async with httpx.AsyncClient(timeout=10) as client:
                        # Poll gateway usage endpoint
                        resp = await client.get(
                            f"{node.gateway_url}/api/usage/status"
                        )
                        usage = resp.json()
                        self.store.record_api_usage(
                            node=node.node_id,
                            usage=usage,
                        )

                        # Also check TokenRanger health
                        health = await client.get(
                            f"{node.tokenranger_url}/health"
                        )
                        self.store.update_node_status(
                            node.node_id,
                            status="healthy" if health.status_code == 200 else "degraded",
                        )
                except Exception:
                    self.store.update_node_status(node.node_id, status="offline")

            await asyncio.sleep(self.interval)
```

**`config.py`**:

```python
from pydantic_settings import BaseSettings

class MetricsConfig(BaseSettings):
    db_path: str = "/opt/tokenranger-metrics/metrics.db"
    retention_days: int = 30
    token_estimate_ratio: int = 4        # chars per token
    cost_per_1m_input_tokens: float = 2.50
    poll_interval_seconds: int = 300
    prune_interval_seconds: int = 86400  # daily

    # Tracked nodes
    nodes: str = "pvet630:192.168.1.242,r430a:192.168.1.240"

    class Config:
        env_prefix = "TRMX_"
```

**Systemd unit** (`tokenranger-metrics.service`):

```ini
[Unit]
Description=TokenRanger Metrics Collector
After=network.target

[Service]
Type=simple
User=root
WorkingDirectory=/opt/tokenranger-metrics
ExecStart=/opt/tokenranger-metrics/venv/bin/uvicorn main:app --host 0.0.0.0 --port 8101
Restart=always
RestartSec=5

Environment="TRMX_DB_PATH=/opt/tokenranger-metrics/metrics.db"
Environment="TRMX_NODES=pvet630:192.168.1.242,r430a:192.168.1.240"

[Install]
WantedBy=multi-user.target
```

### Phase 2: Plugin Telemetry Emit (TypeScript — both nodes)

**File**: `index.ts` on pvet630 + r430a

After each compression (or skip), fire-and-forget POST to CT 203:

```typescript
const METRICS_URL = cfg.metricsUrl || "http://192.168.1.203:8101";

// After successful compression
if (result && cfg.metricsEnabled !== false) {
    fetch(`${METRICS_URL}/emit`, {
        method: "POST",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify({
            node: os.hostname(),
            session_id: sessionKey,
            user_turn: userTurnCount,
            original_chars: result.originalChars,
            compressed_chars: result.compressedChars,
            char_reduction_pct: result.reductionPct,
            compute_class: result.computeClass,
            model_used: result.modelUsed,
            strategy: result.strategy,
            latency_ms: result.latencyMs,
            code_blocks_stripped: codeBlockCount,
            skipped: false,
        }),
    }).catch(() => {}); // fire-and-forget, never block gateway
}

// On skip (turn 1, trivial, disabled)
if (cfg.metricsEnabled !== false) {
    fetch(`${METRICS_URL}/emit`, {
        method: "POST",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify({
            node: os.hostname(),
            session_id: sessionKey,
            user_turn: userTurnCount,
            original_chars: 0,
            compressed_chars: 0,
            char_reduction_pct: 0,
            compute_class: "n/a",
            model_used: "n/a",
            strategy: "skip",
            latency_ms: 0,
            skipped: true,
            skip_reason: "turn_1",
        }),
    }).catch(() => {});
}
```

**New config fields** (in `openclaw.json` per node):

```json
{
  "plugins": {
    "entries": {
      "tokenranger": {
        "metricsEnabled": true,
        "metricsUrl": "http://192.168.1.203:8101"
      }
    }
  }
}
```

### Phase 3: Reporting CLI

**File**: `src/setup.ts` (extend existing CLI on each node)

The CLI queries CT 203's HTTP API and formats output locally.

```bash
# Summary for last 24 hours (default) — both nodes
openclaw tokenranger metrics

# Custom time window
openclaw tokenranger metrics --hours 72
openclaw tokenranger metrics --since 2026-02-27 --until 2026-02-28

# Per-node only
openclaw tokenranger metrics --node pvet630
openclaw tokenranger metrics --node r430a

# Node comparison
openclaw tokenranger metrics --compare

# Export as CSV
openclaw tokenranger metrics --export csv > report.csv

# JSON for scripting / piping
openclaw tokenranger metrics --json

# Quick status check
openclaw tokenranger metrics --status
```

**Example output — default (both nodes)**:

```
TokenRanger Metrics — Last 24 hours (all nodes)

  Compressions:     142
  Skipped (turn 1):  38
  Total turns:      180

  Characters:
    Input:      1,245,680
    Output:       287,340
    Saved:        958,340 (76.9%)

  Estimated Tokens:
    Input:        311,420
    Compressed:    71,835
    Saved:        239,585 (~$0.60 at $2.50/1M tokens)

  Latency:
    Mean:         1,842ms
    P50:          1,620ms
    P95:          3,140ms
    P99:          4,210ms

  By Strategy:
    full:         128 (90.1%) | avg 82.3% reduction
    light:          9 (6.3%)  | avg 34.1% reduction
    passthrough:    5 (3.5%)  | avg 0% reduction

  By Node:
    pvet630:      98  | 1.6s avg | 84.2% reduction | gpu_full
    r430a:        44  | 1.8s avg | 79.1% reduction | gpu_full (remote)
```

**Example output — `--compare`**:

```
TokenRanger Node Comparison — Last 24 hours

                        pvet630              r430a
                        ───────              ─────
  Compressions:           98                   44
  Skipped:                22                   16
  Chars saved:       724,120              234,220
  Tokens saved (est):181,030               58,555
  Avg reduction:       84.2%               79.1%
  Avg latency:        1,620ms             1,840ms
  P95 latency:        2,840ms             3,420ms
  Compute:            gpu_full        gpu_full (remote)
  Strategy:             full                 full
  Last seen:        2 min ago           5 min ago
  Status:             healthy              healthy
```

**Example output — `--status`**:

```
TokenRanger Metrics Collector — http://192.168.1.203:8101

  Collector:    healthy
  DB size:      2.3 MB (30-day retention)
  Events (24h): 180

  Nodes:
    pvet630  192.168.1.242  healthy   last seen 2m ago
    r430a    192.168.1.240  healthy   last seen 5m ago
```

### Phase 4: Prometheus Metrics Endpoint

**File**: `main.py` on CT 203

Expose `GET /metrics` at `:8101/metrics` in Prometheus text exposition format.
Prometheus on the same host scrapes `localhost:8101/metrics`.

```
# HELP tokenranger_compressions_total Total compression events
# TYPE tokenranger_compressions_total counter
tokenranger_compressions_total{node="pvet630",strategy="full"} 128
tokenranger_compressions_total{node="pvet630",strategy="light"} 3
tokenranger_compressions_total{node="r430a",strategy="full"} 44
tokenranger_compressions_total{node="r430a",strategy="light"} 6

# HELP tokenranger_tokens_saved_total Estimated tokens saved by compression
# TYPE tokenranger_tokens_saved_total counter
tokenranger_tokens_saved_total{node="pvet630"} 181030
tokenranger_tokens_saved_total{node="r430a"} 58555

# HELP tokenranger_chars_input_total Total input characters processed
# TYPE tokenranger_chars_input_total counter
tokenranger_chars_input_total{node="pvet630"} 860640
tokenranger_chars_input_total{node="r430a"} 385040

# HELP tokenranger_compression_ratio Current compression ratio (0-1)
# TYPE tokenranger_compression_ratio gauge
tokenranger_compression_ratio{node="pvet630"} 0.842
tokenranger_compression_ratio{node="r430a"} 0.791

# HELP tokenranger_compression_latency_seconds Compression latency histogram
# TYPE tokenranger_compression_latency_seconds histogram
tokenranger_compression_latency_seconds_bucket{node="pvet630",le="0.5"} 8
tokenranger_compression_latency_seconds_bucket{node="pvet630",le="1.0"} 32
tokenranger_compression_latency_seconds_bucket{node="pvet630",le="2.0"} 85
tokenranger_compression_latency_seconds_bucket{node="pvet630",le="5.0"} 97
tokenranger_compression_latency_seconds_bucket{node="pvet630",le="+Inf"} 98
tokenranger_compression_latency_seconds_bucket{node="r430a",le="0.5"} 2
tokenranger_compression_latency_seconds_bucket{node="r430a",le="1.0"} 10
tokenranger_compression_latency_seconds_bucket{node="r430a",le="2.0"} 35
tokenranger_compression_latency_seconds_bucket{node="r430a",le="5.0"} 43
tokenranger_compression_latency_seconds_bucket{node="r430a",le="+Inf"} 44

# HELP tokenranger_skipped_total Turns skipped by reason
# TYPE tokenranger_skipped_total counter
tokenranger_skipped_total{node="pvet630",reason="turn_1"} 22
tokenranger_skipped_total{node="r430a",reason="turn_1"} 16

# HELP tokenranger_node_up Node reachability (1=healthy, 0=offline)
# TYPE tokenranger_node_up gauge
tokenranger_node_up{node="pvet630"} 1
tokenranger_node_up{node="r430a"} 1

# HELP tokenranger_api_input_tokens_total Actual API input tokens (from gateway)
# TYPE tokenranger_api_input_tokens_total counter
tokenranger_api_input_tokens_total{node="pvet630"} 412890
tokenranger_api_input_tokens_total{node="r430a"} 198340
```

**Prometheus scrape config** (on CT 203, `localhost` — no network hop):

```yaml
scrape_configs:
  - job_name: "tokenranger-metrics"
    scrape_interval: 30s
    static_configs:
      - targets: ["localhost:8101"]
```

### Phase 5: OpenClaw API Token Correlation

**File**: `usage_poller.py` on CT 203

Background task that polls both OpenClaw gateways every 5 minutes for actual
API token usage. Stored in `api_usage_snapshots` and correlated with
compression events by session_id + time window.

**Polling targets**:
```
http://192.168.1.242:3000/api/usage/status   (pvet630)
http://192.168.1.240:3000/api/usage/status   (r430a)
```

This enables the cost impact report:

```
TokenRanger Impact Report — Last 24 hours

  pvet630:
    Without compression (estimated):   311,420 input tokens
    With compression (actual API):     181,030 input tokens
    Net savings:                       130,390 tokens
    Cost savings at $2.50/1M tokens:     $0.33

  r430a:
    Without compression (estimated):   142,680 input tokens
    With compression (actual API):      58,555 input tokens
    Net savings:                        84,125 tokens
    Cost savings at $2.50/1M tokens:     $0.21

  Combined:
    Total tokens saved:                214,515
    Total cost savings:                  $0.54/day (~$16.20/month)
```

### Phase 6: Grafana Dashboard

Pre-built dashboard JSON for import at `192.168.1.203:3001`.
Prometheus data source queries `localhost:9090`.

**Panels**:

| # | Panel | Type | Query |
|---|-------|------|-------|
| 1 | Compressions over time | Time series (stacked by node) | `rate(tokenranger_compressions_total[1h])` |
| 2 | Token savings (24h) | Stat | `sum(tokenranger_tokens_saved_total)` |
| 3 | Compression ratio | Gauge (per node) | `tokenranger_compression_ratio` |
| 4 | Latency P50/P95/P99 | Time series | `histogram_quantile(0.95, ...)` |
| 5 | Strategy distribution | Pie chart | `tokenranger_compressions_total` by strategy |
| 6 | Node comparison | Table | Both nodes side-by-side |
| 7 | Node health | Status map | `tokenranger_node_up` |
| 8 | Estimated cost savings | Stat | `sum(tokenranger_tokens_saved_total) * $cost` |
| 9 | API tokens (actual) | Time series | `tokenranger_api_input_tokens_total` |
| 10 | Skip rate | Gauge | `tokenranger_skipped_total / total` |

**Dashboard variables**:
- `$node`: dropdown (All, pvet630, r430a)
- `$window`: time range override (1h, 6h, 24h, 7d, 30d)
- `$cost`: input token cost per 1M (default 2.50)

---

## Configuration

### CT 203 — Collector env vars

```bash
TRMX_DB_PATH=/opt/tokenranger-metrics/metrics.db
TRMX_RETENTION_DAYS=30
TRMX_TOKEN_ESTIMATE_RATIO=4
TRMX_COST_PER_1M_INPUT_TOKENS=2.50
TRMX_POLL_INTERVAL_SECONDS=300
TRMX_NODES=pvet630:192.168.1.242,r430a:192.168.1.240
```

### pvet630 + r430a — Plugin config (`openclaw.json`)

```json
{
  "plugins": {
    "entries": {
      "tokenranger": {
        "metricsEnabled": true,
        "metricsUrl": "http://192.168.1.203:8101"
      }
    }
  }
}
```

### Adding a new node

1. Add to `TRMX_NODES` env var: `pvet630:192.168.1.242,r430a:192.168.1.240,newnode:192.168.1.XXX`
2. Restart collector: `systemctl restart tokenranger-metrics`
3. On the new node, set `metricsUrl` in `openclaw.json` to `http://192.168.1.203:8101`
4. Node appears automatically in reports, Prometheus, and Grafana

---

## Deployment Steps

### CT 203 Setup

```bash
# SSH to CT 203
ssh ct203

# Create project directory
mkdir -p /opt/tokenranger-metrics
cd /opt/tokenranger-metrics

# Create venv and install deps
python3 -m venv venv
source venv/bin/activate
pip install fastapi uvicorn httpx pydantic-settings

# Copy source files
# (scp from dev machine or git clone)

# Install systemd unit
cp deploy/systemd/tokenranger-metrics.service /etc/systemd/system/
systemctl daemon-reload
systemctl enable --now tokenranger-metrics

# Verify
curl http://localhost:8101/health

# Update Prometheus config
cat >> /etc/prometheus/prometheus.yml << 'EOF'

  - job_name: "tokenranger-metrics"
    scrape_interval: 30s
    static_configs:
      - targets: ["localhost:8101"]
EOF
systemctl reload prometheus

# Import Grafana dashboard
# (via Grafana UI: Dashboards → Import → paste JSON)
```

### pvet630 + r430a Config Update

```bash
# On each node, update openclaw.json:
openclaw config set plugins.entries.tokenranger.metricsEnabled true
openclaw config set plugins.entries.tokenranger.metricsUrl "http://192.168.1.203:8101"
openclaw gateway restart
```

---

## Storage Estimate

At ~200 compression events/day + usage polling every 5 min:
- Compression events: ~500 bytes/row × 200/day → ~100KB/day
- Usage snapshots: ~200 bytes/row × 576/day (2 nodes × 288 polls) → ~115KB/day
- **Total**: ~215KB/day → ~6.5MB/month
- 30-day retention with auto-prune: **DB stays under 10MB**
- SQLite WAL mode handles concurrent reads (Prometheus scrape + CLI query)

---

## Dependencies

**CT 203 collector** (pip):
- `fastapi` + `uvicorn` — HTTP server
- `httpx` — async HTTP client for polling gateways
- `pydantic-settings` — config from env vars
- `sqlite3` — stdlib, no install needed
- Optional: `prometheus_client` — cleaner Prometheus text format generation

**pvet630 + r430a** (no new dependencies):
- Uses existing `fetch()` in the TypeScript plugin — fire-and-forget POST

---

## File Summary

| File | Location | Phase | Description |
|------|----------|-------|-------------|
| `main.py` | CT 203 | 1 | FastAPI collector app (:8101) |
| `metrics_store.py` | CT 203 | 1 | SQLite store + query engine |
| `usage_poller.py` | CT 203 | 1,5 | Background poller for both gateways |
| `config.py` | CT 203 | 1 | Pydantic Settings (TRMX_ prefix) |
| `requirements.txt` | CT 203 | 1 | Python dependencies |
| `deploy/systemd/tokenranger-metrics.service` | CT 203 | 1 | Systemd unit |
| `index.ts` | Both nodes | 2 | Add fire-and-forget POST to /emit |
| `src/config.ts` | Both nodes | 2 | Add metricsEnabled, metricsUrl fields |
| `src/setup.ts` | Both nodes | 3 | Add `openclaw tokenranger metrics` CLI |
| `deploy/grafana/dashboard.json` | CT 203 | 6 | Grafana dashboard import |

---

## Implementation Priority

| Phase | Effort | Value | Priority |
|-------|--------|-------|----------|
| 1. CT 203 collector + SQLite | Medium | High | **Must have** |
| 2. Plugin emit (both nodes) | Low | High | **Must have** |
| 3. Reporting CLI | Low | High | **Must have** |
| 4. Prometheus /metrics | Low | Medium | **Should have** (Prometheus already there) |
| 5. Gateway usage correlation | Medium | Medium | Nice to have |
| 6. Grafana dashboard | Low | Medium | Nice to have (Grafana already there) |

Phases 1-3 deliver the core "measure and report" capability.
Phase 4 is low-effort since Prometheus is already on CT 203.
Phases 5-6 provide the full observability picture.
