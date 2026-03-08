"""Configuration for the TokenRanger Metrics Collector."""

from __future__ import annotations

from dataclasses import dataclass, field
from pydantic_settings import BaseSettings, SettingsConfigDict


class MetricsConfig(BaseSettings):
    """Settings loaded from environment variables with TRMX_ prefix."""

    db_path: str = "/opt/tokenranger-metrics/metrics.db"
    retention_days: int = 30
    token_estimate_ratio: int = 4  # chars per token (GPT-family average)
    cost_per_1m_input_tokens: float = 2.50
    poll_interval_seconds: int = 300  # 5 minutes
    prune_interval_seconds: int = 86400  # daily
    host: str = "0.0.0.0"
    port: int = 8101

    # Tracked nodes: "name:ip,name:ip"
    nodes: str = "pvet630:192.168.1.242,r430a:192.168.1.240"

    model_config = SettingsConfigDict(env_prefix="TRMX_")

    def parse_nodes(self) -> list[NodeConfig]:
        result = []
        for entry in self.nodes.split(","):
            entry = entry.strip()
            if ":" not in entry:
                continue
            name, ip = entry.split(":", 1)
            result.append(
                NodeConfig(
                    node_id=name.strip(),
                    ip=ip.strip(),
                    gateway_url=f"http://{ip.strip()}:3000",
                    tokenranger_url=f"http://{ip.strip()}:8100",
                )
            )
        return result


@dataclass
class NodeConfig:
    node_id: str
    ip: str
    gateway_url: str
    tokenranger_url: str
