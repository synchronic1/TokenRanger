/**
 * Health check for the Python compression service.
 */

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
  const controller = new AbortController();
  const timer = setTimeout(() => controller.abort(), timeoutMs);

  try {
    const res = await fetch(`${serviceUrl}/health`, {
      signal: controller.signal,
    });

    if (!res.ok) {
      return { status: "unavailable" };
    }

    const data = (await res.json()) as Record<string, unknown>;

    return {
      status: data.status === "ok" ? "healthy" : "degraded",
      computeClass: data.compute_class as string | undefined,
      strategy: data.strategy as string | undefined,
      model: data.model as string | undefined,
      endpoint: data.endpoint as string | undefined,
    };
  } catch {
    return { status: "unavailable" };
  } finally {
    clearTimeout(timer);
  }
}
