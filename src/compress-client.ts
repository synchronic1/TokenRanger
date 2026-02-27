/**
 * HTTP client for the Python compression service.
 */

import { fetchWithSsrFGuard } from "openclaw/plugin-sdk";

/** SSRF policy: TokenRanger calls local/LAN services only (admin-configured). */
const LOCAL_POLICY = { allowPrivateNetwork: true } as const;

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

export async function compressContext(req: CompressRequest): Promise<CompressResult | null> {
  try {
    const { response, release } = await fetchWithSsrFGuard({
      url: `${req.serviceUrl}/compress`,
      init: {
        method: "POST",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify({
          prompt: req.prompt,
          session_history: req.sessionHistory,
          lance_results: "",
          ...(req.modelOverride ? { model_override: req.modelOverride } : {}),
          ...(req.strategyOverride ? { strategy_override: req.strategyOverride } : {}),
        }),
      },
      timeoutMs: req.timeoutMs,
      policy: LOCAL_POLICY,
      auditContext: "tokenranger-compress",
    });

    try {
      if (!response.ok) {
        return null;
      }

      const data = (await response.json()) as Record<string, unknown>;

      return {
        compressedContext: data.compressed_context as string,
        computeClass: data.compute_class as string,
        modelUsed: data.model_used as string,
        originalChars: data.original_chars as number,
        compressedChars: data.compressed_chars as number,
        reductionPct: data.reduction_pct as number,
        latencyMs: data.latency_ms as number,
      };
    } finally {
      await release();
    }
  } catch {
    return null;
  }
}
