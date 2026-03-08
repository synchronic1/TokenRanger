# TokenRanger — Test & Verification Report

This document records real-world testing performed against live OpenClaw deployments.
Tests were run on two production nodes with different compute profiles in a home lab
cluster running OpenClaw as a 24/7 personal AI assistant across Discord and Telegram.

## Infrastructure Overview

### Cluster
- **Network**: 192.168.1.0/24 LAN, Proxmox-based virtualization
- **Primary node (pvet630)**: Bare-metal Ubuntu server, triple GPU, runs Ollama natively
  with models loaded in VRAM. Hosts the OpenClaw git repo and serves as the GPU inference
  endpoint for all nodes on the LAN.
- **Secondary node (r430a)**: KVM virtual machine on a separate Proxmox host (pver430).
  CPU-only — offloads all Ollama inference to pvet630 over the LAN (0.6ms RTT).
  Runs OpenClaw gateway with Discord channel.
- **Ollama binding**: pvet630 binds Ollama to `0.0.0.0:11434`, accessible by all LAN nodes.
  r430a's TokenRanger service connects via `TOKENRANGER_OLLAMA_BASE_URL=http://192.168.1.242:11434`.

### Software Stack
- **OpenClaw**: v2026.2.23+, systemd user services on both nodes
- **Ollama**: 0.15.6, hosting qwen3:1.7b/qwen3:8b (current) — migrated from mistral:7b-instruct + phi3.5
- **Python**: 3.10.12, isolated venv at `~/.openclaw/services/tokenranger/`
- **Node.js**: v22+, TypeScript plugin compiled to ESM

### Turbo Boost
Both physical hosts have Intel Turbo Boost enabled for maximum single-thread performance:

| Node | CPU Driver | no_turbo | Status |
|------|-----------|----------|--------|
| pver430 | intel_cpufreq | 0 | Enabled |
| pvet630 | intel_cpufreq | 0 | Enabled |

## Test Nodes

| Node | IP | Hardware | Ollama | Strategy |
|------|------|----------|--------|----------|
| pvet630 | 192.168.1.242 | 3x NVIDIA GPUs (RTX 3090 24GB + 2x RTX 3060 12GB) | local, 11434 | full (qwen3:8b) |
| r430a | 192.168.1.240 | KVM VM, Xeon E5-2680 v4, 8 vCPUs, 16GB RAM, no GPU | remote → pvet630:11434 | full (qwen3:8b) |

---

## 1. Service Health Verification

Both nodes report healthy with correct GPU detection via remote Ollama `/api/ps`:

```bash
# pvet630 (local GPU)
$ curl -s http://127.0.0.1:8100/health | python3 -m json.tool
{
    "status": "ok",
    "compute_class": "gpu_full",
    "endpoint": "http://localhost:11434",
    "model": "mistral:7b-instruct",
    "strategy": "full"
}

# r430a (remote GPU via LAN)
$ curl -s http://127.0.0.1:8100/health | python3 -m json.tool
{
    "status": "ok",
    "compute_class": "gpu_full",
    "endpoint": "http://192.168.1.242:11434",
    "model": "mistral:7b-instruct",
    "strategy": "full"
}
```

---

## 2. Model Comparison Benchmark (2026-03-08)

Comparative benchmark across 7 SLM candidates on pvet630 (GPU). Three structured turn-tagged
payloads: SHORT (749 chars, 3 turns), MEDIUM (1959 chars, 5 turns), LONG (4206 chars, 8 turns).
Qwen3 models tested with `/no_think` system prompt prefix to disable hidden thinking tokens.

### Results

| Model | SHORT (749c) | MEDIUM (1959c) | LONG (4206c) | Tok/s | 1st-person violations |
|-------|:---:|:---:|:---:|:---:|:---:|
| **qwen3:1.7b** | **54.3%** | **62.1%** | **89.8%** | **287-300** | **0** |
| qwen2.5:7b | 78.1% | 85.9% | 82.4% | 147-152 | 0 |
| qwen3:4b | 3.3% | 15.7% | 19.4% | 157-167 | 1 |
| qwen3:8b | -68.4% | -0.4% | 44.4% | 115-120 | 1 |
| mistral:7b-instruct | 6.7% | 24.0% | 37.2% | 143-149 | 0 |
| llama3.1:8b | 28.3% | 41.4% | 38.4% | 63-65 | 0 |
| llama3.2:3b | 51.0% | 28.9% | 47.2% | 124-132 | 0 |

### Key Findings

1. **qwen3:1.7b is the best compression model** — highest reduction on long contexts (89.8%),
   fastest throughput (~300 tok/s on GPU), and zero first-person voice leakage
