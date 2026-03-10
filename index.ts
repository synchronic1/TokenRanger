/**
 * OpenClaw TokenRanger Plugin
 *
 * Compresses session context via a local SLM (Ollama) before sending
 * to expensive cloud LLMs, reducing token costs by 50-80%.
 *
 * Architecture:
 *   before_agent_start hook → HTTP POST localhost:8100/compress →
 *   FastAPI (LangChain + Ollama) → compressed context →
 *   { prependContext } returned to gateway
 *
 * Install: openclaw tokenranger setup
 */

import os from "node:os";
import path from "node:path";
import { fileURLToPath } from "node:url";
import { fetchWithSsrFGuard, type OpenClawPluginApi } from "openclaw/plugin-sdk";
import { compressContext } from "./src/compress-client.js";
import { parseConfig, tokenRangerConfigSchema } from "./src/config.js";
import type { TokenRangerConfig } from "./src/config.js";
import { checkServiceHealth } from "./src/health.js";
import { detectPlatform, resolveServiceDir } from "./src/platform.js";
import {
  checkPrerequisites,
  installOllama,
  installPythonService,
  pullOllamaModel,
  installLaunchdService,
  installSystemdService,
  uninstallService,
  verifySetup,
} from "./src/setup.js";

const tokenRangerPlugin = {
  id: "tokenranger",
  name: "TokenRanger",
  description:
    "Compresses session context via local SLM before cloud LLM calls, reducing token costs by 50-80%",
  configSchema: tokenRangerConfigSchema,

  register(api: OpenClawPluginApi) {
    // Mutable cfg — updated in-memory by /tokenranger command, takes effect immediately
    let cfg: TokenRangerConfig = parseConfig(api.pluginConfig);
    const nodeHostname = os.hostname();

    // Detect if the chat model is local (Ollama/MLX) and adjust timeouts
    let isLocalChatModel = false;
    try {
      const fullConfig = api.runtime.config.loadConfig();
      const modelCfg = fullConfig?.agents?.defaults?.model;
      const primaryModel =
        typeof modelCfg === "string"
          ? modelCfg
          : (modelCfg as Record<string, unknown> | undefined)?.primary as string ?? "";
      isLocalChatModel =
        primaryModel.startsWith("ollama/") || primaryModel.startsWith("mlx-local/");

      if (isLocalChatModel) {
        // Auto-increase compression timeout for local models (Apple Silicon/CPU
        // compression takes 3-14s vs 1-3s on NVIDIA GPU)
        if (cfg.timeoutMs <= 10_000) {
          cfg.timeoutMs = 30_000;
          api.logger.info(
            `[tokenranger] Local chat model detected (${primaryModel}). ` +
              `Compression timeout increased to ${cfg.timeoutMs}ms`,
          );
        }

        // Ensure agent timeout is sufficient for local model inference + compression.
        // Local models need: compression (3-14s) + model swap (5-15s) + inference (30-120s).
        // Minimum recommended: 300s.
        const agentTimeout = fullConfig?.agents?.defaults?.timeoutSeconds ?? 600;
        if (agentTimeout < 300) {
          const updated = { ...fullConfig };
          updated.agents = updated.agents ?? {};
          updated.agents.defaults = updated.agents.defaults ?? {};
          updated.agents.defaults.timeoutSeconds = 300;
          api.runtime.config.writeConfigFile(updated).catch((err: unknown) => {
            api.logger.warn(
              `[tokenranger] Failed to update agent timeout: ${String(err)}`,
            );
          });
          api.logger.info(
            `[tokenranger] Agent timeout was ${agentTimeout}s (too low for local models). ` +
              `Updated to 300s in config.`,
          );
        }
      }
    } catch {
      // Non-fatal: config read failure doesn't block plugin
    }

    /** Fire-and-forget metrics emit to centralized collector. Never blocks. */
    function emitMetrics(event: Record<string, unknown>): void {
      if (!cfg.metricsEnabled) return;
      try {
        fetch(`${cfg.metricsUrl}/emit`, {
          method: "POST",
          headers: { "Content-Type": "application/json" },
          body: JSON.stringify({ node: nodeHostname, ...event }),
        }).catch(() => {});
      } catch {
        // Never throw from metrics emit
      }
    }

    // ========================================================================
    // Health check on gateway start
    // ========================================================================

    api.on("gateway_start", async () => {
      try {
        const health = await checkServiceHealth(cfg.serviceUrl);
        if (health.status === "unavailable") {
          api.logger.warn(
            `[tokenranger] Compression service not running at ${cfg.serviceUrl}. ` +
              `Run: openclaw tokenranger setup`,
          );
        } else {
          api.logger.info(
            `[tokenranger] Service healthy: ` +
              `strategy=${health.strategy}, model=${health.model}, compute=${health.computeClass}`,
          );
        }
      } catch {
        api.logger.warn(
          `[tokenranger] Could not reach service at ${cfg.serviceUrl}. ` +
            `Run: openclaw tokenranger setup`,
        );
      }
    });

    // ========================================================================
    // Core hook: compress context before agent start
    // ========================================================================

    api.on("before_agent_start", async (event, ctx) => {
      // ── Build structured turn list from messages ──────────────────────
      type TurnMeta = {
        n: number;
        role: "user" | "asst";
        chars: number;
        hasCode: boolean;
      };
      const turns: TurnMeta[] = [];
      const taggedParts: string[] = [];
      let userTurnCount = 0;
      let turnIndex = 0;
      let totalCodeBlocks = 0;

      if (event.messages && Array.isArray(event.messages)) {
        for (const msg of event.messages) {
          if (!msg || typeof msg !== "object") continue;
          const m = msg as Record<string, unknown>;
          const role = m.role as string;
          if (role !== "user" && role !== "assistant") continue;

          // Handle both string content and array content blocks
          let content = "";
          if (typeof m.content === "string") {
            content = m.content;
          } else if (Array.isArray(m.content)) {
            const blocks = m.content as Array<Record<string, unknown>>;
            content = blocks
              .filter((c) => c && typeof c === "object" && c.type === "text")
              .map((c) => (c.text as string) ?? "")
              .join(" ");
          }
          if (!content) continue;

          // Strip agent step-indicator lines (e.g. "→ Checking:", "→ Executing:", "→ Restarting:")
          // from ALL roles. These are internal UI artifacts. Crucially, old compressed summaries
          // are re-injected as user messages (<session-summary>), so stripping assistant-only
          // is insufficient — the leaked arrows re-enter as user content on the next turn.
          content = content
            .split("\n")
            .filter((line) => !/^→\s*\S/.test(line.trim()))
            .join("\n")
            .trim();

          if (!content) continue;

          if (role === "user") userTurnCount++;
          turnIndex++;

          const hasCode = /```[\s\S]*?```/.test(content);
          const meta: TurnMeta = {
            n: turnIndex,
            role: role === "assistant" ? "asst" : "user",
            chars: content.length,
            hasCode,
          };
          turns.push(meta);

          // Strip code blocks — count them but remove before compression
          const codeMatches = content.match(/```[\s\S]*?```/g);
          if (codeMatches) {
            totalCodeBlocks += codeMatches.length;
            content = content.replace(/```[\s\S]*?```/g, "");
          }

          // Format size: "520c" or "1.2k"
          const sizeLabel =
            meta.chars >= 1000
              ? `${(meta.chars / 1000).toFixed(1)}k`
              : `${meta.chars}c`;
          const flags = meta.hasCode ? "|code" : "";
          taggedParts.push(`[T${meta.n}:${meta.role}|${sizeLabel}${flags}] ${content.trim()}`);
        }
      }

      // Skip turn 1: let the initial prompt go directly to the API without
      // compression. The first user message often contains critical constraints,
      // formatting specs, and "don't do X" rules that are easily lost in
      // summarization. Compression begins at turn 2+.
      if (userTurnCount <= 1) {
        api.logger.debug?.(
          `[tokenranger] Skipping turn 1: sending initial prompt directly to API (${userTurnCount} user turn(s))`,
        );
        emitMetrics({
          user_turn: userTurnCount,
          skipped: true,
          skip_reason: "turn_1",
        });
        return;
      }

      const taggedHistory = taggedParts.join("\n\n");

      // Debug: log hook invocation details
      api.logger.debug?.(
        `[tokenranger] before_agent_start: ` +
          `messages=${event.messages?.length ?? 0}, ` +
          `userTurns=${userTurnCount}, ` +
          `turns=${turns.length}, ` +
          `taggedLen=${taggedHistory.length}, ` +
          `codeBlocks=${totalCodeBlocks}, ` +
          `minRequired=${cfg.minPromptLength}, ` +
          `sessionId=${ctx?.sessionId ?? "none"}`,
      );

      // Skip if history (after code block removal) is too short to benefit
      if (taggedHistory.length < cfg.minPromptLength) {
        api.logger.debug?.(
          `[tokenranger] Skipping: history too short after code strip (${taggedHistory.length} < ${cfg.minPromptLength})`,
        );
        emitMetrics({
          user_turn: userTurnCount,
          original_chars: taggedHistory.length,
          skipped: true,
          skip_reason: "trivial_input",
        });
        return;
      }

      // Compute strategy/model overrides from inferenceMode and compressionStrategy
      let strategyOverride: string | undefined;
      if (cfg.compressionStrategy !== "auto") {
        // Explicit compressionStrategy takes priority
        strategyOverride = cfg.compressionStrategy;
      } else if (cfg.inferenceMode !== "auto") {
        // inferenceMode maps to strategy
        strategyOverride =
          cfg.inferenceMode === "cpu"
            ? "light"
            : cfg.inferenceMode === "gpu"
              ? "full"
              : cfg.inferenceMode === "remote"
                ? "full"
                : undefined;
      }

      const modelOverride = cfg.preferredModel ? cfg.preferredModel : undefined;

      try {
        // Scale compression timeout with input size for local models.
        // Base timeout + 5ms per char keeps pace with ~200 tok/s throughput.
        let effectiveTimeout = cfg.timeoutMs;
        if (isLocalChatModel && taggedHistory.length > 2000) {
          effectiveTimeout = Math.max(
            cfg.timeoutMs,
            Math.min(cfg.timeoutMs + Math.ceil(taggedHistory.length * 5), 120_000),
          );
        }

        const result = await compressContext({
          prompt: event.prompt ?? "",
          sessionHistory: taggedHistory,
          serviceUrl: cfg.serviceUrl,
          timeoutMs: effectiveTimeout,
          strategyOverride,
          modelOverride,
          turnMeta: turns,
        });

        if (!result) {
          api.logger.debug?.("[tokenranger] compressContext returned null (service error or timeout)");
          return;
        }
        if (result.reductionPct < 5) {
          api.logger.debug?.(
            `[tokenranger] Skipping: reduction too low (${result.reductionPct}%)`,
          );
          emitMetrics({
            user_turn: userTurnCount,
            original_chars: result.originalChars,
            compressed_chars: result.compressedChars,
            char_reduction_pct: result.reductionPct,
            skipped: true,
            skip_reason: "low_reduction",
          });
          return;
        }

        api.logger.info(
          `[tokenranger] Compressed: ${result.originalChars} → ${result.compressedChars} chars ` +
            `(${result.reductionPct}% reduction, ${result.latencyMs}ms, ${result.computeClass})`,
        );

        emitMetrics({
          session_id: ctx?.sessionId,
          user_turn: userTurnCount,
          original_chars: result.originalChars,
          compressed_chars: result.compressedChars,
          char_reduction_pct: result.reductionPct,
          compute_class: result.computeClass,
          model_used: result.modelUsed,
          strategy: strategyOverride ?? "auto",
          latency_ms: result.latencyMs,
          code_blocks_stripped: totalCodeBlocks,
          skipped: false,
        });

        // Note: prependContext is the only context-injection mechanism available
        // in the before_agent_start hook (same pattern as memory-lancedb).
        // The SDK does not currently support replacing session messages from hooks.
        // The compressed summary is prepended to the prompt, providing a dense
        // representation that works alongside the gateway's context-window management.
        return {
          prependContext: result.compressedContext,
        };
      } catch (err) {
        // Graceful degradation: proceed without compression
        api.logger.warn(`[tokenranger] Compression failed, passing through: ${String(err)}`);
      }
    });

    // ========================================================================
    // Slash command: /tokenranger — interactive settings
    // ========================================================================

    api.registerCommand({
      name: "tokenranger",
      description: "TokenRanger settings — mode, model, enable/disable",
      acceptsArgs: true,
      requireAuth: false,
      handler: async (ctx) => {
        const args = (ctx.args ?? "").trim();
        const isTelegram = ctx.channel === "telegram";

        // Helper: persist a config change to disk and update in-memory
        async function updatePluginConfig(patch: Partial<TokenRangerConfig>) {
          Object.assign(cfg, patch);
          try {
            const fresh = api.runtime.config.loadConfig();
            const entries = fresh.plugins?.entries ?? {};
            const entry = entries["tokenranger"] ?? {};
            const entryConfig = entry.config ?? {};
            Object.assign(entryConfig, patch);
            entry.config = entryConfig;
            entries["tokenranger"] = entry;
            fresh.plugins = fresh.plugins ?? {};
            fresh.plugins.entries = entries;
            await api.runtime.config.writeConfigFile(fresh);
          } catch (err) {
            api.logger.warn(`[tokenranger] config write failed: ${String(err)}`);
          }
        }

        // ── /tokenranger (no args) — Main Menu ──────────────────────────
        if (!args) {
          let serviceInfo = "unreachable";
          try {
            const health = await checkServiceHealth(cfg.serviceUrl, 2000);
            if (health.status === "healthy") {
              serviceInfo = `healthy | ${health.computeClass ?? "?"} | ${health.model ?? "?"}`;
            } else if (health.status === "degraded") {
              serviceInfo = "degraded";
            }
          } catch {
            /* keep unreachable */
          }

          const mode = cfg.inferenceMode ?? "auto";
          const model = cfg.preferredModel ?? "(default)";
          const shortModel = model.length > 12 ? model.slice(0, 12) : model;

          let enabled = true;
          try {
            const fresh = api.runtime.config.loadConfig();
            enabled = fresh.plugins?.entries?.["tokenranger"]?.enabled !== false;
          } catch {
            /* assume enabled */
          }

          const text = [
            "TokenRanger Settings",
            "",
            `Service: ${serviceInfo}`,
            `Mode: ${mode} | Model: ${model}`,
            `Enabled: ${enabled ? "yes" : "no"}`,
          ].join("\n");

          if (isTelegram) {
            return {
              text,
              channelData: {
                telegram: {
                  buttons: [
                    [
                      { text: `Mode: ${mode}`, callback_data: "/tokenranger mode" },
                      { text: `Model: ${shortModel}`, callback_data: "/tokenranger model" },
                    ],
                    [
                      {
                        text: enabled ? "Enabled: ON" : "Enabled: OFF",
                        callback_data: "/tokenranger toggle",
                      },
                    ],
                  ],
                },
              },
            };
          }

          return { text };
        }

        // ── /tokenranger mode ────────────────────────────────────────────
        if (args === "mode") {
          const current = cfg.inferenceMode ?? "auto";
          const text = `Select inference mode (current: ${current}):`;

          if (isTelegram) {
            return {
              text,
              channelData: {
                telegram: {
                  buttons: [
                    [
                      { text: "CPU", callback_data: "/tokenranger mode cpu" },
                      { text: "GPU", callback_data: "/tokenranger mode gpu" },
                    ],
                    [
                      { text: "Remote", callback_data: "/tokenranger mode remote" },
                      { text: "Auto", callback_data: "/tokenranger mode auto" },
                    ],
                    [{ text: "<< Back", callback_data: "/tokenranger" }],
                  ],
                },
              },
            };
          }

          return {
            text: text + "\nOptions: cpu, gpu, remote, auto\nUse: /tokenranger mode <option>",
          };
        }

        // ── /tokenranger mode <value> ────────────────────────────────────
        const modeMatch = args.match(/^mode (auto|cpu|gpu|remote)$/);
        if (modeMatch) {
          const newMode = modeMatch[1] as "auto" | "cpu" | "gpu" | "remote";
          await updatePluginConfig({ inferenceMode: newMode });

          const desc =
            newMode === "cpu"
              ? "light strategy, local Ollama"
              : newMode === "gpu"
                ? "full strategy, local GPU Ollama"
                : newMode === "remote"
                  ? "full strategy, remote Ollama"
                  : "auto-detect via Ollama probe";

          api.logger.info(`[tokenranger] inferenceMode set to ${newMode}`);

          return { text: `Inference mode set to: ${newMode} (${desc})` };
        }

        // ── /tokenranger model ───────────────────────────────────────────
        if (args === "model") {
          let models: string[] = [];
          try {
            const { response, release } = await fetchWithSsrFGuard({
              url: `${cfg.ollamaUrl}/api/tags`,
              timeoutMs: 3000,
              policy: { allowPrivateNetwork: true },
              auditContext: "tokenranger-ollama-tags",
            });
            try {
              const data = (await response.json()) as { models?: Array<{ name: string }> };
              models = (data.models ?? []).map((m) => m.name);
            } finally {
              await release();
            }
          } catch {
            return { text: `Could not reach Ollama at ${cfg.ollamaUrl} to list models.` };
          }

          if (models.length === 0) {
            return {
              text: "No models found in Ollama. Pull a model first: ollama pull qwen3:8b",
            };
          }

          const current = cfg.preferredModel ?? "";

          if (isTelegram) {
            const buttonRows: Array<Array<{ text: string; callback_data: string }>> = [];
            for (let i = 0; i < Math.min(models.length, 8); i += 2) {
              const row: Array<{ text: string; callback_data: string }> = [];
              for (let j = i; j < Math.min(i + 2, models.length, 8); j++) {
                const name = models[j];
                const cbData = `/tokenranger model ${name}`;
                // Skip models that exceed Telegram's 64-byte callback_data limit
                if (cbData.length > 64) continue;
                const label = name === current ? `${name.slice(0, 18)} ✓` : name.slice(0, 20);
                row.push({ text: label, callback_data: cbData });
              }
              buttonRows.push(row);
            }
            buttonRows.push([{ text: "<< Back", callback_data: "/tokenranger" }]);

            return {
              text: `Select model (current: ${current || "default"}):`,
              channelData: { telegram: { buttons: buttonRows } },
            };
          }

          const list = models
            .map((m) => (m === current ? `  ${m} (current)` : `  ${m}`))
            .join("\n");

          return { text: `Available models:\n${list}\n\nUse: /tokenranger model <name>` };
        }

        // ── /tokenranger model <name> ────────────────────────────────────
        const modelMatch = args.match(/^model (.+)$/);
        if (modelMatch) {
          const newModel = modelMatch[1].trim();
          if (!/^[\w.\-/:]+$/.test(newModel)) {
            return { text: `Invalid model name: ${newModel}` };
          }
          await updatePluginConfig({ preferredModel: newModel });
          api.logger.info(`[tokenranger] preferredModel set to ${newModel}`);

          return { text: `Preferred model set to: ${newModel}` };
        }

        // ── /tokenranger toggle ──────────────────────────────────────────
        if (args === "toggle") {
          try {
            const fresh = api.runtime.config.loadConfig();
            const entries = fresh.plugins?.entries ?? {};
            const entry = entries["tokenranger"] ?? {};
            const currentEnabled = entry.enabled !== false;
            const newEnabled = !currentEnabled;
            entry.enabled = newEnabled;
            entries["tokenranger"] = entry;
            fresh.plugins = fresh.plugins ?? {};
            fresh.plugins.entries = entries;
            await api.runtime.config.writeConfigFile(fresh);
            api.logger.info(`[tokenranger] plugin ${newEnabled ? "enabled" : "disabled"}`);

            return {
              text: `TokenRanger ${newEnabled ? "enabled" : "disabled"}. Restart gateway to take effect.`,
            };
          } catch (err) {
            return { text: `Failed to toggle: ${String(err)}` };
          }
        }

        // ── fallback ─────────────────────────────────────────────────────
        return {
          text:
            "Usage: /tokenranger [mode|model|toggle]\n\n" +
            "/tokenranger — show settings\n" +
            "/tokenranger mode — set inference mode (cpu/gpu/remote/auto)\n" +
            "/tokenranger model — select Ollama model\n" +
            "/tokenranger toggle — enable/disable",
        };
      },
    });

    // ========================================================================
    // CLI: setup / status / uninstall
    // ========================================================================

    api.registerCli(
      ({ program }: { program: any }) => {
        const cmd = program
          .command("tokenranger")
          .description("TokenRanger context compression plugin commands");

        cmd
          .command("setup")
          .description("Install the Python compression service and Ollama models")
          .option("--skip-ollama", "Skip Ollama model pull")
          .option("--skip-service", "Skip system service creation")
          .option("--venv-path <path>", "Custom installation directory")
          .action(async (opts: Record<string, unknown>) => {
            const logger = {
              info: (msg: string) => console.log(msg),
              warn: (msg: string) => console.log(`⚠ ${msg}`),
              error: (msg: string) => console.error(`✗ ${msg}`),
            };

            console.log("\n  TokenRanger — Setup\n");
            console.log("  Checking prerequisites...");

            if (!checkPrerequisites(logger)) {
              console.error(
                "\n  Prerequisites not met. Install python3 >= 3.10 and pip, then retry.",
              );
              process.exit(1);
            }

            const platform = detectPlatform();
            console.log(`\n  Platform: ${platform.os} (${platform.serviceManager})`);

            const serviceDir =
              typeof opts.venvPath === "string" ? opts.venvPath : resolveServiceDir();
            // Resolve the plugin directory (where service/ files live)
            // When loaded as ./index.ts, dirname is already the plugin root.
            // When loaded as dist/index.js, we need to go up one level.
            const thisDir = path.dirname(fileURLToPath(import.meta.url));
            const pluginDir =
              path.basename(thisDir) === "dist" ? path.resolve(thisDir, "..") : thisDir;

            console.log(`  Install directory: ${serviceDir}\n`);

            // Step 1: Install Ollama (if missing)
            if (!opts.skipOllama) {
              console.log("  Step 1/5: Checking Ollama...");
              installOllama(logger);
            } else {
              console.log("  Step 1/5: Skipped Ollama install");
            }

            // Step 2: Install Python service
            console.log("\n  Step 2/5: Installing Python service...");
            installPythonService(serviceDir, pluginDir, logger);

            // Step 3: Pull default Ollama model
            if (!opts.skipOllama) {
              console.log("\n  Step 3/5: Pulling Ollama model...");
              pullOllamaModel(cfg.preferredModel, cfg.ollamaUrl, logger);
            } else {
              console.log("\n  Step 3/5: Skipped Ollama model pull");
            }

            // Step 4: Install system service
            if (!opts.skipService) {
              console.log("\n  Step 4/5: Installing system service...");
              if (platform.serviceManager === "launchd") {
                installLaunchdService(serviceDir, pluginDir, cfg, logger);
              } else if (platform.serviceManager === "systemd") {
                installSystemdService(serviceDir, pluginDir, cfg, logger);
              } else {
                logger.warn("  No supported service manager. Start manually:");
                logger.info(
                  `  cd ${serviceDir} && venv/bin/uvicorn main:app --host 127.0.0.1 --port 8100`,
                );
              }
            } else {
              console.log("\n  Step 4/5: Skipped system service creation");
            }

            // Step 5: Verify
            console.log("\n  Step 5/5: Verifying...");
            const ok = await verifySetup(cfg.serviceUrl, logger);
            if (ok) {
              console.log("\n  Setup complete! Restart the gateway: openclaw gateway restart\n");
            } else {
              console.log("\n  Setup finished but service may need manual start.\n");
            }
          });

        cmd
          .command("status")
          .description("Check compression service health")
          .action(async () => {
            const health = await checkServiceHealth(cfg.serviceUrl);
            console.log(JSON.stringify(health, null, 2));
          });

        cmd
          .command("uninstall")
          .description("Remove the Python service and system service")
          .action(async () => {
            const logger = {
              info: (msg: string) => console.log(msg),
              warn: (msg: string) => console.log(`⚠ ${msg}`),
              error: (msg: string) => console.error(`✗ ${msg}`),
            };

            console.log("\n  TokenRanger — Uninstall\n");
            uninstallService(logger);
            console.log("\n  Done. Restart the gateway: openclaw gateway restart\n");
          });
      },
      { commands: ["tokenranger"] },
    );

    // ========================================================================
    // Service registration
    // ========================================================================

    api.registerService({
      id: "tokenranger",
      start: () => {
        api.logger.info(`[tokenranger] registered (serviceUrl: ${cfg.serviceUrl})`);
      },
      stop: () => {
        api.logger.info("[tokenranger] stopped");
      },
    });
  },
};

export default tokenRangerPlugin;
