from __future__ import annotations

import logging
import dataclasses

from langchain_ollama import ChatOllama
from langchain_core.prompts import ChatPromptTemplate
from langchain_core.output_parsers import StrOutputParser

from inference_router import InferenceRouter, InferenceProfile

logger = logging.getLogger("tokenranger.compressor")


class ContextCompressor:
    """Adapts compression aggressiveness to available compute."""

    def __init__(self, router: InferenceRouter):
        self.router = router
        self._llm_cache: dict[tuple[str, str, int, float], ChatOllama] = {}

    @staticmethod
    def _no_think_prefix(model: str) -> str:
        """Qwen3 models emit hidden thinking tokens by default.
        Prepend /no_think to disable this for compression where
        thinking overhead is pure waste."""
        return "/no_think\n\n" if model.startswith("qwen3") else ""

    def _get_llm(self, profile: InferenceProfile, temperature: float = 0.1) -> ChatOllama:
        """Return a cached ChatOllama instance keyed on (model, base_url, num_ctx, temperature)."""
        key = (profile.model, profile.endpoint_url, profile.max_context, temperature)
        if key not in self._llm_cache:
            self._llm_cache[key] = ChatOllama(
                model=profile.model,
                base_url=profile.endpoint_url,
                num_ctx=profile.max_context,
                temperature=temperature,
            )
        return self._llm_cache[key]

    async def compress(
        self,
        session_history: str,
        lance_results: str,
        user_prompt: str,
        model_override: str | None = None,
        strategy_override: str | None = None,
        turn_meta: list[dict] | None = None,
    ) -> tuple[str, InferenceProfile]:
        """Compress context. Returns (compressed_text, profile_used)."""
        profile = await self.router.probe()

        if strategy_override in ("full", "light", "passthrough"):
            profile = dataclasses.replace(profile, compression_strategy=strategy_override)
        if model_override:
            profile = dataclasses.replace(profile, model=model_override)

        # Strip agent step-indicator lines before any compression path.
        # Lines starting with → (e.g. "→ Checking:", "→ Reading:", "→ Starting:")
        # are internal runtime UI artifacts. If they reach the SLM, the model echoes
        # them back, and the next turn amplifies the loop — planning voice leakage.
        session_history = "\n".join(
            line for line in session_history.splitlines()
            if not line.lstrip().startswith("\u2192")
        )

        # Guard: skip LLM if there is nothing meaningful to compress
        total_input = len(session_history.strip()) + len(lance_results.strip())
        if total_input < 50:
            logger.debug("compress: skipping, input too short (%d chars)", total_input)
            return self._passthrough(session_history, lance_results), profile

        if profile.compression_strategy == "passthrough":
            return self._passthrough(session_history, lance_results), profile

        meta = turn_meta or []

        if profile.compression_strategy == "full":
            result = await self._full_compression(
                profile, session_history, lance_results, user_prompt, meta
            )
            return result, profile

        if profile.compression_strategy == "light":
            result = await self._light_compression(
                profile, session_history, lance_results, user_prompt, meta
            )
            return result, profile

        return self._passthrough(session_history, lance_results), profile

    @staticmethod
    def _build_turn_guidance(turn_meta: list[dict]) -> str:
        """Build per-turn compression instructions from metadata.

        Early user turns (T1, T2) contain task specifications and constraints
        that must be preserved verbatim. Later turns can be summarized
        aggressively. Turns flagged with code had code blocks stripped before
        reaching the compressor — note this in the summary.
        """
        if not turn_meta:
            return ""

        preserve_turns = []
        code_turns = []
        for t in turn_meta:
            if t.get("n", 0) <= 2 and t.get("role") == "user":
                preserve_turns.append(f"T{t['n']}")
            if t.get("has_code"):
                code_turns.append(f"T{t['n']}")

        parts = []
        if preserve_turns:
            parts.append(
                f"IMPORTANT: Preserve {', '.join(preserve_turns)} nearly verbatim — "
                "these contain the user's original instructions and constraints."
            )
        if code_turns:
            parts.append(
                f"Turns {', '.join(code_turns)} originally contained code blocks "
                "(stripped before compression). Note 'code discussed' in their summary."
            )
        parts.append(
            "Summarize as factual state. Do NOT use first-person ('I'll...', 'I will...'). "
            "IMPORTANT: If an assistant turn contains only planning statements without a "
            "concrete result (e.g. 'Let me check...', 'I'll investigate...', "
            "'I need to verify...', 'Checking now...'), discard it entirely — "
            "do NOT summarize the intent, only summarize completed actions and their results. "
            "Output as: '[T<n>] bullet summary' for each turn."
        )
        return " ".join(parts)

    async def _full_compression(
        self,
        profile: InferenceProfile,
        session_history: str,
        lance_results: str,
        user_prompt: str,
        turn_meta: list[dict],
    ) -> str:
        """GPU mode: Use 7B model for deep summarization."""
        llm = self._get_llm(profile, temperature=0.1)

        turn_guidance = self._build_turn_guidance(turn_meta)
        no_think = self._no_think_prefix(profile.model)
        system_msg = (
            no_think
            + "You are a context compressor for a multi-turn conversation. "
            "Each turn is tagged as [T<n>:<role>|<size>] or [T<n>:<role>|<size>|code] "
            "(the |code flag means code blocks were stripped before compression). "
            "Extract only: key decisions made, current state of work, "
            "open questions, and user preferences. "
            "Discard greetings, pleasantries, and redundant information. "
            "Discard any assistant turn that contains only planning statements without a "
            "concrete result (e.g. 'Let me check...', 'I'll investigate...', "
            "'Checking now...', 'I need to verify...') — summarize only completed "
            "actions and their results, never unexecuted intent. "
            "Summarize as factual state. Do NOT use first-person "
            "('I'll...', 'I will...', 'Let me...'). "
            + turn_guidance
        )

        compress_prompt = ChatPromptTemplate.from_messages([
            ("system", system_msg),
            ("human", "{history}"),
        ])
        compress_chain = compress_prompt | llm | StrOutputParser()
        compressed_history = await compress_chain.ainvoke(
            {"history": session_history}
        )

        filtered_memories = lance_results
        if lance_results and len(lance_results) > 100:
            filter_prompt = ChatPromptTemplate.from_messages([
                ("system",
                 no_think
                 + "Given the user's current message, keep only the memories "
                 "that are directly relevant. Remove anything unrelated. "
                 "Output as a concise third-person factual bullet list. "
                 "Do NOT use first-person phrasing."),
                ("human",
                 "User message: {prompt}\n\nMemories:\n{memories}"),
            ])
            filter_chain = filter_prompt | llm | StrOutputParser()
            filtered_memories = await filter_chain.ainvoke(
                {"prompt": user_prompt, "memories": lance_results}
            )

        return self._format_context(compressed_history, filtered_memories)

    async def _light_compression(
        self,
        profile: InferenceProfile,
        session_history: str,
        lance_results: str,
        user_prompt: str,
        turn_meta: list[dict],
    ) -> str:
        """CPU mode: Use 3B model for extractive-only compression."""
        llm = self._get_llm(profile, temperature=0.0)

        # Light mode: simpler prompt but still turn-aware
        preserve_note = ""
        for t in turn_meta:
            if t.get("n", 0) <= 2 and t.get("role") == "user":
                preserve_note = " Keep T1/T2 user instructions intact."
                break

        no_think = self._no_think_prefix(profile.model)
        compress_prompt = ChatPromptTemplate.from_messages([
            ("system",
             no_think
             + "Extract key facts from this tagged conversation as a short bullet list. "
             "Each turn is tagged as [T<n>:<role>|<size>] or [T<n>:<role>|<size>|code]. "
             "Max 10 bullets. Only include decisions, state, and open items. "
             "Skip any assistant turn that is only planning statements with no result "
             "('Let me check...', 'I'll investigate...', 'Checking now...'). "
             "Do NOT use first-person phrasing ('I'll...', 'I will...', 'Let me...'). "
             "Output as: '- [T<n>] fact' for each relevant turn."
             + preserve_note),
            ("human", "{history}"),
        ])
        compress_chain = compress_prompt | llm | StrOutputParser()
        compressed_history = await compress_chain.ainvoke(
            {"history": session_history}
        )

        return self._format_context(compressed_history, lance_results)

    def _passthrough(self, session_history: str, lance_results: str) -> str:
        """No Ollama: Heuristic extraction only."""
        lines = session_history.strip().split("\n")
        truncated = "\n".join(lines[-20:]) if len(lines) > 20 else session_history
        return self._format_context(truncated, lance_results)

    @staticmethod
    def _strip_arrow_lines(text: str) -> str:
        """Remove any → step-indicator lines from SLM output.
        Applied to compressed output so leaked arrows never enter the session history,
        regardless of whether they came from the input or were hallucinated by the SLM."""
        return "\n".join(
            line for line in text.splitlines()
            if not line.lstrip().startswith("\u2192")
        ).strip()

    def _format_context(self, history: str, memories: str) -> str:
        history = self._strip_arrow_lines(history)
        parts = []
        if history:
            parts.append(f"<session-summary>\n{history}\n</session-summary>")
        if memories:
            parts.append(f"<relevant-memories>\n{memories}\n</relevant-memories>")
        return "\n\n".join(parts)
