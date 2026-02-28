"""SQLite-backed metrics store for TokenRanger compression events."""

from __future__ import annotations

import os
import sqlite3
import statistics
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any


SCHEMA = """
CREATE TABLE IF NOT EXISTS compression_events (
    id                  INTEGER PRIMARY KEY AUTOINCREMENT,
    timestamp           TEXT NOT NULL,
    node                TEXT NOT NULL,
    session_id          TEXT,
    user_turn           INTEGER,
    original_chars      INTEGER NOT NULL DEFAULT 0,
    compressed_chars    INTEGER NOT NULL DEFAULT 0,
    char_reduction_pct  REAL NOT NULL DEFAULT 0,
    original_tokens_est     INTEGER NOT NULL DEFAULT 0,
    compressed_tokens_est   INTEGER NOT NULL DEFAULT 0,
    tokens_saved_est        INTEGER NOT NULL DEFAULT 0,
    compute_class       TEXT NOT NULL DEFAULT 'unknown',
    model_used          TEXT NOT NULL DEFAULT 'unknown',
    strategy            TEXT NOT NULL DEFAULT 'unknown',
    latency_ms          REAL NOT NULL DEFAULT 0,
    code_blocks_stripped INTEGER DEFAULT 0,
    skipped             INTEGER DEFAULT 0,
    skip_reason         TEXT
);

CREATE INDEX IF NOT EXISTS idx_events_timestamp ON compression_events(timestamp);
CREATE INDEX IF NOT EXISTS idx_events_session ON compression_events(session_id);
CREATE INDEX IF NOT EXISTS idx_events_node ON compression_events(node);
CREATE INDEX IF NOT EXISTS idx_events_node_time ON compression_events(node, timestamp);

CREATE TABLE IF NOT EXISTS api_usage_snapshots (
    id              INTEGER PRIMARY KEY AUTOINCREMENT,
    timestamp       TEXT NOT NULL,
    node            TEXT NOT NULL,
    session_id      TEXT,
    input_tokens    INTEGER,
    output_tokens   INTEGER,
    total_tokens    INTEGER,
    cache_read      INTEGER DEFAULT 0,
    cache_write     INTEGER DEFAULT 0,
    model           TEXT,
    provider        TEXT
);

CREATE INDEX IF NOT EXISTS idx_usage_timestamp ON api_usage_snapshots(timestamp);
CREATE INDEX IF NOT EXISTS idx_usage_node ON api_usage_snapshots(node);
CREATE INDEX IF NOT EXISTS idx_usage_node_time ON api_usage_snapshots(node, timestamp);

CREATE TABLE IF NOT EXISTS nodes (
    node_id         TEXT PRIMARY KEY,
    ip              TEXT NOT NULL,
    gateway_url     TEXT NOT NULL,
    tokenranger_url TEXT NOT NULL,
    last_seen       TEXT,
    status          TEXT DEFAULT 'unknown'
);
"""


