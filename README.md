# TokenRanger — Context Compression Plugin

Compresses session context via a local SLM (Ollama) before sending to cloud LLMs, reducing input token costs by **50-80%** with 1-3 second latency overhead.

## How It Works

```
User message → OpenClaw gateway
  → before_agent_start hook fires
  → Plugin sends history to localhost:8100/compress
  → FastAPI service runs LangChain LCEL chain (Ollama qwen3:8b)
  → Compressed summary returned as { prependContext }
  → Cloud LLM receives compressed context instead of full history
```

## Requirements

- **Node.js** 18+ and npm
- **Ollama** installed and running (GPU recommended, works on CPU)
- **Python 3.10+** with pip
- ~2GB disk (venv + model)

## Quick Start

### One-line install (all dependencies)

```bash
./scripts/install.sh
```

This installs Node dependencies, Python venv + packages, Ollama, and pulls the right model based on GPU detection. Flags:

| Flag | Effect |
|------|--------|
| `--cpu-only` | Force CPU mode — pulls qwen3:1.7b only, skips GPU detection |
| `--skip-ollama` | Skip Ollama install and model pull |
| `--skip-build` | Skip npm install and TypeScript build |

### Manual install

```bash
# 1. Install Node dependencies + build
npm install && npm run build

# 2. Enable the plugin
openclaw plugins enable tokenranger

# 3. Install the compression service (Python venv, Ollama model, system service)
openclaw tokenranger setup

# 4. Restart the gateway
openclaw gateway restart
```

## Configuration

Add to `~/.openclaw/openclaw.json`:

```json
{
  "plugins": {
    "entries": {
      "tokenranger": {
        "enabled": true,
        "config": {
          "serviceUrl": "http://127.0.0.1:8100",
          "timeoutMs": 10000,
          "minPromptLength": 500,
          "ollamaUrl": "http://127.0.0.1:11434",
          "preferredModel": "qwen3:8b",
          "compressionStrategy": "auto",
          "inferenceMode": "auto",
          "metricsEnabled": false,
          "metricsUrl": "http://192.168.1.203:8101"
        }
      }
    }
  }
}
```

| Option | Default | Description |
|--------|---------|-------------|
| `serviceUrl` | `http://127.0.0.1:8100` | Compression service URL |
| `timeoutMs` | `10000` | Max wait before passthrough (ms). Auto-adjusted: 30s for local chat models, 60s for CPU inference mode |
| `minPromptLength` | `500` | Minimum history length (chars) to trigger compression |
| `ollamaUrl` | `http://127.0.0.1:11434` | Ollama API endpoint |
| `preferredModel` | `qwen3:8b` | Ollama model for compression |
| `compressionStrategy` | `auto` | `auto`/`full`/`light`/`passthrough` |
| `inferenceMode` | `auto` | `auto`/`cpu`/`gpu`/`remote` — controls inference strategy override |
| `metricsEnabled` | `false` | Enable fire-and-forget metrics emission to centralized collector |
| `metricsUrl` | `http://192.168.1.203:8101` | Metrics collector endpoint |

## CLI Commands

```bash
openclaw tokenranger setup       # Install Python service + Ollama model
openclaw tokenranger setup --skip-ollama   # Skip model pull
openclaw tokenranger status      # Check service health
openclaw tokenranger uninstall   # Remove service
```

## Slash Commands

Use `/tokenranger` in any chat to access the settings menu:

| Command | Description |
|---------|-------------|
| `/tokenranger` | Show current settings and service status |
| `/tokenranger mode` | Set inference mode (CPU / GPU / Remote / Auto) |
| `/tokenranger model` | Select from pulled Ollama models |
| `/tokenranger toggle` | Enable/disable the plugin (persisted to config) |
| `/tokenranger no` | Disable compression for this session only |
| `/tokenranger yes` | Re-enable compression for this session |

`/tokenranger no` is useful when you want full uncompressed context for a specific conversation without changing global settings. The disable is in-memory only — a gateway restart resets it. Aliases: `off`/`on`.

## CPU Inference (16GB Shared RAM)