2. **Larger models are worse at compression** — qwen3:4b and qwen3:8b are too conservative,
   echoing input rather than summarizing. qwen3:8b produces *expansion* on short input (-68.4%)
3. **qwen3:4b ignores `/no_think`** — generated 3280 hidden thinking tokens for 724 chars of
   visible output on SHORT. The directive is not respected by this model size
4. **qwen2.5:7b has strong raw reduction** but at half the throughput of qwen3:1.7b
5. **mistral:7b-instruct underperforms** — only 6.7-37% reduction, previously the default model
6. **`/no_think` is essential for Qwen3** — without it, qwen3:1.7b generates ~2000 hidden thinking
   tokens per response, adding 3-5s latency with no benefit to compression quality

### Recommendation

- **Default model (all strategies)**: `qwen3:1.7b` with `/no_think` system prompt prefix
- **GPU full strategy**: `qwen3:1.7b` (not 8b — smaller model compresses better)
- **CPU light strategy**: `qwen3:1.7b` (1.1GB, fits comfortably in RAM)

Config updated across `service/config.py`, `src/config.ts`, `src/setup.ts`, `openclaw.plugin.json`.
Compressor prompts updated with `_no_think_prefix()` helper in `service/compressor.py`.

---

## 3. Compression Benchmark — 5-Turn Simulated Conversation (Legacy: mistral:7b)

A scripted 5-turn Discord bot setup conversation was used to benchmark both nodes.
Each turn builds on the previous compressed output, simulating real session accumulation.

### pvet630 (local GPU)

| Turn | Input (chars) | Output (chars) | Reduction | Latency |
|------|--------------|----------------|-----------|---------|
| 1 | 88 | 381 | -333.0% | 768ms |
| 2 | 630 | 1,266 | -101.0% | 2,261ms |
| 3 | 2,065 | 726 | 64.8% | 1,406ms |
| 4 | 2,978 | 895 | 69.9% | 1,648ms |
| 5 | 4,065 | 1,188 | 70.8% | 2,047ms |

**Total**: 9,826 → 4,456 chars (**54.7% overall**), avg 1,626ms/turn

### r430a (remote GPU via pvet630 LAN)

| Turn | Input (chars) | Output (chars) | Reduction | Latency |
|------|--------------|----------------|-----------|---------|
| 1 | 88 | 387 | -339.8% | 824ms |
| 2 | 636 | 673 | -5.8% | 1,329ms |
| 3 | 1,478 | 967 | 34.6% | 1,885ms |
| 4 | 2,632 | 1,078 | 59.0% | 2,057ms |
| 5 | 3,902 | 1,171 | 70.0% | 2,191ms |

**Total**: 8,736 → 4,276 chars (**51.1% overall**), avg 1,657ms/turn

**Note**: Short test prompts (turns 1-2) produce expansion because the `full` strategy's
semantic summarization generates structured bullet points that are longer than the sparse
input. This is expected — real conversations with 500+ char history show 60-85% reduction.

---

## 4. Live Production Verification — Real Discord Conversations

### 4a. Post-Code-Review Benchmark (GPU-full, mistral:7b-instruct)

Run against actual multi-turn Discord conversations after deploying code review fixes:

| Turn | Input (tokens) | Compressed (tokens) | Reduction | Latency |
|------|---------------|--------------------:|----------:|--------:|
| 1 | 241 | 121 | 49.8% | 916ms |
| 2 | 732 | 125 | 82.9% | 1,086ms |
| 3 | 1,180 | 150 | 87.3% | 1,375ms |
| 4 | 1,685 | 212 | 87.4% | 1,960ms |
| 5 | 2,028 | 277 | 86.3% | 2,420ms |

**Cumulative**: 5,866 → 885 tokens (**84.9% reduction**), 1.6s avg/turn

### 4b. Live Discord Session

A real Discord conversation compressed 25,146 chars → 578 chars (**97.7% reduction**).

---

## 5. Content Extraction Bug — Discovery and Verification

### Problem
After initial deployment, the `before_agent_start` hook fired on every message but
compression was **never triggered**. Gateway logs showed `historyLen=0` on every
invocation despite active conversations.

### Root Cause
OpenClaw messages use **array content blocks** `[{type:"text", text:"..."}]`, not plain
strings. The plugin only handled string content:

```typescript
// BROKEN: treated array content as empty string
const content = typeof m.content === "string" ? m.content : "";
```