class MetricsStore:
    def __init__(self, db_path: str, token_ratio: int = 4):
        self.db_path = db_path
        self.token_ratio = token_ratio

        # Ensure parent directory exists
        Path(db_path).parent.mkdir(parents=True, exist_ok=True)

        self.conn = sqlite3.connect(db_path, check_same_thread=False)
        self.conn.execute("PRAGMA journal_mode=WAL")
        self.conn.execute("PRAGMA busy_timeout=5000")
        self.conn.executescript(SCHEMA)
        self.conn.commit()

    # ------------------------------------------------------------------
    # Write
    # ------------------------------------------------------------------

    def record_compression(self, event: dict[str, Any]) -> int:
        now = event.get("timestamp") or datetime.now(timezone.utc).isoformat()
        node = event.get("node", "unknown")
        original_chars = int(event.get("original_chars", 0))
        compressed_chars = int(event.get("compressed_chars", 0))
        char_reduction = float(event.get("char_reduction_pct", 0))
        original_tokens = original_chars // self.token_ratio
        compressed_tokens = compressed_chars // self.token_ratio
        tokens_saved = original_tokens - compressed_tokens
        skipped = 1 if event.get("skipped") else 0

        cur = self.conn.execute(
            """INSERT INTO compression_events
               (timestamp, node, session_id, user_turn,
                original_chars, compressed_chars, char_reduction_pct,
                original_tokens_est, compressed_tokens_est, tokens_saved_est,
                compute_class, model_used, strategy, latency_ms,
                code_blocks_stripped, skipped, skip_reason)
               VALUES (?,?,?,?, ?,?,?, ?,?,?, ?,?,?,?, ?,?,?)""",
            (
                now,
                node,
                event.get("session_id"),
                event.get("user_turn"),
                original_chars,
                compressed_chars,
                char_reduction,
                original_tokens,
                compressed_tokens,
                tokens_saved,
                event.get("compute_class", "unknown"),
                event.get("model_used", "unknown"),
                event.get("strategy", "unknown"),
                float(event.get("latency_ms", 0)),
                int(event.get("code_blocks_stripped", 0)),
                skipped,
                event.get("skip_reason"),
            ),
        )
        self.conn.commit()

        # Update node last_seen
        self.conn.execute(
            """INSERT INTO nodes (node_id, ip, gateway_url, tokenranger_url, last_seen, status)
               VALUES (?, '', '', '', ?, 'healthy')
               ON CONFLICT(node_id) DO UPDATE SET last_seen = ?, status = 'healthy'""",
            (node, now, now),
        )
        self.conn.commit()
        return cur.lastrowid  # type: ignore[return-value]

    def record_api_usage(self, node: str, usage: dict[str, Any]) -> None:
        now = datetime.now(timezone.utc).isoformat()
        self.conn.execute(
            """INSERT INTO api_usage_snapshots
               (timestamp, node, session_id, input_tokens, output_tokens,
                total_tokens, cache_read, cache_write, model, provider)
               VALUES (?,?,?,?,?,?,?,?,?,?)""",
            (
                now,
                node,
                usage.get("session_id"),
                usage.get("input_tokens", 0),
                usage.get("output_tokens", 0),
                usage.get("total_tokens", 0),
                usage.get("cache_read", 0),
                usage.get("cache_write", 0),
                usage.get("model"),
                usage.get("provider"),
            ),
        )
        self.conn.commit()

    def update_node_status(self, node_id: str, status: str) -> None:
        now = datetime.now(timezone.utc).isoformat()
        self.conn.execute(
            """INSERT INTO nodes (node_id, ip, gateway_url, tokenranger_url, last_seen, status)
               VALUES (?, '', '', '', ?, ?)
               ON CONFLICT(node_id) DO UPDATE SET status = ?, last_seen = ?""",
            (node_id, now, status, status, now),
        )
        self.conn.commit()

    def register_node(self, node_id: str, ip: str, gateway_url: str, tokenranger_url: str) -> None:
        self.conn.execute(
            """INSERT INTO nodes (node_id, ip, gateway_url, tokenranger_url, status)
               VALUES (?,?,?,?, 'unknown')
               ON CONFLICT(node_id) DO UPDATE SET
                 ip = ?, gateway_url = ?, tokenranger_url = ?""",
            (node_id, ip, gateway_url, tokenranger_url, ip, gateway_url, tokenranger_url),
        )
        self.conn.commit()

    # ------------------------------------------------------------------
    # Read
    # ------------------------------------------------------------------

    def query_events(
        self,
        since: str | None = None,
        until: str | None = None,
        node: str | None = None,
        limit: int = 100,
    ) -> list[dict[str, Any]]:
        conditions = []
        params: list[Any] = []
        if since:
            conditions.append("timestamp >= ?")
            params.append(since)
        if until:
            conditions.append("timestamp <= ?")
            params.append(until)
        if node:
            conditions.append("node = ?")
            params.append(node)

        where = " AND ".join(conditions) if conditions else "1=1"
        rows = self.conn.execute(
            f"SELECT * FROM compression_events WHERE {where} ORDER BY timestamp DESC LIMIT ?",
            [*params, limit],
        ).fetchall()
        cols = [d[0] for d in self.conn.execute("SELECT * FROM compression_events LIMIT 0").description]
        return [dict(zip(cols, row)) for row in rows]

    def summary(self, hours: int = 24, node: str | None = None) -> dict[str, Any]:
        since = (datetime.now(timezone.utc) - timedelta(hours=hours)).isoformat()
        conditions = ["timestamp >= ?"]
        params: list[Any] = [since]
        if node:
            conditions.append("node = ?")
            params.append(node)
        where = " AND ".join(conditions)

        rows = self.conn.execute(
            f"SELECT * FROM compression_events WHERE {where} ORDER BY timestamp",
            params,
        ).fetchall()
        cols = [d[0] for d in self.conn.execute("SELECT * FROM compression_events LIMIT 0").description]
        events = [dict(zip(cols, row)) for row in rows]

        compressed = [e for e in events if not e["skipped"]]
        skipped = [e for e in events if e["skipped"]]

        total_orig_chars = sum(e["original_chars"] for e in compressed)
        total_comp_chars = sum(e["compressed_chars"] for e in compressed)
        total_orig_tokens = sum(e["original_tokens_est"] for e in compressed)
        total_comp_tokens = sum(e["compressed_tokens_est"] for e in compressed)
        latencies = sorted(e["latency_ms"] for e in compressed if e["latency_ms"] > 0)

        return {
            "window_hours": hours,
            "node_filter": node,
            "total_events": len(events),
            "compressions": len(compressed),
            "skipped": len(skipped),
            "characters": {
                "input": total_orig_chars,
                "output": total_comp_chars,
                "saved": total_orig_chars - total_comp_chars,
                "reduction_pct": round(
                    (1 - total_comp_chars / max(total_orig_chars, 1)) * 100, 1
                ),
            },
            "tokens_estimated": {
                "input": total_orig_tokens,
                "compressed": total_comp_tokens,
                "saved": total_orig_tokens - total_comp_tokens,
            },
            "latency_ms": _latency_stats(latencies),
            "by_strategy": _group_by_field(compressed, "strategy"),
            "by_node": _group_by_field(compressed, "node"),
            "by_compute_class": _group_by_field(compressed, "compute_class"),
        }

    def compare_nodes(self, hours: int = 24) -> dict[str, Any]:
        since = (datetime.now(timezone.utc) - timedelta(hours=hours)).isoformat()
        rows = self.conn.execute(
            "SELECT * FROM compression_events WHERE timestamp >= ? ORDER BY timestamp",
            (since,),
        ).fetchall()
        cols = [d[0] for d in self.conn.execute("SELECT * FROM compression_events LIMIT 0").description]
        events = [dict(zip(cols, row)) for row in rows]

        nodes_data: dict[str, list[dict]] = {}
        for e in events:
            nodes_data.setdefault(e["node"], []).append(e)

        result = {}
        for nid, node_events in nodes_data.items():
            compressed = [e for e in node_events if not e["skipped"]]
            skipped_list = [e for e in node_events if e["skipped"]]
            latencies = sorted(e["latency_ms"] for e in compressed if e["latency_ms"] > 0)
            total_orig = sum(e["original_chars"] for e in compressed)
            total_comp = sum(e["compressed_chars"] for e in compressed)
            result[nid] = {
                "compressions": len(compressed),
                "skipped": len(skipped_list),
                "chars_saved": total_orig - total_comp,
                "tokens_saved_est": sum(e["tokens_saved_est"] for e in compressed),
                "avg_reduction_pct": round(
                    statistics.mean(e["char_reduction_pct"] for e in compressed), 1
                )
                if compressed
                else 0,
                "latency_ms": _latency_stats(latencies),
                "primary_compute": _mode_value(compressed, "compute_class"),
                "primary_strategy": _mode_value(compressed, "strategy"),
            }

        # Add node status
        for nid in result:
            node_row = self.conn.execute(
                "SELECT last_seen, status FROM nodes WHERE node_id = ?", (nid,)
            ).fetchone()
            if node_row:
                result[nid]["last_seen"] = node_row[0]
                result[nid]["status"] = node_row[1]

        return {"window_hours": hours, "nodes": result}

    def node_statuses(self) -> list[dict[str, Any]]:
        rows = self.conn.execute("SELECT * FROM nodes").fetchall()
        cols = [d[0] for d in self.conn.execute("SELECT * FROM nodes LIMIT 0").description]
        return [dict(zip(cols, row)) for row in rows]

    def event_count(self, hours: int = 24) -> int:
        since = (datetime.now(timezone.utc) - timedelta(hours=hours)).isoformat()
        row = self.conn.execute(
            "SELECT COUNT(*) FROM compression_events WHERE timestamp >= ?", (since,)
        ).fetchone()
        return row[0] if row else 0

    def db_size(self) -> int:
        try:
            return os.path.getsize(self.db_path)
        except OSError:
            return 0

    # ------------------------------------------------------------------
    # Prometheus
    # ------------------------------------------------------------------

    def prometheus_metrics(self) -> str:
        """Generate Prometheus text exposition format."""
        since_24h = (datetime.now(timezone.utc) - timedelta(hours=24)).isoformat()

        lines: list[str] = []

        # Compressions total by node+strategy
        rows = self.conn.execute(
            """SELECT node, strategy, COUNT(*) FROM compression_events
               WHERE skipped = 0 GROUP BY node, strategy"""
        ).fetchall()
        lines.append("# HELP tokenranger_compressions_total Total compression events")
        lines.append("# TYPE tokenranger_compressions_total counter")
        for node, strategy, count in rows:
            lines.append(f'tokenranger_compressions_total{{node="{node}",strategy="{strategy}"}} {count}')

        # Tokens saved total by node
        rows = self.conn.execute(
            """SELECT node, SUM(tokens_saved_est) FROM compression_events
               WHERE skipped = 0 GROUP BY node"""
        ).fetchall()
        lines.append("# HELP tokenranger_tokens_saved_total Estimated tokens saved")
        lines.append("# TYPE tokenranger_tokens_saved_total counter")
        for node, total in rows:
            lines.append(f'tokenranger_tokens_saved_total{{node="{node}"}} {total or 0}')

        # Chars input total by node
        rows = self.conn.execute(
            """SELECT node, SUM(original_chars) FROM compression_events
               WHERE skipped = 0 GROUP BY node"""
        ).fetchall()
        lines.append("# HELP tokenranger_chars_input_total Total input characters processed")
        lines.append("# TYPE tokenranger_chars_input_total counter")
        for node, total in rows:
            lines.append(f'tokenranger_chars_input_total{{node="{node}"}} {total or 0}')

        # Compression ratio gauge (24h window)
        rows = self.conn.execute(
            """SELECT node, SUM(original_chars), SUM(compressed_chars)
               FROM compression_events
               WHERE skipped = 0 AND timestamp >= ?
               GROUP BY node""",
            (since_24h,),
        ).fetchall()
        lines.append("# HELP tokenranger_compression_ratio Compression ratio (24h, 0-1)")
        lines.append("# TYPE tokenranger_compression_ratio gauge")
        for node, orig, comp in rows:
            ratio = round(1 - (comp or 0) / max(orig or 1, 1), 3)
            lines.append(f'tokenranger_compression_ratio{{node="{node}"}} {ratio}')

        # Latency histogram by node
        buckets = [0.5, 1.0, 2.0, 3.0, 5.0, 10.0]
        rows = self.conn.execute(
            """SELECT node, latency_ms FROM compression_events
               WHERE skipped = 0 AND timestamp >= ?""",
            (since_24h,),
        ).fetchall()
        node_latencies: dict[str, list[float]] = {}
        for node, lat in rows:
            node_latencies.setdefault(node, []).append(lat / 1000.0)

        lines.append("# HELP tokenranger_compression_latency_seconds Compression latency")
        lines.append("# TYPE tokenranger_compression_latency_seconds histogram")
        for node, lats in node_latencies.items():
            for b in buckets:
                count = sum(1 for l in lats if l <= b)
                lines.append(
                    f'tokenranger_compression_latency_seconds_bucket{{node="{node}",le="{b}"}} {count}'
                )
            lines.append(
                f'tokenranger_compression_latency_seconds_bucket{{node="{node}",le="+Inf"}} {len(lats)}'
            )
            lines.append(
                f'tokenranger_compression_latency_seconds_sum{{node="{node}"}} {sum(lats):.3f}'
            )
            lines.append(
                f'tokenranger_compression_latency_seconds_count{{node="{node}"}} {len(lats)}'
            )

        # Skipped total by node+reason
        rows = self.conn.execute(
            """SELECT node, skip_reason, COUNT(*) FROM compression_events
               WHERE skipped = 1 GROUP BY node, skip_reason"""
        ).fetchall()
        lines.append("# HELP tokenranger_skipped_total Turns skipped by reason")
        lines.append("# TYPE tokenranger_skipped_total counter")
        for node, reason, count in rows:
            lines.append(
                f'tokenranger_skipped_total{{node="{node}",reason="{reason or "unknown"}"}} {count}'
            )

        # Node health
        node_rows = self.conn.execute("SELECT node_id, status FROM nodes").fetchall()
        lines.append("# HELP tokenranger_node_up Node reachability (1=healthy, 0=offline)")
        lines.append("# TYPE tokenranger_node_up gauge")
        for nid, status in node_rows:
            val = 1 if status == "healthy" else 0
            lines.append(f'tokenranger_node_up{{node="{nid}"}} {val}')

        # API usage totals by node
        rows = self.conn.execute(
            """SELECT node, SUM(input_tokens), SUM(output_tokens)
               FROM api_usage_snapshots GROUP BY node"""
        ).fetchall()
        if rows:
            lines.append("# HELP tokenranger_api_input_tokens_total Actual API input tokens")
            lines.append("# TYPE tokenranger_api_input_tokens_total counter")
            for node, inp, _ in rows:
                lines.append(f'tokenranger_api_input_tokens_total{{node="{node}"}} {inp or 0}')
            lines.append("# HELP tokenranger_api_output_tokens_total Actual API output tokens")
            lines.append("# TYPE tokenranger_api_output_tokens_total counter")
            for node, _, out in rows:
                lines.append(f'tokenranger_api_output_tokens_total{{node="{node}"}} {out or 0}')

        return "\n".join(lines) + "\n"

    # ------------------------------------------------------------------
    # Export
    # ------------------------------------------------------------------

    def export_csv(self, hours: int = 24, node: str | None = None) -> str:
        since = (datetime.now(timezone.utc) - timedelta(hours=hours)).isoformat()
        conditions = ["timestamp >= ?"]
        params: list[Any] = [since]
        if node:
            conditions.append("node = ?")
            params.append(node)
        where = " AND ".join(conditions)

        rows = self.conn.execute(
            f"SELECT * FROM compression_events WHERE {where} ORDER BY timestamp",
            params,
        ).fetchall()
        cols = [d[0] for d in self.conn.execute("SELECT * FROM compression_events LIMIT 0").description]

        lines = [",".join(cols)]
        for row in rows:
            lines.append(",".join(str(v) if v is not None else "" for v in row))
        return "\n".join(lines) + "\n"

    # ------------------------------------------------------------------
    # Maintenance
    # ------------------------------------------------------------------

    def prune(self, retention_days: int = 30) -> int:
        cutoff = (datetime.now(timezone.utc) - timedelta(days=retention_days)).isoformat()
        cur = self.conn.execute(
            "DELETE FROM compression_events WHERE timestamp < ?", (cutoff,)
        )
        self.conn.execute(
            "DELETE FROM api_usage_snapshots WHERE timestamp < ?", (cutoff,)
        )
        self.conn.commit()
        return cur.rowcount