TokenRanger supports CPU-only inference for users without a discrete GPU. The `light` compression strategy uses `qwen3:1.7b` (~1.1GB) for extractive bullet-point summarization — fast enough on modern x86 and Apple Silicon CPUs.

```bash
# Install with CPU-only mode
./scripts/install.sh --cpu-only

# Or set via slash command
/tokenranger mode cpu
```

**CPU defaults:**
- Model: `qwen3:1.7b` (Q4_K_M, ~1.1GB RAM)
- Strategy: `light` — extractive bullets, max 10 per turn
- Max context: 8192 tokens
- Timeout: 60s base, scales with input size (+5ms/char, capped at 120s)

On Apple Silicon with 16GB unified memory, Ollama uses Metal GPU by default — TokenRanger auto-detects this as GPU mode. For x86 Linux/Windows without a GPU, `cpu` mode is automatically selected when no VRAM is detected.

## Architecture

```
┌─────────────────────────────────────────────┐
│  OpenClaw Gateway                           │
│  ┌──────────────────────────────────────┐   │
│  │  tokenranger plugin (TS)             │   │
│  │  hooks: before_agent_start           │   │
│  │  → POST localhost:8100/compress      │───┼──┐
│  └──────────────────────────────────────┘   │  │
└─────────────────────────────────────────────┘  │
                                                  │
┌─────────────────────────────────────────────┐  │
│  Compression Service (Python FastAPI)        │◄─┘
│  localhost:8100                               │
│  ┌────────────┐ ┌──────────────┐             │
│  │ Inference   │ │ Compressor   │             │
│  │ Router      │ │ (LangChain)  │             │
│  │ GPU detect  │ │ LCEL chains  │             │
│  └──────┬─────┘ └──────┬───────┘             │
│         │              │                      │
│         └──────┬───────┘                      │
│                ▼                              │
│  ┌──────────────────────┐                    │
│  │  Ollama (localhost)   │                    │
│  │  qwen3:8b (GPU)       │                    │
│  │  qwen3:1.7b (CPU)     │                    │
│  └──────────────────────┘                    │
└─────────────────────────────────────────────┘
```

## Turn Tagging

Messages are serialized with structured tags before compression:

```
[T1:user|520c] Scrape 100 luxury apartments...
[T2:asst|1.2k|code] Verified initial URLs, updated script...
[T3:user|180c] Also add Broadstone properties
```

Tag format: `[T{n}:{role}|{size}{|flags}]` where `n` is the turn number, `role` is `user` or `asst`, `size` is the original character count, and `|code` flags turns that contained code blocks (stripped before compression).

## Compression Strategies

| Strategy | When | Model | Description |
|----------|------|-------|-------------|
| `full` | GPU available (>80% VRAM) | qwen3:8b | Deep semantic summarization |
| `light` | CPU only | qwen3:1.7b | Extractive bullet points, max 10 |
| `passthrough` | Ollama down | none | Truncate to last 20 lines |

## Measured Results

### Model Comparison Benchmark (2026-03-08, pvet630 3x NVIDIA GPU)

Tested with structured turn-tagged payloads (SHORT 749c/3 turns, MEDIUM 1959c/5 turns, LONG 4206c/8 turns).
Qwen3 models use `/no_think` prefix to disable hidden thinking tokens.

| Model | SHORT | MEDIUM | LONG | Tok/s | 1st-person |
|-------|-------|--------|------|-------|------------|
| **qwen3:1.7b** | **54.3%** | **62.1%** | **89.8%** | **287-300** | **0** |
| qwen2.5:7b | 78.1% | 85.9% | 82.4% | 147-152 | 0 |
| qwen3:4b | 3.3% | 15.7% | 19.4% | 157-167 | 1 |
| qwen3:8b | -68.4% | -0.4% | 44.4% | 115-120 | 1 |
| mistral:7b | 6.7% | 24.0% | 37.2% | 143-149 | 0 |
| llama3.1:8b | 28.3% | 41.4% | 38.4% | 63-65 | 0 |
| llama3.2:3b | 51.0% | 28.9% | 47.2% | 124-132 | 0 |

