# ragbot/conversation_rag.py
"""
Conversational RAG layer.

Adds two things on top of the existing single-turn RAG:

1. Query rewriting
   A follow-up like "what about the pricing?" is rewritten into a
   self-contained question ("What is the pricing for Nature's Academy
   courses?") before hitting the vector store. This is the single most
   important change for quality.

2. History-aware answering
   The last N messages are passed as context so the LLM can refer back
   to earlier turns naturally.

Nothing in vectorstore_db.py or models.py (existing) is modified.
"""

from __future__ import annotations

import logging
from typing import TYPE_CHECKING

from django.conf import settings
from openai import OpenAI

if TYPE_CHECKING:
    from ragbot.models import Conversation

logger = logging.getLogger(__name__)

# ---------------------------------------------------------------------------
# Prompts
# ---------------------------------------------------------------------------

CONDENSE_PROMPT = """\
Given the conversation history and a follow-up message from the user, \
rewrite the follow-up as a fully self-contained question that includes \
all necessary context from the history.

Rules:
- If the follow-up is already standalone (first message, or makes complete \
sense without history), return it unchanged.
- Output ONLY the rewritten question — no preamble, no explanation.
- Keep it concise; do not pad with unnecessary words.

Conversation history:
{history}

Follow-up: {question}
Standalone question:"""


CHAT_PROMPT = """\
You are a document assistant for Nature's Academy. \
Answer the user's question using ONLY the retrieved document context below.

Strict rules:
1. Use exact wording, figures, and structure from the documents.
2. Cite the source after every claim: (Source: filename).
3. If the answer is not in the context, say: \
"This information is not available in the provided documents."
4. Do not add external knowledge or assumptions.
5. Preserve tables, lists, and procedural steps from the source.

{history_block}\
Retrieved context:
{context}

User: {question}
Assistant:"""


# ---------------------------------------------------------------------------
# Public API
# ---------------------------------------------------------------------------

def rewrite_query(question: str, history: list[dict]) -> str:
    """
    Rewrite a follow-up question into a standalone retrieval query.
    Returns the original question unchanged if history is empty.
    """
    if not history:
        return question

    history_text = "\n".join(
        f"{m['role'].capitalize()}: {m['content']}" for m in history[-6:]
    )
    prompt = CONDENSE_PROMPT.format(history=history_text, question=question)

    try:
        client = OpenAI()
        resp = client.chat.completions.create(
            model=settings.OPENAI_CHAT_MODEL,
            messages=[{"role": "user", "content": prompt}],
            temperature=0.0,
            max_tokens=200,
        )
        rewritten = resp.choices[0].message.content.strip()
        logger.debug("Query rewrite: %r → %r", question, rewritten)
        return rewritten or question
    except Exception as exc:
        logger.warning("Query rewrite failed (%s); using original query.", exc)
        return question


def build_context(retrieved: list[tuple], snippet_size: int = 2000) -> str:
    """
    Build the context block from vectorstore search results.
    Identical logic to views.py build_context() so both paths stay in sync.
    """
    blocks = []
    seen   = set()
    for score, meta in retrieved:
        key = f"{meta.get('source_name')}_{meta.get('chunk')}"
        if key in seen:
            continue
        seen.add(key)
        text = meta.get("text", "")
        if len(text) > snippet_size:
            # Trim at sentence boundary
            cut = text[:snippet_size]
            last = max(cut.rfind("."), cut.rfind("!"), cut.rfind("?"))
            text = cut[: last + 1] if last > snippet_size * 0.7 else cut + "..."
        blocks.append(
            f"=== DOCUMENT EXCERPT ===\n"
            f"Source: {meta.get('source_name', 'Unknown')} "
            f"(Chunk {meta.get('chunk', 0)}, score {score:.3f})\n"
            f"Content:\n{text}\n"
            f"=== END EXCERPT ==="
        )
    return "\n\n".join(blocks) if blocks else "No relevant context found."


