# Changelog

All notable changes to the TokenRanger extension are documented here.
Entries are date-stamped to maintain a running history of updates, fixes, and benchmarks.

---

## [2026-02-26] Rename to TokenRanger + `/tokenranger` Slash Command

### Rename
Full rename from `langchain-before-llm` to `tokenranger` across all files, services,
configs, and documentation. The name "LangChain" is a trademark and should not be used
in the plugin/service identity (Python library dependencies `langchain`, `langchain-ollama`
remain unchanged — those are actual PyPI package names).

**What changed:**
- Plugin ID: `langchain-before-llm` → `tokenranger`
- Package name: `@openclaw/langchain-before-llm` → `@openclaw/tokenranger`
- Config type: `LangChainBeforeLlmConfig` → `TokenRangerConfig`
- Env prefix: `BEFORE_LLM_` → `TOKENRANGER_`
- Systemd unit: `openclaw-before-llm.service` → `openclaw-tokenranger.service`
- Launchd plist: `com.openclaw.langchain-before-llm` → `com.openclaw.tokenranger`
- Log file: `langchain-before-llm.log` → `tokenranger.log`
- Python loggers: `before-llm.*` → `tokenranger.*`
- FastAPI title: `LangChain Before-LLM Service` → `OpenClaw TokenRanger Service`
- Service/extension dirs: `~/.openclaw/{extensions,services}/langchain-before-llm/` → `.../tokenranger/`
- CLI command: `openclaw langchain-before-llm` → `openclaw tokenranger`

### `/tokenranger` Slash Command
New interactive slash command replacing `/compress-status`:
- `/tokenranger` — Main menu with Mode, Model, Enable buttons
- `/tokenranger mode` — Inference mode selection (CPU / GPU / Remote / Auto)
- `/tokenranger mode <value>` — Set inference mode
- `/tokenranger model` — Lists pulled Ollama models as selectable buttons
- `/tokenranger model <name>` — Set preferred model
- `/tokenranger toggle` — Enable/disable the plugin

### Inference Mode + Model Override
New `inferenceMode` config field (`auto` | `cpu` | `gpu` | `remote`):
- `auto` → no override; inference router probes and decides
- `cpu` → forces `light` strategy
- `gpu` → forces `full` strategy
- `remote` → forces `full` strategy (remote GPU via LAN)

Model and strategy overrides are passed through the entire stack:
- TypeScript: `CompressRequest.modelOverride` / `strategyOverride`
- Python: `CompressRequest.model_override` / `strategy_override`
- Compressor: `dataclasses.replace(profile, ...)` applies overrides before compression

---

## [2026-02-26] r430a Remote GPU Offload — pvet630 Ollama

### Change
Reconfigured r430a's TokenRanger service to offload all Ollama inference
to pvet630's GPUs over the LAN, instead of running CPU inference locally.

### Architecture
```
r430a (192.168.1.240)                    pvet630 (192.168.1.242)
┌──────────────────────┐                 ┌──────────────────────────┐
│ OpenClaw Gateway     │                 │ Ollama (0.0.0.0:11434)   │
│   ↓                  │                 │   RTX 3090 (24GB)        │
│ TypeScript Plugin    │                 │   RTX 3060 (12GB) x2     │
│   ↓ localhost:8100   │    LAN 0.6ms    │                          │
│ Python FastAPI ──────│─────────────────│→ mistral:7b-instruct     │
│   (50MB RAM)         │                 │   (full strategy, GPU)   │
└──────────────────────┘                 └──────────────────────────┘
```

### Configuration Changes
- `TOKENRANGER_OLLAMA_BASE_URL`: `http://localhost:11434` → `http://192.168.1.242:11434`
- `TOKENRANGER_OLLAMA_TIMEOUT`: `3.0` → `10.0` (network headroom)
- `TOKENRANGER_GPU_COMPRESSION_MODEL`: `mistral:7b-instruct`
- `timeoutMs`: `120000` → `15000` (GPU is fast, no need for CPU-level timeout)
- `minPromptLength`: `300` → `500` (full strategy handles longer inputs well)
- Local Ollama stopped and disabled (freed 3.5GB RAM)

### Benchmark — Remote GPU vs Local CPU

| Metric | CPU (local) | GPU (remote) | Improvement |
|--------|------------|-------------|-------------|
| Avg latency/turn | 103s | 1.5s | **67x faster** |
| Strategy | light (phi3.5:3.8B) | full (mistral:7b) | Better model |
| Reduction (short) | 23.1% | 28.3% | +5pp |
| RAM on r430a | 8.4GB used | 4.9GB used | -3.5GB freed |
| Network overhead | 0ms | 0.6ms | Negligible |

