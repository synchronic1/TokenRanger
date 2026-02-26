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
- **Ollama**: 0.15.6, hosting mistral:7b-instruct (GPU) and phi3.5:latest (CPU fallback)
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
| pvet630 | 192.168.1.242 | 3x NVIDIA GPUs (RTX 3090 24GB + 2x RTX 3060 12GB) | local, 11434 | full (mistral:7b-instruct) |
| r430a | 192.168.1.240 | KVM VM, Xeon E5-2680 v4, 8 vCPUs, 16GB RAM, no GPU | remote → pvet630:11434 | full (mistral:7b-instruct) |

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

## 2. Compression Benchmark — 5-Turn Simulated Conversation

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

## 3. Live Production Verification — Real Discord Conversations

### 3a. Post-Code-Review Benchmark (GPU-full, mistral:7b-instruct)

Run against actual multi-turn Discord conversations after deploying code review fixes:

| Turn | Input (tokens) | Compressed (tokens) | Reduction | Latency |
|------|---------------|--------------------:|----------:|--------:|
| 1 | 241 | 121 | 49.8% | 916ms |
| 2 | 732 | 125 | 82.9% | 1,086ms |
| 3 | 1,180 | 150 | 87.3% | 1,375ms |
| 4 | 1,685 | 212 | 87.4% | 1,960ms |
| 5 | 2,028 | 277 | 86.3% | 2,420ms |

**Cumulative**: 5,866 → 885 tokens (**84.9% reduction**), 1.6s avg/turn

### 3b. Live Discord Session

A real Discord conversation compressed 25,146 chars → 578 chars (**97.7% reduction**).

---

## 4. Content Extraction Bug — Discovery and Verification

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

## 5. Empty-Input Hallucination Guard — Discovery and Verification

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

## 6. Graceful Degradation Verification

### 6a. Python Service Down
```bash
systemctl --user stop openclaw-tokenranger.service
# Send message via Discord → gateway continues normally
# Gateway log: [tokenranger] Compression failed, passing through: TypeError: fetch failed
systemctl --user start openclaw-tokenranger.service
```
**Result**: Full context sent to cloud LLM. No user-visible error.

### 6b. Ollama Unreachable
```bash
systemctl stop ollama
curl -s http://127.0.0.1:8100/health
# {"status": "degraded", "compute_class": "unavailable", ...}
# Compression requests return passthrough (truncated last 20 lines)
systemctl start ollama
```
**Result**: Service returns passthrough strategy. No crash, no error to user.

### 6c. Timeout
Configured `timeoutMs: 2000` (artificially low). Sent large conversation:
- AbortController triggered after 2s
- Gateway log: `[tokenranger] Compression failed, passing through`
- Full context sent to cloud LLM

---

## 7. Remote GPU Offload Verification (r430a → pvet630)

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

## 8. CPU vs GPU Strategy Comparison

| Metric | GPU (pvet630) | CPU (r430a, before offload) |
|--------|--------------|---------------------------|
| Strategy | full (mistral:7b) | light (phi3.5:3.8B) |
| Avg latency | 1.6s | 103s |
| Reduction (long conversations) | 84.9% | 23.1% |
| Turn 1 behavior | 49.8% reduction | -44.5% (expansion) |
| Practical for real-time | Yes | No |

---

## 9. `/tokenranger` Slash Command Verification

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

## 10. Setup CLI Verification

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
