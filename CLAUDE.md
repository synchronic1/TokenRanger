# CLAUDE.md

This file provides guidance to Claude Code (claude.ai/code) when working with code in this repository.

## What is TokenRanger

OpenClaw plugin that compresses conversation context via a local SLM (Ollama) before sending to cloud LLMs, reducing input token costs by 50-80% with 1-3s latency overhead. Two layers:

- **TypeScript plugin** (`index.ts`, `src/`) — hooks `before_agent_start` to intercept messages, provides `/tokenranger` slash command for settings, CLI subcommands for setup/status/uninstall
- **Python FastAPI service** (`service/`) — runs on port 8100, uses LangChain LCEL chains + Ollama for compression, with an inference router that probes GPU availability

## Architecture

```
User message → OpenClaw Gateway → before_agent_start hook (index.ts)
                                    ↓
                                compress-client.ts → POST localhost:8100/compress
                                                        ↓
                                                    inference_router.py → probes Ollama /api/ps
                                                    compressor.py → LangChain LCEL chain
                                                        ↓
                                { prependContext } returned to plugin
                                    ↓
                                Cloud LLM receives compressed context
```

Graceful degradation: service down → full passthrough; Ollama down → truncate last 20 lines; timeout → AbortController cancels, full context sent. The plugin never blocks the gateway.

## Build & Development

```bash
# Build TypeScript plugin
npm install && npm run build    # outputs to dist/

# Clean build artifacts
npm run clean

# No unit test suite or linter — testing is done live against OpenClaw deployments
# See TESTING.md for verification procedures
```

TypeScript: strict mode, ES2022 target, Node16 module resolution. No runtime npm dependencies — only `@types/node` and `typescript` as devDeps. Python service files are copied as-is (no build step).

## Deployment

Deployed on two machines: **pvet630** (3x NVIDIA GPU, primary) and **r430a** (CPU-only VM, offloads to pvet630 GPU over LAN).

```bash
# Install via OpenClaw CLI
openclaw plugins enable tokenranger
openclaw tokenranger setup              # installs Python venv, pulls Ollama model, creates systemd unit
openclaw tokenranger setup --skip-ollama  # skip model pull
openclaw gateway restart

# Service management (Linux)
systemctl --user status openclaw-tokenranger.service
journalctl --user -u openclaw-tokenranger.service

# Service management (macOS)
launchctl load ~/Library/LaunchAgents/com.openclaw.tokenranger.plist

# Health / debug
curl http://127.0.0.1:8100/health
curl -X POST http://127.0.0.1:8100/invalidate-cache   # reset GPU detection cache
tail -f ~/.openclaw/logs/tokenranger.log

# Sync source changes to deployed service
cp service/*.py ~/.openclaw/services/tokenranger/
systemctl --user restart openclaw-tokenranger.service
```

Deployed paths: `~/.openclaw/extensions/tokenranger/` (built JS), `~/.openclaw/services/tokenranger/` (Python venv + service).

Remote GPU config for r430a: `TOKENRANGER_OLLAMA_BASE_URL=http://192.168.1.242:11434`

### Local Model Enablement (macOS)

TokenRanger extends small local models' effective context by compressing history each turn.
A 32k-context model behaves like 160k+ with 74-83% compression. OpenClaw's hard minimum
is 16k tokens (`CONTEXT_WINDOW_HARD_MIN_TOKENS` in `context-window-guard.ts`).

macOS setup uses launchd (`com.openclaw.tokenranger.plist`) with env vars:
`TOKENRANGER_GPU_COMPRESSION_MODEL=qwen3:1.7b`, `TOKENRANGER_GPU_FAST_MODEL=qwen3:1.7b`.
The `preferredModel` in `openclaw.json` plugin config overrides the inference router's
model selection, so both must be set consistently.

Python 3.9 (macOS system Python) requires `from __future__ import annotations` in service files.

### Automatic Timeout Adjustment

When the plugin detects a local chat model (`ollama/` or `mlx-local/` prefix in
`agents.defaults.model.primary`), it automatically:
1. Increases compression timeout from 10s → 30s (local compression is slower)
2. Scales timeout dynamically with input size (5ms/char, capped at 120s)
3. Ensures `agents.defaults.timeoutSeconds` is at least 300s (updates config if lower)

## Metrics Collector (`metrics-collector/`)

Centralized FastAPI service on CT 203 (port 8101, `TRMX_` env prefix) that aggregates compression events from all nodes.

