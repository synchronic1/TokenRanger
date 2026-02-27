import time
import logging
from typing import Optional

from fastapi import FastAPI
from pydantic import BaseModel

from config import Settings
from inference_router import InferenceRouter
from compressor import ContextCompressor

settings = Settings()
logging.basicConfig(
    level=getattr(logging, settings.log_level),
    format="%(asctime)s [%(name)s] %(levelname)s: %(message)s",
)
logger = logging.getLogger("tokenranger")

app = FastAPI(title="OpenClaw TokenRanger Service", version="0.2.0")

router = InferenceRouter(settings)
compressor = ContextCompressor(router)


class CompressRequest(BaseModel):
    prompt: str
    session_history: str = ""
    lance_results: str = ""
    max_tokens: int = 2000
    model_override: Optional[str] = None
    strategy_override: Optional[str] = None


class CompressResponse(BaseModel):
    compressed_context: str
    compute_class: str
    model_used: str
    original_chars: int
    compressed_chars: int
    reduction_pct: float
    latency_ms: float


@app.post("/compress", response_model=CompressResponse)
async def compress(req: CompressRequest):
    start = time.monotonic()

    # Fast-path: return passthrough for empty/trivial input
    total_input = len(req.session_history.strip()) + len(req.lance_results.strip())
    if total_input < 50:
        logger.debug("compress: trivial input (%d chars), returning passthrough", total_input)
        passthrough = compressor._passthrough(req.session_history, req.lance_results)
        return CompressResponse(
            compressed_context=passthrough,
            compute_class="passthrough",
            model_used="none",
            original_chars=total_input,
            compressed_chars=len(passthrough),
            reduction_pct=0.0,
            latency_ms=0.0,
        )

    result, profile = await compressor.compress(
        req.session_history, req.lance_results, req.prompt,
        model_override=req.model_override,
        strategy_override=req.strategy_override,
    )

    elapsed_ms = (time.monotonic() - start) * 1000
    original = len(req.session_history) + len(req.lance_results)
    compressed = len(result)
    reduction = ((original - compressed) / max(original, 1)) * 100

    logger.info(
        "compress: %s | %s | %d->%d chars (%.0f%%) | %.0fms",
        profile.compute_class.value, profile.model,
        original, compressed, reduction, elapsed_ms,
    )

    return CompressResponse(
        compressed_context=result,
        compute_class=profile.compute_class.value,
        model_used=profile.model,
        original_chars=original,
        compressed_chars=compressed,
        reduction_pct=round(reduction, 1),
        latency_ms=round(elapsed_ms, 1),
    )


@app.get("/health")
async def health():
    profile = await router.probe()
    if profile.compute_class.value == "unavailable":
        status = "degraded"
    elif profile.compression_strategy == "passthrough":
        status = "degraded"
    else:
        status = "ok"
    return {
        "status": status,
        "compute_class": profile.compute_class.value,
        "endpoint": profile.endpoint_url,
        "model": profile.model,
        "strategy": profile.compression_strategy,
    }


@app.post("/invalidate-cache")
async def invalidate():
    router.invalidate_cache()
    profile = await router.probe()
    return {
        "status": "refreshed",
        "compute_class": profile.compute_class.value,
    }
