# ragbot/conversation_rag.py
"""
Conversational RAG layer.

Improvements over the original:

1. Query rewriting
   A follow-up like "what about the pricing?" is rewritten into a
   self-contained question before hitting the vector store.

2. Sub-query expansion
   The standalone query is expanded into N facets and results are merged,
   so vague or indirect questions retrieve more relevant chunks.

3. Surrounding chunk context
   For every matched chunk, the chunk immediately before and after (in the
   same document) is fetched and merged into the excerpt. This prevents
   answers from being missed because the key sentence sat just outside the
   matched chunk.

4. History-aware answering
   The last N messages are passed as context so the LLM can refer back
   to earlier turns naturally.

5. Rewritten system prompt
   The LLM is instructed to answer in its own words, cite sources, and only
   refuse when NONE of the retrieved chunks are relevant — not on every
   partial match.

Nothing in models.py is modified.
"""

from __future__ import annotations

import json
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

Conversation history:
{history}

Follow-up: {question}
Standalone question:"""


SUB_QUERY_PROMPT = """\
Given a user question, generate {n} different search queries that together \
cover the full scope of what the user wants to know. These will be used to \
search a policy-document database, so phrase each query the way a policy \
document might word the same concept.

Rules:
- Each query must be 5-12 words and target a different facet of the question.
- Include the original question's core terms in at least one query.
- Output ONLY a valid JSON array of strings, no preamble, no code fences.

User question: {question}
JSON array:"""


CHAT_PROMPT = """\
You are a helpful document assistant for Nature's Academy.
Your job is to answer questions using the retrieved document excerpts below.

Guidelines:
1. Base your answer on the retrieved excerpts. Write in your own words — \
do not copy text verbatim from the documents.
2. After each factual claim, cite the source in parentheses: \
(Source: <filename>). If multiple excerpts support a claim, list all sources.
3. If the retrieved excerpts contain relevant information but do not fully \
answer the question, answer as much as you can from the excerpts and clearly \
note what aspect is not covered.
4. Only say "This topic does not appear in the available documents." when \
NONE of the retrieved excerpts are relevant to the question at all.
5. If the question uses different wording than the documents, use your \
judgment to recognise when the excerpts address the same topic.

{history_block}\
Retrieved document excerpts:
{context}