**Winner**: qwen3:1.7b — highest reduction on long contexts (89.8%), fastest throughput (300 tok/s), zero first-person voice leakage. Larger models are too conservative, echoing input rather than summarizing.

### Mac Local Inference Benchmark (2026-03-08, Apple Silicon)

Ollama and MLX runtimes tested with the same payloads. Neither produces viable compression.

| Runtime / Model | SHORT | MEDIUM | LONG | Avg Latency | Notes |
|-----------------|-------|--------|------|-------------|-------|
| Ollama qwen3:1.7b | −172% | −9% | +6% | 7.5s | Expands short/medium inputs |
| Ollama qwen3:8b | −90% | −8% | +4% | 31.3s | Too slow; expands inputs |
| MLX Qwen3-14B-4bit | +21% | +38% | −18% | 23.4s | Marginal; slower than Ollama 1.7b |
| MLX DeepSeek-R1-8B-4bit | −2038% | −950% | −500% | 73.4s | Reasoning model; unusable |

Negative reduction = output is larger than input. The same qwen3:1.7b model achieves 54–90% reduction in 1–3s on pvet630 (NVIDIA GPU). Gap is hardware-driven: Apple Silicon unified memory vs dedicated VRAM changes quantization path and throughput. Cloud API compression is the recommended alternative for Mac. See [TESTING.md](TESTING.md) Section 15 for full analysis.

### Previous: 5-turn Discord bot conversation (2026-02-26, mistral:7b-instruct)

| Metric | Value |
|--------|-------|
| Overall reduction | 84.9% (5,866 → 885 tokens) |
| Avg latency | 1.6s per turn (GPU-full) |
| Projected savings | $37/month on GPT-4o at 500 msgs/day |

See [CHANGELOG.md](CHANGELOG.md) for full history.

## Using Local Models with TokenRanger

TokenRanger enables practical use of local SLMs as the primary OpenClaw chat model by keeping
conversation context within the model's context window. Without compression, conversations
quickly exceed a 32k-131k token limit; with TokenRanger achieving 74-83% reduction per turn,
a 32k model's effective capacity becomes equivalent to ~160k uncompressed.

**Setup**: Set the chat model to a local Ollama model and configure TokenRanger to use
`qwen3:1.7b` for compression (a different, smaller model that won't contend for resources):

```json
{
  "agents": { "defaults": { "model": { "primary": "ollama/qwen2.5:7b" } } },
  "plugins": {
    "entries": {
      "tokenranger": {
        "enabled": true,
        "config": { "preferredModel": "qwen3:1.7b" }
      }
    }
  }
}
```

**Automatic timeout adjustment**: When TokenRanger detects a local chat model, it
automatically increases the compression timeout (10s → 30s), scales it with input size,
and ensures the agent timeout is at least 300s. CPU inference mode (`/tokenranger mode cpu`)
uses a 60s base timeout to accommodate cold-start model loading on x86 CPUs.

**Requirements**: OpenClaw's hard minimum context is 16k tokens — any model above this
threshold works. Tested with qwen2.5:7b (131k) and qwen3:8b (32k) on Apple Silicon.
See [TESTING.md](TESTING.md) Section 12 for full benchmark results.

## Graceful Degradation

The plugin never blocks or breaks the gateway:

1. **Service down**: `before_agent_start` catch returns `undefined` → full context sent to LLM
2. **Ollama down**: Python service returns `passthrough` strategy → truncated context
3. **Timeout**: AbortController cancels after `timeoutMs` → full context sent
4. **Session disabled**: `/tokenranger no` → hook returns immediately, zero overhead

## Troubleshooting

```bash
# Check if service is running
curl http://127.0.0.1:8100/health

# Check logs
tail -f ~/.openclaw/logs/tokenranger.log

# Restart service (Linux)
systemctl --user restart openclaw-tokenranger

# Restart service (macOS)
launchctl unload ~/Library/LaunchAgents/com.openclaw.tokenranger.plist
launchctl load ~/Library/LaunchAgents/com.openclaw.tokenranger.plist

# Invalidate GPU detection cache
curl -X POST http://127.0.0.1:8100/invalidate-cache
```

## License

MIT
