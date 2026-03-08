"""TokenRanger Metrics Collector — Centralized FastAPI service on CT 203.

Receives compression events from TokenRanger instances on pvet630 and r430a,
stores in SQLite, provides reporting endpoints and Prometheus metrics.
"""

from __future__ import annotations

import asyncio
import logging
from contextlib import asynccontextmanager

from fastapi import FastAPI, Query, Response
from pydantic import BaseModel

from config import MetricsConfig
from metrics_store import MetricsStore
from usage_poller import PruneScheduler, UsagePoller

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(name)s] %(levelname)s %(message)s",
)
logger = logging.getLogger("tokenranger-metrics")

cfg = MetricsConfig()
store = MetricsStore(db_path=cfg.db_path, token_ratio=cfg.token_estimate_ratio)

# Parse nodes once, reuse for registration and poller
_nodes = cfg.parse_nodes()
for node_cfg in _nodes:
    store.register_node(
        node_cfg.node_id,
        node_cfg.ip,
        node_cfg.gateway_url,
        node_cfg.tokenranger_url,
    )


# -----------------------------------------------------------------------
# Background tasks
# -----------------------------------------------------------------------

@asynccontextmanager
async def lifespan(app: FastAPI):
    poller = UsagePoller(
        store=store,
        nodes=_nodes,
        interval=cfg.poll_interval_seconds,
    )
    pruner = PruneScheduler(
        store=store,
        retention_days=cfg.retention_days,
        interval=cfg.prune_interval_seconds,
    )
    poll_task = asyncio.create_task(poller.poll_loop())
    prune_task = asyncio.create_task(pruner.prune_loop())
    logger.info(
        "Metrics collector started: db=%s, nodes=%s",
        cfg.db_path,
        cfg.nodes,
    )
    yield
    poll_task.cancel()
    prune_task.cancel()


app = FastAPI(
    title="TokenRanger Metrics Collector",
    description="Centralized token consumption measurement for OpenClaw instances",
    lifespan=lifespan,
)


# -----------------------------------------------------------------------
# Request models
# -----------------------------------------------------------------------

class CompressionEventIn(BaseModel):
    """Compression event emitted by TokenRanger plugin hooks."""

    node: str = "unknown"
    session_id: str = ""
    user_turn: int = 0
    original_chars: int = 0
    compressed_chars: int = 0
    char_reduction_pct: float = 0
    compute_class: str = "unknown"
    model_used: str = "unknown"
    strategy: str = "unknown"
    latency_ms: float = 0
    code_blocks_stripped: int = 0
    skipped: bool = False
    skip_reason: str | None = None
    timestamp: str | None = None


# -----------------------------------------------------------------------
# Endpoints
# -----------------------------------------------------------------------

@app.post("/emit")
async def emit_event(event: CompressionEventIn):
    """Receive a compression event from a TokenRanger node (fire-and-forget)."""
    row_id = store.record_compression(event.model_dump())
    return {"ok": True, "id": row_id}


@app.get("/summary")
async def get_summary(
    hours: int = Query(24, ge=1, le=8760),
    node: str | None = None,
):
    """Aggregated metrics for the given time window."""
    return store.summary(hours=hours, node=node)


@app.get("/events")
async def get_events(
    since: str | None = None,
    until: str | None = None,
    node: str | None = None,
    limit: int = Query(100, ge=1, le=10000),
):
    """Raw compression events, newest first."""
    return store.query_events(since=since, until=until, node=node, limit=limit)


@app.get("/compare")
async def compare_nodes(hours: int = Query(24, ge=1, le=8760)):
    """Side-by-side comparison of all tracked nodes."""
    return store.compare_nodes(hours=hours)


@app.get("/export")
async def export_csv(
    hours: int = Query(24, ge=1, le=8760),
    node: str | None = None,
):
    """CSV download of compression events."""
    csv_data = store.export_csv(hours=hours, node=node)
    return Response(
        content=csv_data,
        media_type="text/csv",
        headers={"Content-Disposition": "attachment; filename=tokenranger-metrics.csv"},
    )


@app.get("/metrics")
async def prometheus_metrics():
    """Prometheus text exposition format."""
    return Response(
        content=store.prometheus_metrics(),
        media_type="text/plain; version=0.0.4; charset=utf-8",
    )


@app.get("/health")
async def health():
    """Collector health and node connectivity status."""
    return {
        "status": "ok",
        "db_path": cfg.db_path,
        "db_size_bytes": store.db_size(),
        "nodes": store.node_statuses(),
        "event_count_24h": store.event_count(hours=24),
        "retention_days": cfg.retention_days,
    }


# -----------------------------------------------------------------------
# Entry point
# -----------------------------------------------------------------------

if __name__ == "__main__":
    import uvicorn

    uvicorn.run(app, host=cfg.host, port=cfg.port)
