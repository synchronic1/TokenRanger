"""Background task that polls OpenClaw gateway usage endpoints on both nodes."""

from __future__ import annotations

import asyncio
import logging

import httpx

from config import NodeConfig
from metrics_store import MetricsStore

logger = logging.getLogger("tokenranger-metrics.poller")


class UsagePoller:
    """Polls OpenClaw gateway usage + TokenRanger health on all tracked nodes."""

    def __init__(
        self,
        store: MetricsStore,
        nodes: list[NodeConfig],
        interval: int = 300,
    ):
        self.store = store
        self.nodes = nodes
        self.interval = interval

    async def poll_once(self) -> None:
        for node in self.nodes:
            await self._poll_node(node)

    async def poll_loop(self) -> None:
        logger.info(
            "Usage poller started: %d nodes, %ds interval",
            len(self.nodes),
            self.interval,
        )
        while True:
            await self.poll_once()
            await asyncio.sleep(self.interval)

    async def _poll_node(self, node: NodeConfig) -> None:
        try:
            async with httpx.AsyncClient(timeout=10) as client:
                # Poll TokenRanger health
                try:
                    health_resp = await client.get(f"{node.tokenranger_url}/health")
                    if health_resp.status_code == 200:
                        health = health_resp.json()
                        status = "healthy" if health.get("status") == "ok" else "degraded"
                    else:
                        status = "degraded"
                except Exception:
                    status = "offline"

                self.store.update_node_status(node.node_id, status)

                # Poll OpenClaw gateway usage
                try:
                    usage_resp = await client.get(f"{node.gateway_url}/api/usage/status")
                    if usage_resp.status_code == 200:
                        usage_data = usage_resp.json()
                        if isinstance(usage_data, dict):
                            self.store.record_api_usage(
                                node=node.node_id,
                                usage=usage_data,
                            )
                except Exception as e:
                    logger.debug("Failed to poll gateway usage for %s: %s", node.node_id, e)

        except Exception as e:
            logger.warning("Failed to poll node %s: %s", node.node_id, e)
            self.store.update_node_status(node.node_id, "offline")


class PruneScheduler:
    """Runs daily pruning of expired rows."""

    def __init__(self, store: MetricsStore, retention_days: int = 30, interval: int = 86400):
        self.store = store
        self.retention_days = retention_days
        self.interval = interval

    async def prune_loop(self) -> None:
        logger.info("Prune scheduler started: %d day retention", self.retention_days)
        while True:
            deleted = self.store.prune(self.retention_days)
            if deleted:
                logger.info("Pruned %d expired rows", deleted)
            await asyncio.sleep(self.interval)