### Fix Applied
```typescript
let content = "";
if (typeof m.content === "string") {
  content = m.content;
} else if (Array.isArray(m.content)) {
  content = (m.content as any[])
    .filter((c: any) => c && typeof c === "object" && c.type === "text")
    .map((c: any) => c.text ?? "")
    .join(" ");
}
```

### Verification Steps
1. Deployed fix to pvet630
2. Sent test messages via Discord
3. Gateway logs confirmed `historyLen > 0` on subsequent hook fires
4. Compression triggered: `[tokenranger] Compressed: 25146 → 578 chars (97.7% reduction)`
5. Cloud LLM received compressed context (verified via debug logging)

---

## 6. Empty-Input Hallucination Guard — Discovery and Verification

### Problem
When `session_history` was empty (first message in a conversation), the Ollama model
**hallucinated** 1,079 chars from nothing, reported as `-107,900% reduction`.

### Root Cause
The `compressor.compress()` method forwarded empty strings to the LangChain LCEL chain,
which prompted Ollama to generate fabricated conversation summaries.

### Fix Applied
Guard in both `main.py` (fast-path) and `compressor.py`:
```python
total_input = len(session_history.strip()) + len(lance_results.strip())
if total_input < 50:
    return passthrough(session_history, lance_results), profile
```

### Verification Steps
1. Sent single-message conversation (no prior history)
2. Service log confirmed: `compress: trivial input (0 chars), returning passthrough`
3. No hallucinated output, no negative reduction percentages
4. Multi-turn conversations still compress correctly (only empty/trivial inputs skip)

---

## 7. Graceful Degradation Verification

### 7a. Python Service Down
```bash
systemctl --user stop openclaw-tokenranger.service
# Send message via Discord → gateway continues normally
# Gateway log: [tokenranger] Compression failed, passing through: TypeError: fetch failed
systemctl --user start openclaw-tokenranger.service
```
**Result**: Full context sent to cloud LLM. No user-visible error.

### 7b. Ollama Unreachable
```bash
systemctl stop ollama
curl -s http://127.0.0.1:8100/health
# {"status": "degraded", "compute_class": "unavailable", ...}
# Compression requests return passthrough (truncated last 20 lines)
systemctl start ollama
```
**Result**: Service returns passthrough strategy. No crash, no error to user.

### 7c. Timeout
Configured `timeoutMs: 2000` (artificially low). Sent large conversation:
- AbortController triggered after 2s
- Gateway log: `[tokenranger] Compression failed, passing through`
- Full context sent to cloud LLM

---

## 8. Remote GPU Offload Verification (r430a → pvet630)

### Configuration
```ini
# r430a systemd unit
Environment="TOKENRANGER_OLLAMA_BASE_URL=http://192.168.1.242:11434"
Environment="TOKENRANGER_OLLAMA_TIMEOUT=10.0"
```

### Verification
| Test | Result |
|------|--------|
| Health check | `gpu_full`, model `mistral:7b-instruct` via remote Ollama |
| Network latency (r430a → pvet630) | 0.6ms |
| Compression latency (remote GPU) | 1.5s avg/turn |
| Compression latency (local CPU) | 103s avg/turn |
| Improvement | **67x faster** |
| RAM freed on r430a (stopped local Ollama) | 3.5GB |
| pvet630 unreachable | Degrades to passthrough |

---

## 9. CPU vs GPU Strategy Comparison

| Metric | GPU (pvet630) | CPU (r430a, before offload) |
|--------|--------------|---------------------------|
| Strategy | full (mistral:7b) | light (phi3.5:3.8B) |
| Avg latency | 1.6s | 103s |
| Reduction (long conversations) | 84.9% | 23.1% |
| Turn 1 behavior | 49.8% reduction | -44.5% (expansion) |
| Practical for real-time | Yes | No |

---

## 10. `/tokenranger` Slash Command Verification

| Command | Tested On | Result |
|---------|-----------|--------|
| `/tokenranger` | Discord | Settings menu with markdown formatting |
| `/tokenranger` | Telegram | Settings menu with inline keyboard buttons |
| `/tokenranger mode` | Discord | Lists modes with descriptions |
| `/tokenranger mode gpu` | Both | Persisted to openclaw.json, took effect immediately |
| `/tokenranger model` | Both | Listed pulled Ollama models from configured endpoint |
| `/tokenranger model mistral:7b-instruct` | Both | Updated preferredModel in config |
| `/tokenranger toggle` | Both | Flipped enabled flag, confirmed in config file |

---

## 11. Discord Interactive Components — Test Protocol & Results (2026-02-26)

### Deployment

