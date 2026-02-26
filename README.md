# TokenRanger — Context Compression Plugin

Compresses session context via a local SLM (Ollama) before sending to cloud LLMs, reducing input token costs by **50-80%** with 1-3 second latency overhead.

## How It Works

```
User message → OpenClaw gateway
  → before_agent_start hook fires
  → Plugin sends history to localhost:8100/compress
  → FastAPI service runs LangChain LCEL chain (Ollama mistral:7b)
  → Compressed summary returned as { prependContext }
  → Cloud LLM receives compressed context instead of full history
```

## Requirements

- **Ollama** installed and running (GPU recommended, works on CPU)
- **Python 3.10+** with pip
- ~2GB disk (venv + model)

## Quick Start

```bash
# 1. Enable the plugin
openclaw plugins enable tokenranger

# 2. Install the compression service
openclaw tokenranger setup

# 3. Restart the gateway
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
          "preferredModel": "mistral:7b",
          "compressionStrategy": "auto",
          "inferenceMode": "auto"
        }
      }
    }
  }
}
```

| Option | Default | Description |
|--------|---------|-------------|
| `serviceUrl` | `http://127.0.0.1:8100` | Compression service URL |
| `timeoutMs` | `10000` | Max wait before passthrough (ms) |
| `minPromptLength` | `500` | Minimum history length (chars) to trigger compression |
| `ollamaUrl` | `http://127.0.0.1:11434` | Ollama API endpoint |
| `preferredModel` | `mistral:7b` | Ollama model for compression |
| `compressionStrategy` | `auto` | `auto`/`full`/`light`/`passthrough` |
| `inferenceMode` | `auto` | `auto`/`cpu`/`gpu`/`remote` — controls inference strategy override |

## CLI Commands

```bash
openclaw tokenranger setup       # Install Python service + Ollama model
openclaw tokenranger setup --skip-ollama   # Skip model pull
openclaw tokenranger status      # Check service health
openclaw tokenranger uninstall   # Remove service
```

## Slash Command

Use `/tokenranger` in any chat to access the settings menu:

- **Mode** — Set inference mode (CPU / GPU / Remote / Auto)
- **Model** — Select from pulled Ollama models
- **Enable/Disable** — Toggle the plugin on/off

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
│  │  mistral:7b (GPU)     │                    │
│  │  phi3.5:3b (CPU)      │                    │
│  └──────────────────────┘                    │
└─────────────────────────────────────────────┘
```

## Compression Strategies

| Strategy | When | Model | Description |
|----------|------|-------|-------------|
| `full` | GPU available (>80% VRAM) | mistral:7b | Deep semantic summarization |
| `light` | CPU only | phi3.5:3b | Extractive bullet points |
| `passthrough` | Ollama down | none | Truncate to last 20 lines |

## Measured Results

### Latest: 5-turn Discord bot conversation (2026-02-26, post code-review fixes)

| Turn | Input (tokens) | Compressed (tokens) | Reduction | Latency |
|------|---------------|--------------------:|----------:|--------:|
| 1    | 241           | 121                 | 49.8%     | 916ms   |
| 2    | 732           | 125                 | 82.9%     | 1,086ms |
| 3    | 1,180         | 150                 | 87.3%     | 1,375ms |
| 4    | 1,685         | 212                 | 87.4%     | 1,960ms |
| 5    | 2,028         | 277                 | 86.3%     | 2,420ms |

**Cumulative**: 5,866 input → 885 output tokens (**84.9% overall reduction**)
**Avg latency**: 1.6s per turn (GPU-full, mistral:7b-instruct)
**Projected savings**: $37/month on GPT-4o at 500 msgs/day

### Previous: Token comparison benchmark (2026-02-25)

| Metric | Value |
|--------|-------|
| Ollama token savings | 85.0% |
| Gemini token savings | 85.9% |
| Compression latency | 1.6-2.9s per turn (GPU) |
| Graceful degradation | Full passthrough if service down |

See [CHANGELOG.md](CHANGELOG.md) for full history.

## Graceful Degradation

The plugin never blocks or breaks the gateway:

1. **Service down**: `before_agent_start` catch returns `undefined` → full context sent to LLM
2. **Ollama down**: Python service returns `passthrough` strategy → truncated context
3. **Timeout**: AbortController cancels after `timeoutMs` → full context sent

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
