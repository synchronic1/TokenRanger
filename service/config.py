from pydantic_settings import BaseSettings, SettingsConfigDict
from typing import Optional


class Settings(BaseSettings):
    """Configuration for the TokenRanger compression service."""

    ollama_base_url: str = "http://localhost:11434"
    ollama_timeout: float = 3.0

    service_host: str = "127.0.0.1"
    service_port: int = 8100

    gpu_compression_model: str = "qwen3:8b"
    gpu_fast_model: str = "qwen3:4b"
    gpu_max_context: int = 8192

    cpu_compression_model: str = "qwen3:1.7b"
    cpu_fast_model: str = "qwen3:1.7b"
    cpu_max_context: int = 4096

    probe_cache_ttl: float = 300.0

    log_level: str = "INFO"
    metrics_file: Optional[str] = None

    model_config = SettingsConfigDict(env_prefix="TOKENRANGER_")