# ------------------------------------------------------------------
# Helpers
# ------------------------------------------------------------------


def _latency_stats(latencies: list[float]) -> dict[str, float]:
    if not latencies:
        return {"mean": 0, "p50": 0, "p95": 0, "p99": 0}
    n = len(latencies)
    return {
        "mean": round(statistics.mean(latencies), 0),
        "p50": latencies[n // 2],
        "p95": latencies[int(n * 0.95)] if n > 1 else latencies[-1],
        "p99": latencies[int(n * 0.99)] if n > 1 else latencies[-1],
    }


def _group_by_field(events: list[dict], field: str) -> dict[str, dict]:
    groups: dict[str, list[dict]] = {}
    for e in events:
        groups.setdefault(e[field], []).append(e)
    result = {}
    for key, group in groups.items():
        latencies = sorted(e["latency_ms"] for e in group if e["latency_ms"] > 0)
        result[key] = {
            "count": len(group),
            "pct": round(len(group) / max(len(events), 1) * 100, 1),
            "avg_reduction_pct": round(
                statistics.mean(e["char_reduction_pct"] for e in group), 1
            )
            if group
            else 0,
            "avg_latency_ms": round(statistics.mean(latencies), 0) if latencies else 0,
        }
    return result


def _mode_value(events: list[dict], field: str) -> str:
    if not events:
        return "unknown"
    values = [e[field] for e in events]
    return max(set(values), key=values.count)
