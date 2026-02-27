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

# No unit test suite — testing is done live against OpenClaw deployments
# See TESTING.md for verification procedures
```

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
| `cpu` | `light` | Extractive bullets via phi3.5:3b |
| `gpu` | `full` | Deep semantic summarization via mistral:7b |
| `remote` | `full` | Same as gpu, uses remote Ollama endpoint |

## Config Write Pattern

The plugin uses a mutable `cfg` ref parsed from `api.pluginConfig`. Slash command changes update `cfg` in-memory (immediate effect on next hook fire), then persist to `openclaw.json` via `api.runtime.config.writeConfigFile()`.

## Known Gotchas

- OpenClaw messages use **array content blocks** `[{type:"text", text:"..."}]`, not plain strings — content extraction must handle both formats
- Empty/trivial input (<50 chars) is short-circuited to passthrough to prevent Ollama hallucination
- Telegram `callback_data` has a 64-byte limit; `/tokenranger model ` = 19 chars → max 44 chars for model names
- Python pip packages (`langchain`, `langchain-ollama`) are NOT renamed — these are upstream dependencies
- Env var prefix is `TOKENRANGER_` (renamed from `BEFORE_LLM_`)
