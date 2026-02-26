import asyncio
import time
import logging
from enum import Enum
from dataclasses import dataclass
from typing import Optional

import httpx

from config import Settings

logger = logging.getLogger("tokenranger.router")


class ComputeClass(Enum):
    GPU_FULL = "gpu_full"
    GPU_PARTIAL = "gpu_partial"
    CPU_ONLY = "cpu_only"
    UNAVAILABLE = "unavailable"


@dataclass
class InferenceProfile:
    compute_class: ComputeClass
    endpoint_url: str
    model: str
    max_context: int
    compression_strategy: str  # "full" | "light" | "passthrough"


class InferenceRouter:
    """Probes local Ollama, detects compute class, selects model + strategy.
    Simplified for single-VM deployment: only probes localhost:11434.
    """

    def __init__(self, settings: Settings):
        self.settings = settings
        self._profile_cache: Optional[InferenceProfile] = None
        self._cache_ts: float = 0
        self._probe_lock: asyncio.Lock = asyncio.Lock()

    async def probe(self) -> InferenceProfile:
        now = time.time()
        if self._profile_cache and (now - self._cache_ts) < self.settings.probe_cache_ttl:
            return self._profile_cache

        async with self._probe_lock:
            # Re-check after acquiring lock (another coroutine may have refreshed)
            now = time.time()
            if self._profile_cache and (now - self._cache_ts) < self.settings.probe_cache_ttl:
                return self._profile_cache

            return await self._do_probe(now)

    async def _do_probe(self, now: float) -> InferenceProfile:
        url = self.settings.ollama_base_url
        timeout = self.settings.ollama_timeout

        try:
            async with httpx.AsyncClient(timeout=timeout) as client:
                tags_resp = await client.get(f"{url}/api/tags")
                if tags_resp.status_code != 200:
                    return self._unavailable_profile(now)

                available_models = [
                    m["name"] for m in tags_resp.json().get("models", [])
                ]

                ps_resp = await client.get(f"{url}/api/ps")
                compute_class = ComputeClass.CPU_ONLY

                if ps_resp.status_code == 200:
                    running = ps_resp.json().get("models", [])
                    if running:
                        model_info = running[0]
                        size = model_info.get("size", 0)
                        size_vram = model_info.get("size_vram", 0)
                        if size > 0 and size_vram > 0:
                            vram_ratio = size_vram / size
                            if vram_ratio > 0.8:
                                compute_class = ComputeClass.GPU_FULL
                            elif vram_ratio > 0.1:
                                compute_class = ComputeClass.GPU_PARTIAL
                    else:
                        compute_class = await self._infer_compute_from_generate(
                            client, url, available_models
                        )

                profile = self._build_profile(compute_class, url, available_models)
                self._profile_cache = profile
                self._cache_ts = now
                logger.info(
                    "probe: %s | model=%s | strategy=%s",
                    compute_class.value, profile.model, profile.compression_strategy
                )
                return profile

        except (httpx.ConnectError, httpx.TimeoutException) as e:
            logger.warning("Ollama unreachable at %s: %s", url, e)
            return self._unavailable_profile(now)

    async def _infer_compute_from_generate(
        self, client: httpx.AsyncClient, url: str, available_models: list
    ) -> ComputeClass:
        """Detect GPU by loading model with raw=true (no actual generation).
        Falls back to GPU_FULL if nvidia-smi detects GPUs, CPU_ONLY otherwise.
        """
        # First, try nvidia-smi for a quick GPU check without loading any model
        try:
            proc = await asyncio.create_subprocess_exec(
                "nvidia-smi", "--query-gpu=memory.total", "--format=csv,noheader,nounits",
                stdout=asyncio.subprocess.PIPE,
                stderr=asyncio.subprocess.DEVNULL,
            )
            stdout, _ = await asyncio.wait_for(proc.communicate(), timeout=5.0)
            if proc.returncode == 0 and stdout and stdout.strip():
                logger.info("probe: GPU detected via nvidia-smi, assuming gpu_full")
                return ComputeClass.GPU_FULL
        except (FileNotFoundError, asyncio.TimeoutError, OSError):
            pass

        # Fallback: load a small model with raw=true to check VRAM allocation
        preferred = ["phi3.5:3b", "mistral:7b-instruct", "qwen2.5:7b"]
        model = next((m for m in preferred if m in available_models), None)
        if not model and available_models:
            model = available_models[0]
        if not model:
            return ComputeClass.CPU_ONLY

        try:
            await client.post(
                f"{url}/api/generate",
                json={"model": model, "prompt": "", "raw": True, "stream": False},
                timeout=30.0,
            )
            ps_resp = await client.get(f"{url}/api/ps")
            if ps_resp.status_code == 200:
                running = ps_resp.json().get("models", [])
                if running:
                    size = running[0].get("size", 0)
                    size_vram = running[0].get("size_vram", 0)
                    if size > 0 and size_vram / size > 0.8:
                        return ComputeClass.GPU_FULL
                    elif size > 0 and size_vram > 0:
                        return ComputeClass.GPU_PARTIAL
        except Exception as e:
            logger.warning("probe: generate-based compute inference failed: %s", e)
        return ComputeClass.CPU_ONLY

    def _build_profile(
        self, compute_class: ComputeClass, endpoint_url: str,
        available_models: list
    ) -> InferenceProfile:
        s = self.settings
        if compute_class in (ComputeClass.GPU_FULL, ComputeClass.GPU_PARTIAL):
            preferred = s.gpu_compression_model
            fallback = s.gpu_fast_model
            strategy = "full"
            max_ctx = s.gpu_max_context
        else:
            preferred = s.cpu_compression_model
            fallback = s.cpu_fast_model
            strategy = "light"
            max_ctx = s.cpu_max_context

        model = preferred
        if preferred not in available_models:
            if fallback in available_models:
                model = fallback
            elif available_models:
                model = available_models[0]
            else:
                model = ""
                strategy = "passthrough"

        return InferenceProfile(
            compute_class=compute_class,
            endpoint_url=endpoint_url,
            model=model,
            max_context=max_ctx,
            compression_strategy=strategy,
        )

    def _unavailable_profile(self, now: float) -> InferenceProfile:
        profile = InferenceProfile(
            compute_class=ComputeClass.UNAVAILABLE,
            endpoint_url="",
            model="",
            max_context=0,
            compression_strategy="passthrough",
        )
        self._profile_cache = profile
        self._cache_ts = now
        return profile

    def invalidate_cache(self) -> None:
        self._cache_ts = 0
