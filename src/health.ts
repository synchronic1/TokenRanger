/**
 * Health check for the Python compression service.
 */

import { fetchWithSsrFGuard } from "openclaw/plugin-sdk";

/** SSRF policy: TokenRanger calls local/LAN services only (admin-configured). */
const LOCAL_POLICY = { allowPrivateNetwork: true } as const;

export type HealthStatus = {
  status: "healthy" | "degraded" | "unavailable";
  computeClass?: string;
  strategy?: string;
  model?: string;
  endpoint?: string;
};

export async function checkServiceHealth(
  serviceUrl: string,
  timeoutMs = 3000,
): Promise<HealthStatus> {
  try {
    const { response, release } = await fetchWithSsrFGuard({
      url: `${serviceUrl}/health`,
      timeoutMs,
      policy: LOCAL_POLICY,
      auditContext: "tokenranger-health",
    });

    try {
      if (!response.ok) {
        return { status: "unavailable" };
      }

      const data = (await response.json()) as Record<string, unknown>;

      return {
        status: data.status === "ok" ? "healthy" : "degraded",
        computeClass: data.compute_class as string | undefined,
        strategy: data.strategy as string | undefined,
        model: data.model as string | undefined,
        endpoint: data.endpoint as string | undefined,
      };
    } finally {
      await release();
    }
  } catch {
    return { status: "unavailable" };
  }
}