| Target | IP | OpenClaw Dist | TokenRanger Dist | Gateway Restart |
|--------|-----|--------------|-----------------|----------------|
| pvet630 | 192.168.1.242 | Deployed (782 files via tar+ssh) | Deployed (scp) | Yes, clean startup |
| r430a | 192.168.1.240 | Pending (SSH unreachable) | Deployed (scp, prior session) | Pending |

**New files deployed:**
- `plugin-command-picker.ts` → bundled into OpenClaw dist chunks (confirmed `plgcmd` in 4 chunk files)
- Modified `native-command.ts`, `provider.ts`, `monitor.ts` → plugin command handlers + fallback components
- Modified `index.ts` → Discord `channelData.discord` specs for interactive components

### Pre-Test Verification (Programmatic — pvet630)

| Check | Status | Details |
|-------|--------|---------|
| Gateway startup | PASS | Zero errors in journalctl, clean restart |
| Plugin load | PASS | `[tokenranger] registered (serviceUrl: http://127.0.0.1:8100)` |
| Service health | PASS | `gpu_full`, `mistral:7b-instruct`, strategy `full` |
| Discord login | PASS | `logged in to discord as 1467804096978354238` (@ClawBaby) |
| Plugin manifest | PASS | configSchema 7 properties, uiHints loaded |
| Compression test | PASS | 486→737 chars, gpu_full, 3.6s latency |
| Bundle integrity | PASS | `plgcmd` custom ID key found in 4 dist chunk files |

### 5-Turn Interactive Test Protocol

Discord slash commands and component interactions require the Discord client.

**Turn 1: Main Menu** — `/tokenranger` (no args)
- Expected: Ephemeral Container with title "TokenRanger Settings", detail lines (service/mode/model/enabled), button row (Mode/Model/Enable)
- Pass: [ ] Ephemeral | [ ] Container layout | [ ] Mode: auto | [ ] Model: mistral:7b | [ ] Enabled: ON (green)

**Turn 2: Mode Picker** — Click "Mode: auto" button
- Expected: Container updates in-place to "Inference Mode" with CPU/GPU/Remote/Auto buttons, current highlighted primary
- Pass: [ ] In-place update | [ ] Auto=primary | [ ] Others=secondary | [ ] Back button

**Turn 3: Set Mode** — Click "GPU" button
- Expected: Returns to main menu, Mode button now "Mode: gpu", config persisted
- Pass: [ ] Main menu | [ ] Mode: gpu | [ ] Config updated in openclaw.json

**Turn 4: Model Picker** — Click "Model: mistral:7b" button
- Expected: Select dropdown with Ollama models, current marked default, Back button
- Pass: [ ] Select dropdown | [ ] Models listed | [ ] Current=default | [ ] Back button

**Turn 5: Toggle** — Click "Enabled: ON" button
- Expected: Returns to main menu, button shows "Enabled: OFF" (red/danger), config persisted
- Pass: [ ] Main menu | [ ] Enabled: OFF (red) | [ ] Config persisted | [ ] Re-toggle works

### Post-Test Verification Commands

```bash
# Check config was updated
ssh -i ~/.ssh/id_ed25519_cluster rm@192.168.1.242 \
  "cat ~/.openclaw/openclaw.json | python3 -c 'import sys,json; c=json.load(sys.stdin); print(json.dumps(c[\"plugins\"][\"entries\"][\"tokenranger\"],indent=2))'"

# Check gateway logs for interaction handling
ssh -i ~/.ssh/id_ed25519_cluster rm@192.168.1.242 \
  "journalctl --user -u openclaw-gateway.service --since '10 minutes ago' --no-pager | grep -i 'tokenranger\|plgcmd'"

# Verify Telegram still works (inline keyboard, not components)
# Send /tokenranger via Telegram, confirm button layout unchanged
```

### r430a Deployment (Pending — SSH Unreachable)

r430a (192.168.1.240) SSH timed out during deployment. Once connectivity is restored:

```bash
cd /path/to/openclaw && tar czf - dist/ | ssh vm404 "cd /home/rm/.npm-global/lib/node_modules/openclaw/ && rm -rf dist && tar xzf -"
ssh vm404 "openclaw gateway restart"
ssh vm404 "openclaw tokenranger status"
# Repeat 5-turn test protocol
```

r430a differences: CPU-only, remote GPU via `http://192.168.1.242:11434`, expected light strategy unless remote GPU is reachable.

---

## 12. Local Model Enablement — TokenRanger as Context Window Extender (2026-03-08)

### Problem

