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

    async def compress(
        self,
        session_history: str,
        lance_results: str,
        user_prompt: str,
        model_override: str | None = None,
        strategy_override: str | None = None,
    ) -> tuple:
        """Compress context. Returns (compressed_text, profile_used)."""
        profile = await self.router.probe()

        if strategy_override in ("full", "light", "passthrough"):
            profile = dataclasses.replace(profile, compression_strategy=strategy_override)
        if model_override:
            profile = dataclasses.replace(profile, model=model_override)

        # Guard: skip LLM if there is nothing meaningful to compress
        total_input = len(session_history.strip()) + len(lance_results.strip())
        if total_input < 50:
            logger.debug("compress: skipping, input too short (%d chars)", total_input)
            return self._passthrough(session_history, lance_results), profile

        if profile.compression_strategy == "passthrough":
            return self._passthrough(session_history, lance_results), profile

        if profile.compression_strategy == "full":
            result = await self._full_compression(
                profile, session_history, lance_results, user_prompt
            )
            return result, profile

        if profile.compression_strategy == "light":
            result = await self._light_compression(
                profile, session_history, lance_results, user_prompt
            )
            return result, profile

        return self._passthrough(session_history, lance_results), profile

    async def _full_compression(
        self,
        profile: InferenceProfile,
        session_history: str,
        lance_results: str,
        user_prompt: str,
    ) -> str:
        """GPU mode: Use 7B model for deep summarization."""
        llm = ChatOllama(
            model=profile.model,
            base_url=profile.endpoint_url,
            num_ctx=profile.max_context,
            temperature=0.1,
        )

        compress_prompt = ChatPromptTemplate.from_messages([
            ("system",
             "You are a context compressor. Extract only: key decisions made, "
             "current state of work, open questions, and user preferences. "
             "Output as a concise bullet list. Discard greetings, "
             "pleasantries, and redundant information."),
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
                 "Given the user's current message, keep only the memories "
                 "that are directly relevant. Remove anything unrelated. "
                 "Output as a concise bullet list."),
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
    ) -> str:
        """CPU mode: Use 3B model for extractive-only compression."""
        llm = ChatOllama(
            model=profile.model,
            base_url=profile.endpoint_url,
            num_ctx=profile.max_context,
            temperature=0.0,
        )

        compress_prompt = ChatPromptTemplate.from_messages([
            ("system",
             "Extract key facts from this conversation as a short bullet list. "
             "Max 10 bullets. Only include decisions, state, and open items."),
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

    def _format_context(self, history: str, memories: str) -> str:
        parts = []
        if history:
            parts.append(f"<session-summary>\n{history}\n</session-summary>")
        if memories:
            parts.append(f"<relevant-memories>\n{memories}\n</relevant-memories>")
        return "\n\n".join(parts)
