# TokenRanger V2 — Turn-Tagged Compression Plan

Research-driven plan for the next major iteration. Based on analysis in
`TurnTagging` and production observations from V1 deployment.

**Status**: Planning only. Not for implementation in current PR.

---

## Problem Statement (V1 Limitations)

1. **Planning voice leakage**: The SLM compressor flattens agent planning
   ("I'll verify X...") into assistant history, causing the cloud LLM to
   continue in that voice — visible as repetitive meta-level statements
   instead of concise task output.

2. **Role flattening**: Compression loses distinctions between system,
   user, assistant, and tool messages. Internal agent state leaks into
   user-visible channel.

3. **Control instruction stripping**: Safety constraints, formatting
   guidelines, and "don't show chain-of-thought" instructions get
   summarized away, causing style/safety drift.

4. **Reconstruction errors**: Lossy compression over many turns causes
   subtle factual drift — wrong filenames, counts, or constraints.

5. **No temporal awareness**: Compressed context has no notion of when
   things happened, making it harder for the cloud LLM to distinguish
   stale state from current state.

---

## V2 Architecture: Turn-Tagged Selective Compression

### Core Concept

Replace flat compression with structured, turn-aware, role-sensitive
compression that preserves code and critical instructions verbatim.

### Turn Tagging

Prefix each compressed turn summary with `[Turn N]`:

```
[Turn 1] User asked for luxury apartment scraping in San Diego.
Output format: CSV with name, url, mgmt_company, units, contacts.
No duplicates.

[Turn 2] Agent verified initial URLs, found issues with "Centrum"
and "Aura Circle". Updated scrape_sd_comprehensive.py with 95
properties.

[Turn 3] Script running. sd_luxury_apts_comprehensive.csv created.
12 URLs pending verification.
```

**Benefits**:
- Temporal awareness for the cloud LLM
- Prevents repetition (model sees it already did X in turn 4)
- Better debugging/observability
- Compression decisions become turn-aware

### Selective Compression Policy

```
Turn 1:     NEVER compress. Full fidelity.
Turn 2:     NEVER compress. Let model see its own first reasoning.
Turn 3+:    Compress selectively:
  - Compress everything BEFORE the last 2 turns
  - Keep most recent user + assistant + tool messages uncompressed
  - Trigger full compression only when total context > 60% of limit
```

### Content-Type Awareness

Split each message into segments before compression:

| Segment Type | Treatment |
|-------------|-----------|
| Code blocks (```) | NEVER compress. Pass through verbatim. |
| JSON/YAML/config | NEVER compress. Pass through verbatim. |
| Stack traces | Keep 1-2 canonical examples, summarize rest |
| Tool outputs | Summarize older ones, keep latest verbatim |
| System instructions | NEVER compress. Concatenate on every call |
| User messages | Compress older turns, keep recent verbatim |
| Assistant planning ("I'll...") | Collapse to factual state |
| Assistant final answers | Compress normally |

### Compressor SLM Prompt Changes

Current V1 prompt is generic summarization. V2 prompt should be:

```
You are summarizing an internal agent conversation. Your output will
be fed back to the main model as context.

Rules:
- Output as tagged turns: '[Turn 1] Summary. [Turn 2] Summary.'
- Summarize as FACTUAL STATE only: what has been done, what remains,
  relevant data.
- Do NOT use first-person commitments ("I'll...", "I'm going to...").
- Do NOT remove or alter safety/control instructions from system
  messages.
- PRESERVE verbatim: file names, counts, URLs, exact column names,
  user constraints, and "don't do X" rules.
- Do NOT invent new instructions or constraints.
- OMIT: repetitive planning statements, redundant status updates,
  internal tool-call scaffolding.
```

---

## Implementation Plan

### Phase 1: Turn Counter + Metadata (TypeScript plugin)

**File**: `index.ts` — `before_agent_start` hook

- Count user turns from `event.messages`
- Tag each turn in the session history with `[Turn N]`
- Pass turn count to the Python service as a new field
- The service can use turn count for compression decisions

### Phase 2: Content-Type Splitter (Python service)

**File**: `service/compressor.py`

- Add `split_code_and_text(content)` function:
  - Regex to extract triple-backtick blocks, JSON blocks, YAML blocks
  - Returns `(code_parts: list[str], text_parts: list[str])`
- Only send `text_parts` to the LangChain LCEL chain
- Reassemble: `compressed_text + preserved_code`

### Phase 3: Sliding Window Compression

**File**: `service/compressor.py`

- Split messages into "old" (before last 2 turns) and "recent"
- Only compress "old" messages
- Keep "recent" messages verbatim
- New `CompressRequest` fields:
  - `turn_count: int`
  - `recent_window: int = 2` (configurable)

### Phase 4: Role-Aware Compression

**File**: `service/compressor.py`

- Separate messages by role before compression
- System messages: never compress, pass through
- Tool messages: summarize older, keep latest
- Assistant planning: collapse to state bullets
- User messages: compress with key-detail preservation

### Phase 5: Threshold-Based Trigger

**File**: `index.ts` — `before_agent_start` hook

- Estimate token count of current context
- Only trigger compression when context exceeds 60% of model limit
- Below threshold: pass through without any compression overhead
- New config fields:
  - `compressionThreshold: number` (0.0-1.0, default 0.6)
  - `contextLimit: number` (default 128000)

### Phase 6: Hierarchical Summarization

**File**: `service/compressor.py`

- Multi-level compression for very long sessions (30+ turns):
  - Level 1: Latest 2 turns (full verbatim)
  - Level 2: Turns 3-10 (per-turn summaries)
  - Level 3: Turns 1-2 + older (high-level session summary)
- Each level is a separate SLM call with appropriate prompts

---

## API Changes (CompressRequest)

```python
class CompressRequestV2(BaseModel):
    prompt: str
    session_history: str = ""
    lance_results: str = ""
    max_tokens: int = 2000
    model_override: Optional[str] = None
    strategy_override: Optional[str] = None
    # V2 fields
    turn_count: int = 0
    recent_window: int = 2
    code_blocks: list[str] = []       # Pre-extracted, passed through
    system_instructions: str = ""     # Preserved verbatim
    compression_level: str = "auto"   # auto|per-turn|hierarchical
```

---

## Metrics to Track (A/B Testing)

| Metric | V1 Baseline | V2 Target |
|--------|-------------|-----------|
| Token reduction (>5 turns) | 50-85% | 60-90% |
| Planning voice leakage rate | Unknown | <5% |
| Code syntax preservation | Not measured | 100% |
| Reconstruction error rate | Unknown | <2% |
| Avg latency per compression | 1.6s | <2.0s |
| Turns to task completion | Baseline | Same or fewer |

---

## References

- [Acon: Optimizing Context Compression for Long-horizon LLM Agents](https://arxiv.org/html/2510.00615v1)
- [Context Engineering - LangChain Blog](https://blog.langchain.com/context-engineering-for-agents/)
- [Context Management for Deep Agents - LangChain Blog](https://blog.langchain.com/context-management-for-deepagents/)
- [LLMLingua/SecurityLingua structured compression](https://www.freecodecamp.org/news/how-to-compress-your-prompts-and-reduce-llm-costs/)
- [Factory.ai: Compressing Context](https://factory.ai/news/compressing-context)
- Local analysis: `TurnTagging` (production trace analysis + recommendations)
