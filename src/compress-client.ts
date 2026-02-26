/**
 * HTTP client for the Python compression service.
 */

export type CompressRequest = {
  prompt: string;
  sessionHistory: string;
  serviceUrl: string;
  timeoutMs: number;
  modelOverride?: string;
  strategyOverride?: string;
};

export type CompressResult = {
  compressedContext: string;
  computeClass: string;
  modelUsed: string;
  originalChars: number;
  compressedChars: number;
  reductionPct: number;
  latencyMs: number;
};

export async function compressContext(
  req: CompressRequest,
): Promise<CompressResult | null> {
  const controller = new AbortController();
  const timer = setTimeout(() => controller.abort(), req.timeoutMs);

  try {
    const res = await fetch(`${req.serviceUrl}/compress`, {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({
        prompt: req.prompt,
        session_history: req.sessionHistory,
        lance_results: "",
        ...(req.modelOverride ? { model_override: req.modelOverride } : {}),
        ...(req.strategyOverride ? { strategy_override: req.strategyOverride } : {}),
      }),
      signal: controller.signal,
    });

    if (!res.ok) {
      return null;
    }

    const data = (await res.json()) as Record<string, unknown>;

    return {
      compressedContext: data.compressed_context as string,
      computeClass: data.compute_class as string,
      modelUsed: data.model_used as string,
      originalChars: data.original_chars as number,
      compressedChars: data.compressed_chars as number,
      reductionPct: data.reduction_pct as number,
      latencyMs: data.latency_ms as number,
    };
  } catch {
    return null;
  } finally {
    clearTimeout(timer);
  }
}