- `main.py` — Endpoints: `POST /emit`, `GET /summary`, `/events`, `/export`, `/compare`, `/metrics`
- `metrics_store.py` — SQLite-backed storage at `/opt/tokenranger-metrics/metrics.db`, 30-day retention
- `usage_poller.py` — Polls OpenClaw `/usage.status` every 5 min; `PruneScheduler` runs daily cleanup
- `config.py` — `MetricsConfig` with tracked nodes: `pvet630:192.168.1.242,r430a:192.168.1.240`

The plugin emits metrics fire-and-forget (never blocks the gateway). Skipped events (turn 1, trivial input) are also logged.

## Key Source Files

| File | Role |
|------|------|
| `index.ts` | Plugin entry: mutable `cfg` ref, `/tokenranger` command handler, `before_agent_start` hook with content extraction (handles array content blocks) |
| `src/config.ts` | `TokenRangerConfig` Zod schema with `inferenceMode` field |
| `src/compress-client.ts` | HTTP client; `CompressRequest` with `modelOverride`/`strategyOverride` |
| `src/setup.ts` | CLI `openclaw tokenranger setup/status/uninstall`; platform detection; systemd/launchd templates |
| `src/health.ts` | Health check client |
| `src/platform.ts` | OS/platform detection for service installation |
| `service/main.py` | FastAPI app on port 8100; `/compress` and `/health` endpoints; `CompressRequest` Pydantic model with override fields |
| `service/compressor.py` | LangChain LCEL compression; `full`/`light`/`passthrough` strategies; honors model/strategy overrides; trivial-input guard (<50 chars → passthrough) |
| `service/inference_router.py` | Probes Ollama `/api/ps` for GPU VRAM; caches result; selects compression strategy |
| `service/config.py` | Pydantic Settings with `TOKENRANGER_` env prefix |

## Inference Modes & Strategies

| inferenceMode | Strategy Override | Compression |
|---------------|-------------------|-------------|
| `auto` | none (router probes) | Router decides based on GPU availability |
| `cpu` | `light` | Extractive bullets via qwen3:1.7b |
| `gpu` | `full` | Deep semantic summarization via qwen3:8b |
| `remote` | `full` | Same as gpu, uses remote Ollama endpoint |

## Config Write Pattern

The plugin uses a mutable `cfg` ref parsed from `api.pluginConfig`. Slash command changes update `cfg` in-memory (immediate effect on next hook fire), then persist to `openclaw.json` via `api.runtime.config.writeConfigFile()`.

## Hook Behavior Details

- **Turn 1 skip**: First turn is never compressed — preserves initial system/user constraints intact
- **Code block stripping**: Fenced code blocks (`` ``` ``) are removed per-turn before compression; the `has_code` flag is set in turn metadata so the compressor notes "code discussed" in summaries
- **Content extraction**: Handles both plain string and array content block formats (`[{type:"text", text:"..."}]`)
- **prependContext**: Compressed output is returned as `{prependContext}` which the gateway prepends to the prompt sent to the cloud LLM
- **Hook context**: The `before_agent_start` handler accepts `(event, ctx)` where `ctx` provides `sessionId`, `agentId`, `sessionKey`, and `messageProvider`

## Turn Tagging

Messages are serialized with structured tags before compression:

```
[T1:user|520c] Scrape 100 luxury apartments...
[T2:asst|1.2k|code] Verified initial URLs, updated script...
[T3:user|180c] Also add Broadstone properties
```

Tag format: `[T{n}:{role}|{size}{|flags}]` where:
- `n` — sequential turn number (derived from message index)
- `role` — `user` or `asst`
- `size` — original char count (`520c` or `1.2k`)
- `flags` — `|code` if the turn contained fenced code blocks

Turn metadata (`TurnMeta[]`) travels from the plugin through the compress client to the Python service. The compressor uses it to:
- Preserve early user turns (T1, T2) nearly verbatim — these contain task specs and constraints
- Note which turns had code blocks stripped
- Instruct the SLM to output factual state, not first-person commitments ("I'll...")
- Output tagged summaries: `[T1] bullet summary`

## TurnTagging (Research)

Untracked 43k research document analyzing V1 compression artifacts — specifically "planning voice leakage" where the SLM flattens internal agent monologue into visible assistant messages. The turn tagging implementation above is the V2 solution derived from this research.

## Known Gotchas

- OpenClaw messages use **array content blocks** `[{type:"text", text:"..."}]`, not plain strings — content extraction must handle both formats
- Empty/trivial input (<50 chars) is short-circuited to passthrough to prevent Ollama hallucination
- Telegram `callback_data` has a 64-byte limit; `/tokenranger model ` = 19 chars → max 44 chars for model names
- Python pip packages (`langchain`, `langchain-ollama`) are NOT renamed — these are upstream dependencies
- Env var prefix is `TOKENRANGER_` (renamed from `BEFORE_LLM_`)