### 5-Turn Benchmark Results (Remote GPU)

| Turn | Input (chars) | Output (chars) | Reduction | Latency |
|------|--------------|----------------|-----------|---------|
| 1    | 237          | 341            | -43.9%    | 699ms   |
| 2    | 691          | 710            | -2.7%     | 1,268ms |
| 3    | 1,214        | 654            | 46.1%     | 1,267ms |
| 4    | 1,389        | 826            | 40.5%     | 2,659ms |
| 5    | 1,264        | 905            | 28.4%     | 1,838ms |

**Total**: 4,795 → 3,436 chars (28.3% reduction), 7.7s total, 1.5s avg/turn

Note: Short test conversations show lower reduction. Real Discord conversations
(2000+ tokens) achieve 85%+ reduction with the full strategy.

### Why This Works
1. pvet630 Ollama already binds to `0.0.0.0:11434` (was pre-configured)
2. LAN latency is 0.6ms — negligible vs inference time
3. Inference router's GPU detection works via remote `/api/ps` VRAM check
4. Python service stays local (caching, graceful degradation, no network exposure)
5. If pvet630 is unreachable, service degrades to passthrough (no error to user)

---

## [2026-02-26] Deployment to r430a (192.168.1.240) — CPU-Only

### Target Environment
- **Host**: r430a (192.168.1.240), Ubuntu 22.04, x86_64
- **Hardware**: CPU-only (no NVIDIA GPU / nvidia-smi unavailable)
- **OpenClaw**: 2026.2.23 with Discord channel, Kimi coding model
- **Ollama**: 0.15.6, pulled `phi3.5:latest` (3.8B, Q4_0, 2.2GB)
- **Python**: 3.10.12

### Deployment Steps
1. Packaged extension on pvet630 as tarball (excluding node_modules)
2. Transferred via relay to r430a
3. Extracted to `~/.openclaw/extensions/tokenranger/`
4. Created Python venv at `~/.openclaw/services/tokenranger/`
5. Installed dependencies, pulled `phi3.5:latest` model
6. Created systemd user service (`openclaw-tokenranger.service`)
7. Updated `openclaw.json`: plugin enabled, `timeoutMs: 120000`, `minPromptLength: 300`
8. Added `tokenranger` to `plugins.allow` (pin trust)
9. Restarted gateway — plugin loaded, health check passed

