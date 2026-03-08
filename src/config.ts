/**
 * Configuration parsing for tokenranger plugin.
 * Follows the memory-lancedb pattern: manual parsing with defaults.
 */

export type CompressionStrategy = "auto" | "full" | "light" | "passthrough";
export type InferenceMode = "auto" | "cpu" | "gpu" | "remote";

export type TokenRangerConfig = {
  serviceUrl: string;
  timeoutMs: number;
  minPromptLength: number;
  ollamaUrl: string;
  preferredModel: string;
  compressionStrategy: CompressionStrategy;
  inferenceMode: InferenceMode;
  metricsEnabled: boolean;
  metricsUrl: string;
};

const DEFAULTS: TokenRangerConfig = {
  serviceUrl: "http://127.0.0.1:8100",
  timeoutMs: 10_000,
  minPromptLength: 500,
  ollamaUrl: "http://127.0.0.1:11434",
  preferredModel: "qwen3:8b",
  compressionStrategy: "auto",
  inferenceMode: "auto",
  metricsEnabled: false,
  metricsUrl: "http://192.168.1.203:8101",
};

const VALID_STRATEGIES: CompressionStrategy[] = ["auto", "full", "light", "passthrough"];
const VALID_MODES: InferenceMode[] = ["auto", "cpu", "gpu", "remote"];

export function parseConfig(value: unknown): TokenRangerConfig {
  if (!value || typeof value !== "object" || Array.isArray(value)) {
    return { ...DEFAULTS };
  }
  const raw = value as Record<string, unknown>;
  return {
    serviceUrl: typeof raw.serviceUrl === "string" ? raw.serviceUrl : DEFAULTS.serviceUrl,
    timeoutMs: typeof raw.timeoutMs === "number" ? raw.timeoutMs : DEFAULTS.timeoutMs,
    minPromptLength:
      typeof raw.minPromptLength === "number" ? raw.minPromptLength : DEFAULTS.minPromptLength,
    ollamaUrl: typeof raw.ollamaUrl === "string" ? raw.ollamaUrl : DEFAULTS.ollamaUrl,
    preferredModel:
      typeof raw.preferredModel === "string" ? raw.preferredModel : DEFAULTS.preferredModel,
    compressionStrategy:
      typeof raw.compressionStrategy === "string" &&
      VALID_STRATEGIES.includes(raw.compressionStrategy as CompressionStrategy)
        ? (raw.compressionStrategy as CompressionStrategy)
        : DEFAULTS.compressionStrategy,
    inferenceMode:
      typeof raw.inferenceMode === "string" &&
      VALID_MODES.includes(raw.inferenceMode as InferenceMode)
        ? (raw.inferenceMode as InferenceMode)
        : DEFAULTS.inferenceMode,
    metricsEnabled:
      typeof raw.metricsEnabled === "boolean" ? raw.metricsEnabled : DEFAULTS.metricsEnabled,
    metricsUrl: typeof raw.metricsUrl === "string" ? raw.metricsUrl : DEFAULTS.metricsUrl,
  };
}

export const tokenRangerConfigSchema = {
  parse: parseConfig,
};