User question: {question}
Answer:"""


# ---------------------------------------------------------------------------
# Query rewriting
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


# ---------------------------------------------------------------------------
# Sub-query expansion
# ---------------------------------------------------------------------------

def expand_to_sub_queries(question: str, n: int = 3) -> list[str]:
    """
    Generate up to n additional search queries that cover different facets of
    the question.  The original question is always included as the first item.

    Falls back gracefully to [question] on any error.
    """
    try:
        client = OpenAI()
        resp = client.chat.completions.create(
            model=settings.OPENAI_CHAT_MODEL,
            messages=[{"role": "user", "content": SUB_QUERY_PROMPT.format(
                question=question, n=n,
            )}],
            temperature=0.3,
            max_tokens=300,
        )
        raw = resp.choices[0].message.content.strip()
        # Strip accidental code-fence wrapping
        raw = raw.strip("`").strip()
        if raw.startswith("json"):
            raw = raw[4:].strip()
        sub_queries: list[str] = json.loads(raw)
        # Always keep the original as the anchor query
        all_queries = [question] + [q for q in sub_queries if q != question]
        logger.debug("Sub-queries for %r: %s", question, all_queries)
        return all_queries
    except Exception as exc:
        logger.warning("Sub-query expansion failed (%s); using original query.", exc)
        return [question]


# ---------------------------------------------------------------------------
# Context building (with surrounding chunk fetch)
# ---------------------------------------------------------------------------

def build_context(
    retrieved: list[tuple],
    store,
    snippet_size: int = 2000,
    surround: int = 1,
) -> str:
    """
    Build the context block from vector-store search results.

    For each matched chunk, fetches `surround` neighbours on each side from
    the same source document and merges them into a single excerpt.  This
    ensures the LLM has the sentence(s) immediately before and after the
    matched passage, which is the most common reason for missed answers.

    Parameters
    ----------
    retrieved   : list of (score, meta) tuples from store.search()
    store       : DBVectorStore instance (for fetch_surrounding_chunks)
    snippet_size: max characters per merged excerpt before trimming
    surround    : how many neighbour chunks to fetch on each side (default 1)
    """
    blocks: list[str] = []
    seen_chunk_pks: set[int] = set()

    for score, meta in retrieved:
        anchor_pk = meta.get("chunk_pk")
        if anchor_pk is None:
            continue

        # Fetch anchor + neighbours
        window = store.fetch_surrounding_chunks(anchor_pk, window=surround)
        if not window:
            continue

        # Collect only chunks we haven't already included from a prior result
        new_chunks = [wc for wc in window if wc["chunk_pk"] not in seen_chunk_pks]
        if not new_chunks:
            continue

        for wc in new_chunks:
            seen_chunk_pks.add(wc["chunk_pk"])

        # Merge the texts in sequence order (fetch_surrounding_chunks returns
        # them ordered by chunk_index)
        merged_text = "\n".join(wc["text"] for wc in new_chunks)

        if len(merged_text) > snippet_size:
            cut = merged_text[:snippet_size]
            last = max(cut.rfind("."), cut.rfind("!"), cut.rfind("?"))
            merged_text = cut[:last + 1] if last > snippet_size * 0.7 else cut + "..."

        first_idx = new_chunks[0]["chunk_index"]
        last_idx  = new_chunks[-1]["chunk_index"]
        chunk_range = (
            f"Chunk {first_idx}" if first_idx == last_idx
            else f"Chunks {first_idx}–{last_idx}"
        )

        source_url  = meta.get("source_url", "")
        source_ref  = (
            f"{meta.get('source_name', 'Unknown')} ({source_url})"
            if source_url else
            meta.get("source_name", "Unknown")
        )

        blocks.append(
            f"=== DOCUMENT EXCERPT ===\n"
            f"Source: {source_ref} | {chunk_range} | match score {score:.3f}\n"
            f"Content:\n{merged_text}\n"
            f"=== END EXCERPT ==="
        )

    return "\n\n".join(blocks) if blocks else "No relevant context found."


# ---------------------------------------------------------------------------
# Multi-query retrieval
# ---------------------------------------------------------------------------

def multi_query_retrieve(
    standalone_query: str,
    store,
    k_per_query: int = 6,
    max_total: int = 12,
    n_sub_queries: int = 3,
) -> list[tuple]:
    """
    Run the standalone query plus sub-queries against the vector store and
    return a deduplicated, score-sorted list of (score, meta) tuples.

    Parameters
    ----------
    standalone_query : already-rewritten question
    store            : DBVectorStore instance
    k_per_query      : chunks retrieved per query (default 6)
    max_total        : maximum chunks to pass to the LLM (default 12)
    n_sub_queries    : number of sub-queries to generate (default 3)
    """
    all_queries = expand_to_sub_queries(standalone_query, n=n_sub_queries)

    seen_pks: set[int] = set()
    merged: list[tuple] = []

    for q in all_queries:
        for score, meta in store.search(q, k=k_per_query):
            pk = meta.get("chunk_pk")
            if pk not in seen_pks:
                seen_pks.add(pk)
                merged.append((score, meta))

    # Sort descending by score, keep top max_total
    merged.sort(key=lambda x: x[0], reverse=True)
    return merged[:max_total]


# ---------------------------------------------------------------------------
# Public API
# ---------------------------------------------------------------------------

def conversational_rag_answer(question: str, conversation: "Conversation") -> dict:
    """
    Full conversational RAG pipeline:

    1. Load recent history from the Conversation.
    2. Rewrite the question into a standalone retrieval query.
    3. Expand into sub-queries and retrieve chunks from the vector store.
    4. Fetch surrounding chunks for each match.
    5. Build prompt with history + context.
    6. Return full answer.

    Returns:
        {
            "answer":          str,
            "sources":         [{"source_name": str, "chunk_pk": int, "score": float}],
            "rewritten_query": str,
        }
    """
    from ragbot.vectorstore_db import get_db_store

    history = conversation.get_history(max_messages=10)

    # Step 1 — rewrite
    standalone_query = rewrite_query(question, history)

    # Step 2 — retrieve (multi-query)
    store     = get_db_store()
    retrieved = multi_query_retrieve(standalone_query, store)

    # Step 3 — build context with surrounding chunks
    context_text = build_context(retrieved, store, snippet_size=2000, surround=1)

    # Step 4 — build history block
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
            "source_url":  meta.get("source_url", ""),
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
    Same pipeline as conversational_rag_answer() but yields SSE-formatted
    strings for use in a StreamingHttpResponse.

    Yields strings ready to write directly (caller adds 'data: ' prefix).
    Use like:

        for chunk in conversational_rag_stream(q, conv):
            yield chunk
    """
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
        retrieved = multi_query_retrieve(standalone_query, store)

        yield _event({"type": "status", "message": "Building context..."})
        context_text = build_context(retrieved, store, snippet_size=2000, surround=1)

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
                "source_url":  meta.get("source_url", ""),
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