def conversational_rag_answer(question: str, conversation: "Conversation") -> dict:
    """
    Full conversational RAG pipeline:

    1. Load recent history from the Conversation.
    2. Rewrite the question into a standalone retrieval query.
    3. Retrieve chunks from the vector store.
    4. Build prompt with history + context.
    5. Stream or return full answer.

    Returns:
        {
            "answer":   str,
            "sources":  [{"source_name": str, "chunk_pk": int, "score": float}],
            "rewritten_query": str,   # useful for debugging
        }
    """
    from ragbot.vectorstore_db import get_db_store

    history = conversation.get_history(max_messages=10)

    # Step 1 — rewrite
    standalone_query = rewrite_query(question, history)

    # Step 2 — retrieve
    store     = get_db_store()
    retrieved = store.search(standalone_query, k=8)

    # Step 3 — build context
    context_text = build_context(retrieved, snippet_size=2000)

    # Step 4 — build history block for the answer prompt
    if history:
        history_lines = "\n".join(
            f"{m['role'].capitalize()}: {m['content']}" for m in history
        )
        history_block = f"Conversation so far:\n{history_lines}\n\n"
    else:
        history_block = ""

    prompt_content = CHAT_PROMPT.format(
        history_block=history_block,
        context=context_text,
        question=question,
    )

    # Step 5 — generate
    client = OpenAI()
    resp = client.chat.completions.create(
        model=settings.OPENAI_CHAT_MODEL,
        messages=[{"role": "user", "content": prompt_content}],
        temperature=0.1,
        max_tokens=2000,
    )
    answer = resp.choices[0].message.content

    sources = [
        {
            "source_name": meta.get("source_name", "Unknown"),
            "chunk_pk":    meta.get("chunk_pk"),
            "score":       round(score, 3),
        }
        for score, meta in retrieved
    ]

    return {
        "answer":          answer,
        "sources":         sources,
        "rewritten_query": standalone_query,
    }


def conversational_rag_stream(question: str, conversation: "Conversation"):
    """
    Same as conversational_rag_answer() but yields SSE-formatted strings
    for use in a StreamingHttpResponse.

    Yields strings ready to write directly (caller adds 'data: ' prefix).
    Use like:

        for chunk in conversational_rag_stream(q, conv):
            yield chunk
    """
    import json
    from ragbot.vectorstore_db import get_db_store

    def _event(payload: dict) -> str:
        return f"data: {json.dumps(payload)}\n\n"

    try:
        yield _event({"type": "status", "message": "Loading conversation history..."})
        history = conversation.get_history(max_messages=10)

        yield _event({"type": "status", "message": "Rewriting query..."})
        standalone_query = rewrite_query(question, history)
        yield _event({"type": "status", "message": "Searching documents..."})
        store     = get_db_store()
        retrieved = store.search(standalone_query, k=8)
        context_text = build_context(retrieved, snippet_size=2000)
        if history:
            history_lines = "\n".join(
                f"{m['role'].capitalize()}: {m['content']}" for m in history
            )
            history_block = f"Conversation so far:\n{history_lines}\n\n"
        else:
            history_block = ""

        prompt_content = CHAT_PROMPT.format(
            history_block=history_block,
            context=context_text,
            question=question,
        )

        yield _event({"type": "status", "message": "Generating response..."})
        yield _event({"type": "clear_status"})

        client = OpenAI()
        stream = client.chat.completions.create(
            model=settings.OPENAI_CHAT_MODEL,
            messages=[{"role": "user", "content": prompt_content}],
            temperature=0.1,
            max_tokens=2000,
            stream=True,
        )

        full_response = ""
        for chunk in stream:
            delta = chunk.choices[0].delta.content
            if delta is not None:
                full_response += delta
                yield _event({"type": "content", "content": delta})

        sources = [
            {
                "source_name": meta.get("source_name", "Unknown"),
                "chunk_pk":    meta.get("chunk_pk"),
                "score":       round(score, 3),
            }
            for score, meta in retrieved
        ]

        yield _event({
            "type":            "done",
            "full_response":   full_response,
            "sources":         sources,
            "rewritten_query": standalone_query,
        })

    except Exception as exc:
        logger.error("conversational_rag_stream error: %s", exc, exc_info=True)
        yield _event({"type": "error", "error": str(exc)})