OpenClaw defaults to cloud models with large context windows (200k+ tokens). Local models
(qwen2.5:7b at 131k, qwen3:8b at 32k) can technically pass OpenClaw's hard minimum context
check (16k tokens, defined in `context-window-guard.ts`), but conversations grow beyond
their effective capacity mid-session, causing degraded output or failures.

### Context Window Architecture

OpenClaw enforces context limits at two levels:

1. **Model selection gate** (pre-compression): `CONTEXT_WINDOW_HARD_MIN_TOKENS = 16,000`.
   Models below this are blocked with `FailoverError`. Warning at 32k.
   Location: `src/agents/pi-embedded-runner/run.ts` lines 371-396.
2. **Runtime compaction** ("safeguard" mode): budgets ~50% of context for history,
   summarizes overflow. This is separate from TokenRanger.

TokenRanger's `before_agent_start` hook fires **after** the model selection gate but
**before** prompt assembly, compressing the conversation history so it fits within the
local model's context window.

### Test Configuration (macOS, Apple Silicon)

| Component | Config |
|-----------|--------|
| Chat model | `ollama/qwen2.5:7b` (131k context) |
| Compression model | `qwen3:1.7b` via TokenRanger (set in `preferredModel` config) |
| Platform | macOS, Apple Silicon (Metal GPU) |
| Service | launchd `com.openclaw.tokenranger`, port 8100 |
| Environment | `TOKENRANGER_GPU_COMPRESSION_MODEL=qwen3:1.7b` |

### Results

Multi-turn conversation with growing context (task management API design):

| Turn | Input (chars) | Output (chars) | Reduction | Latency |
|------|--------------|----------------|-----------|---------|
| 2 | 3,037 | 1,464 | 52% | 10.8s (cold start) |
| 3 | 3,084 | 798 | **74%** | **2.9s** (warm) |
| 4 | 4,144 | 968 | **77%** | 7.7s |
| 5 | 5,388 | 927 | **83%** | 14.2s |
| 6 | 5,662 | 1,214 | **79%** | 9.1s |

The local qwen2.5:7b model successfully handled all compressed prompts without context
window errors. Compression maintained 74-83% reduction on warm cache.

### Key Findings

1. **TokenRanger enables practical local model usage** — by compressing history each turn,
   the effective context capacity of a 32k model becomes equivalent to ~160k uncompressed
2. **Model contention on shared GPU** — on Apple Silicon, the compression model (qwen3:1.7b)
   and chat model (qwen2.5:7b) compete for unified memory, causing variable latency.
   Cold-start turns take 10-14s; warm-cache turns take 3-8s
3. **`preferredModel` config overrides inference router** — the plugin config's
   `preferredModel` field takes priority over the service's `TOKENRANGER_GPU_COMPRESSION_MODEL`
   env var. Must set both consistently
4. **Python 3.9 compatibility** — macOS system Python is 3.9, requiring
   `from __future__ import annotations` for `str | None` type syntax

### Setup for Local Model Usage

```json
{
  "agents": {
    "defaults": {
      "model": { "primary": "ollama/qwen2.5:7b" }
    }
  },
  "plugins": {
    "entries": {
      "tokenranger": {
        "enabled": true,
        "config": {
          "preferredModel": "qwen3:1.7b",
          "inferenceMode": "auto"
        }
      }
    }
  }
}
```

LaunchAgent env vars (macOS):
```xml
<key>TOKENRANGER_GPU_COMPRESSION_MODEL</key>
<string>qwen3:1.7b</string>
<key>TOKENRANGER_GPU_FAST_MODEL</key>
<string>qwen3:1.7b</string>
```

---

## 13. Setup CLI Verification

```bash
$ openclaw tokenranger setup

  TokenRanger — Setup

  Checking prerequisites...
  python3 3.10.12 ✓
  pip ✓
  ollama ✓

  Platform: linux (systemd)
  Install directory: /home/rm/.openclaw/services/tokenranger

  Step 1/5: Checking Ollama...
  ollama already installed ✓

  Step 2/5: Installing Python service...
  copied main.py
  copied config.py
  copied inference_router.py
  copied compressor.py
  copied requirements.txt
  creating Python venv...
  installing Python dependencies...
  Python service installed ✓

  Step 3/5: Pulling Ollama model...
  model mistral:7b-instruct already present ✓

  Step 4/5: Installing system service...
  wrote /home/rm/.config/systemd/user/openclaw-tokenranger.service
  systemd service enabled and started ✓

  Step 5/5: Verifying...
  service healthy: strategy=full, model=mistral:7b-instruct ✓

  Setup complete! Restart the gateway: openclaw gateway restart
```