### CPU Model Config Adjustments
- `cpu_compression_model`: `phi3.5:latest` (was `phi3.5:3b` which doesn't exist)
- `timeoutMs`: `120000` (up from 10000 — CPU inference is 90-170s/turn)
- `minPromptLength`: `300` (down from 500 — CPU compression is less aggressive)

### E2E Benchmark — CPU-Only (phi3.5:latest, light strategy)

| Turn | Input (tokens) | Output (tokens) | Reduction | Latency |
|------|---------------|-----------------|-----------|---------|
| 1    | 224           | 324             | -44.5%    | 170s    |
| 2    | 459           | 201             | 56.1%     | 139s    |
| 3    | (timeout)     | —               | —         | >180s   |

**Cumulative**: 683 → 525 tokens (23.1% reduction), 310s total, 103s avg/turn

### Key Observations — CPU vs GPU
| Metric | pvet630 (GPU) | r430a (CPU) |
|--------|--------------|-------------|
| Strategy | full (mistral:7b) | light (phi3.5:3.8B) |
| Avg latency | 1.6s | 103s |
| Reduction | 84.9% | 23.1% |
| Turn 1 behavior | 49.8% reduction | -44.5% (expansion) |

### Known Issues on CPU
1. **Latency**: 90-170s per turn makes real-time compression impractical
2. **Expansion on short input**: Turn 1 produced MORE output than input (light strategy
   with phi3.5 generates verbose extractive bullets instead of condensing)
3. **Timeout risk**: 120s timeout still insufficient for some turns

### Possible Improvements
- Use a smaller model (tinyllama, gemma:2b) for faster CPU inference
- Add output length cap to light compression prompt (e.g., "Max 5 bullets")
- Consider offloading compression to pvet630's GPUs via network call
- Set `compressionStrategy: "passthrough"` on CPU-only machines (skip LLM entirely,
  just truncate to last 20 lines — instant, no latency)

---

## [2026-02-26] Code Review Fixes — Cross-Platform & Async Safety

### Problems Identified
A full code review and cross-platform compatibility audit identified 6 critical issues
across the TypeScript plugin and Python service. Windows support was entirely broken,
and several async safety and security issues were present.

### Fixes Applied

1. **Command injection in `checkCommand()`** — `setup.ts`
   - **Before**: `execSync(`which ${cmd}`)` passed input directly into a shell string.
   - **After**: `spawnSync(whichCmd, [cmd])` with `process.platform === "win32"` detection
     to use `where` on Windows and `which` on Unix. No shell invocation.

2. **Blocking subprocess on async event loop** — `inference_router.py`
   - **Before**: `subprocess.run(["nvidia-smi", ...])` blocked the entire FastAPI server
     for up to 5 seconds during GPU detection.
   - **After**: `asyncio.create_subprocess_exec()` with `await asyncio.wait_for(..., timeout=5.0)`.
     Non-blocking; other requests proceed while nvidia-smi runs.

3. **Race condition on probe cache** — `inference_router.py`
   - **Before**: Multiple concurrent requests could all miss the stale cache simultaneously,
     causing redundant Ollama API calls (thundering herd on cache expiry).
   - **After**: `asyncio.Lock` with double-check-after-acquire pattern. Probe logic
     extracted into `_do_probe()`. Only one coroutine refreshes; others wait and reuse.

4. **Passthrough response dropped `lance_results`** — `main.py`
   - **Before**: Trivial-input fast path returned `compressed_context=req.session_history`,
     silently dropping any `lance_results` content.
   - **After**: Calls `compressor._passthrough(session_history, lance_results)` to format
     both fields consistently with the non-trivial path.

5. **Silent exception swallowing** — `inference_router.py`
   - **Before**: `except Exception: pass` in `_infer_compute_from_generate()` discarded
     all errors from the generate+ps round-trip with zero logging.
   - **After**: `except Exception as e: logger.warning("probe: generate-based compute inference failed: %s", e)`.

6. **XML/shell injection in plist generation** — `setup.ts`
   - **Before**: `config.ollamaUrl` embedded raw in plist XML (vulnerable to `<`, `>`, `&`)
     and `launchctl` commands used shell interpolation with `execSync`.
   - **After**: Added `escapeXml()` helper for plist values. Added `venvBin()` helper for
     cross-platform venv paths (`bin/` on Unix, `Scripts/` on Windows). Replaced all
     `execSync("launchctl ...")` with `spawnSync("launchctl", [...])`.

### Additional Cross-Platform Improvements
- `venvBin()` helper resolves `venv/bin/pip` vs `venv\Scripts\pip.exe` per platform
- `checkCommand()` uses `where` on Windows, `which` on Unix
- Windows now detected (returns `serviceManager: "none"` with manual-start instructions)

### Known Windows Gaps (Not Yet Addressed)
- `python3` command doesn't exist on Windows (need to try `python` fallback)
- No Windows service manager support (nssm, Task Scheduler)
- `package.json` clean script uses `rm -rf` (not cross-platform)

### E2E Simulation Results (Post-Fix)

5-turn Discord bot setup conversation, GPU-full strategy, mistral:7b-instruct:

| Turn | Input (tokens) | Output (tokens) | Saved | Reduction | Latency |
|------|---------------|-----------------|-------|-----------|---------|
| 1    | 241           | 121             | 120   | 49.8%     | 916ms   |
| 2    | 732           | 125             | 607   | 82.9%     | 1,086ms |
| 3    | 1,180         | 150             | 1,030 | 87.3%     | 1,375ms |
| 4    | 1,685         | 212             | 1,473 | 87.4%     | 1,960ms |
| 5    | 2,028         | 277             | 1,751 | 86.3%     | 2,420ms |

**Cumulative**: 5,866 → 885 tokens (**84.9% reduction**), 7.8s total Ollama time (1.6s avg/turn)

**Projected savings**: $37.36/month on GPT-4o at 500 msgs/day

---

## [2026-02-26] Content Extraction Bug Fix & Inferencing Optimization

### Problem: Compression Never Fired on Real Discord Messages
After deploying the plugin and observing 24+ hook fires, compression was never triggered.
Gateway logs showed `historyLen=0` on every `before_agent_start` invocation.

### Root Cause
OpenClaw messages use array content blocks `[{type:"text", text:"..."}]`, not plain strings.
The plugin's content extraction only handled strings:
```typescript
// BROKEN: treated array content as empty string
const content = typeof m.content === "string" ? m.content : "";
```

### Fix — `index.ts`
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

### Additional Fixes in Same Deployment

**Empty-input hallucination guard** — `compressor.py` + `main.py`
- Problem: Empty `session_history` sent to Ollama caused it to hallucinate 1,079 chars
  from nothing (reported as `-107,900%` reduction).
- Fix: `total_input < 50` guard returns passthrough in both the endpoint and compressor.

**GPU probe optimization** — `inference_router.py`
- Problem: `_infer_compute_from_generate()` sent `POST /api/generate` with `prompt: "hi"`
  every 5 minutes when no model was loaded, causing unnecessary inference.
- Fix: Try `nvidia-smi` first for instant GPU detection. Fallback uses `raw: True, prompt: ""`
  (loads model without generating tokens).

**Debug logging** — `index.ts`
- Added detailed `api.logger.debug()` on every `before_agent_start` invocation showing
  message count, history length, min threshold, and prompt preview.

### Verification
- Live Discord conversation: 25,146 chars → 578 chars (**97.7% reduction**)
- 18/18 e2e tests passing
- Token comparison benchmark: 85.0% Ollama savings, 85.9% Gemini savings

### Commit
`0b953d6` — "fix: resolve content extraction bug and reduce excessive Ollama inferencing"

---

## [2026-02-25] Initial Extension Scaffolding & Deployment

### What Was Built
Restructured the TokenRanger system from a standalone prototype into a proper
OpenClaw bundled extension following the plugin SDK patterns (modeled after `memory-lancedb`
and `voice-call` extensions).

### Architecture
- **TypeScript plugin** (`index.ts`, `src/*.ts`): Hooks into `before_agent_start` and
  `gateway_start`, registers CLI commands and `/tokenranger` slash command
- **Python FastAPI service** (`service/*.py`): LangChain LCEL chains with Ollama,
  GPU-aware inference routing, 5-minute probe cache
- **System services**: launchd plist (macOS) and systemd unit (Linux) for auto-start
- **Setup CLI**: `openclaw tokenranger setup` handles venv creation, dependency
  install, model pull, and service registration

### Plugin Workflow
```
openclaw plugins enable tokenranger
→ openclaw tokenranger setup
→ openclaw gateway restart
→ compression active on all conversations
```

### Key Design Decisions
- `before_agent_start` hook returns `{ prependContext }` (same pattern as `memory-lancedb`)
- Graceful degradation at two layers: Python returns passthrough if Ollama down;
  TypeScript catches ECONNREFUSED and returns `undefined`
- GPU detection via Ollama `/api/ps` VRAM ratio with nvidia-smi fallback
- Three compression strategies: `full` (GPU, mistral:7b), `light` (CPU, phi3.5:3b),
  `passthrough` (no Ollama)

### Deployment Target
pvet630 (192.168.1.242): 3x NVIDIA GPUs (RTX 3090 24GB + 2x RTX 3060 12GB),
Ubuntu, systemd user unit, Ollama with mistral:7b-instruct loaded in VRAM.

### Initial Benchmark
18/18 e2e tests passing. Token comparison: 84.6% savings (Ollama), 85.3% savings (Gemini).

---

## [2026-02-26] VoC Paper Comparison Analysis

### Context
Compared the TokenRanger implementation against the academic paper
"Value of Computation as an Executive Layer for Memory-Centric Agent Runtimes"
(32-page research paper on VoC-based agent architecture).

### 9 Areas Where VoC Paper Approach Is Superior

1. **Multi-signal decision function**: Paper uses `VoC(s) = [P(s)·G(s) + λ·I(s)] · τ(s) - C(s)`
   combining relevance, expected gain, information value, time-sensitivity, and compute cost.
   Our system uses a simple `sessionHistory.length < 500` character threshold.

2. **Ternary decisions**: Paper proposes compute/defer/skip. Our system is binary (compress or don't).

3. **Architectural inversion**: Paper argues memory should be the hub with LLM called on-demand.
   Our system keeps the LLM as center with compression as a peripheral optimization.

4. **Agent-level vs inference-level VoC**: Paper applies VoC to decide which agent subtasks
   deserve LLM calls at all, not just how to compress context.

5. **Feedback loops**: Paper proposes VoC parameters update based on outcome quality.
   Our system has no learning; thresholds are static.

6. **Structured memory taxonomy**: Paper distinguishes episodic, semantic, and procedural memory
   with different compression policies per type.

7. **Self-scheduling**: Paper's executive layer can preemptively compress during idle periods.

8. **Cost modeling**: Paper models actual $/token costs in the VoC function.

9. **Novelty/epistemic value**: Paper factors in information gain from uncertainty reduction.

### Our Advantage
The TokenRanger system is deployed and working in production with measured 85% token
savings. The VoC paper is entirely theoretical with no implementation or benchmarks.
