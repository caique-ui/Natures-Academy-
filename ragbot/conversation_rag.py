# ragbot/conversation_rag.py
"""
Conversational RAG layer.

Changes over the original:

1. Query rewriting     — follow-ups rewritten into standalone questions.
2. Sub-query expansion — one vague query becomes N focused facets so more
                         relevant chunks are retrieved.
3. Surrounding chunks  — N-1 and N+1 neighbours fetched alongside every
                         matched chunk so the LLM never sees a passage in
                         isolation.
4. History-aware answering — last N messages passed as context.
5. Rewritten prompt    — LLM answers in its own words, cites sources, and
                         only refuses when truly nothing relevant was found.
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
You are helping rewrite a follow-up message from a user into a standalone \
search query for a document database.

Rules:
- If the follow-up is a QUESTION or TOPIC REQUEST about document content \
(e.g. "what about fees?", "how do I enrol?", "terminate a child's enrolment"), \
rewrite it as a clear standalone question using context from the history.
- If the follow-up is a CONVERSATIONAL INSTRUCTION that refers to the \
assistant's previous answer (e.g. "share it as it is", "show me more detail", \
"can you expand on that", "give me the full text", "repeat that"), extract the \
TOPIC of the previous assistant answer and rewrite as: \
"Show full details about <topic>" — so the document search retrieves \
all relevant chunks on that topic.
- If the follow-up is a greeting, thank you, or unrelated chat, return it unchanged.
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
You are a knowledgeable and helpful assistant for Nature's Academy.
Your job is to answer questions using ONLY the retrieved document excerpts \
below.

Guidelines:
1. Answer in clear, natural language using your own words — you do not need \
to copy text verbatim, but stay faithful to what the documents say.
2. Do NOT cite the document itself inline — no "(Source: filename)", and \
do not repeat a document's own Drive/web URL in your answer text. Every \
document you drew on is already shown to the user separately as a \
clickable link below your answer, so repeating it in the text is \
redundant and risks malformed formatting. This does NOT apply to a \
genuinely different external link that appears WITHIN a document's \
content (e.g. the document references an outside website, health \
resource, or government page) — see guideline 9, those you should feel \
free to surface.
3. Base your answer ONLY on the retrieved excerpts. Never supplement with \
your own general/world knowledge, even if you personally "know" the \
answer (e.g. medical, legal, or factual knowledge from training) — if the \
excerpts don't actually contain it, that information is simply not \
available in these documents, and you must say so rather than filling \
the gap.
4. If the excerpts partially answer the question, answer only the part \
they genuinely cover and explicitly say which aspect is not covered — \
do NOT round it out with outside knowledge to make the answer feel \
complete.
5. If the user asks to "share as it is", "show the original text", or \
"give the full document content", reproduce the relevant excerpt text \
as closely as possible, but still without inline source citations or URLs.
6. Say "This topic does not appear in the available documents." whenever \
the retrieved excerpts are only loosely, tangentially, or coincidentally \
related to the question (e.g. they mention the general subject area — \
like "injuries" or "incidents" — but do not actually contain the specific \
information asked for). Only treat excerpts as sufficient when they \
clearly and substantively address what was asked, not merely when they \
share a keyword or topic area with the question.
7. Use your judgment to match different phrasings of the SAME underlying \
policy concept — e.g. "end an enrolment", "terminate enrolment", and \
"withdraw a child" all refer to the same thing. This is about recognizing \
paraphrases of content that IS in the excerpts, not about inferring \
content that isn't.
8. If the question is vague or incomplete (e.g. just a topic keyword), you \
may infer which topic in the excerpts the user most likely means — but \
this only resolves ambiguity about WHICH excerpt content applies, it \
never licenses adding facts the excerpts don't contain.
9. If a phone number, helpline number, email address, or an external \
website link genuinely appears in the retrieved excerpts and is relevant \
to the answer, include it plainly in the text (e.g. "call 1800 670 305" \
or "see https://raisingchildren.net.au/..." ) — the interface will make \
it clickable automatically. This is especially useful when a document \
references an outside resource for more detail (e.g. a health, safety, \
or government site) — surfacing that link directly helps the user far \
more than describing that it exists. Do not invent or guess any contact \
detail or link that isn't actually present in the excerpts.

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
    Generate up to n additional search queries covering different facets of
    the question. The original question is always the first item.

    Temperature is 0.0 (down from 0.3) to reduce run-to-run variance in
    the generated phrasings — though note this only reduces, not
    eliminates, variance: LLM completions aren't perfectly deterministic
    even at temperature 0. The main defense against that residual
    variance actually affecting answer consistency lives in
    multi_query_retrieve() below: the literal original question is always
    searched with a larger k than these generated sub-queries, so the
    "core" retrieved content stays anchored to something deterministic
    regardless of which sub-query phrasings get generated on a given run.

    Falls back gracefully to [question] on any error.
    """
    try:
        client = OpenAI()
        resp = client.chat.completions.create(
            model=settings.OPENAI_CHAT_MODEL,
            messages=[{"role": "user", "content": SUB_QUERY_PROMPT.format(
                question=question, n=n,
            )}],
            temperature=0.0,
            max_tokens=300,
        )
        raw = resp.choices[0].message.content.strip().strip("`").strip()
        if raw.lower().startswith("json"):
            raw = raw[4:].strip()
        sub_queries: list[str] = json.loads(raw)
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
    ensures the LLM has the sentences immediately before and after the matched
    passage — the most common cause of missed answers in policy documents.

    Parameters
    ----------
    retrieved    : list of (score, meta) tuples from store.search()
    store        : DBVectorStore instance (used for fetch_surrounding_chunks)
    snippet_size : max characters per merged excerpt before trimming
    surround     : neighbour chunks to fetch on each side (default 1)
    """
    blocks: list[str] = []
    seen_chunk_pks: set[int] = set()

    for score, meta in retrieved:
        anchor_pk = meta.get("chunk_pk")
        if anchor_pk is None:
            continue

        window = store.fetch_surrounding_chunks(anchor_pk, window=surround)
        if not window:
            continue

        new_chunks = [wc for wc in window if wc["chunk_pk"] not in seen_chunk_pks]
        if not new_chunks:
            continue

        for wc in new_chunks:
            seen_chunk_pks.add(wc["chunk_pk"])

        merged_text = "\n".join(wc["text"] for wc in new_chunks)

        if len(merged_text) > snippet_size:
            cut  = merged_text[:snippet_size]
            last = max(cut.rfind("."), cut.rfind("!"), cut.rfind("?"))
            merged_text = cut[:last + 1] if last > snippet_size * 0.7 else cut + "..."

        first_idx   = new_chunks[0]["chunk_index"]
        last_idx    = new_chunks[-1]["chunk_index"]
        chunk_range = (
            f"Chunk {first_idx}" if first_idx == last_idx
            else f"Chunks {first_idx}–{last_idx}"
        )

        source_url = meta.get("source_url", "")
        source_ref = (
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
    k_original: int = 10,
    max_total: int = 20,
    n_sub_queries: int = 3,
) -> list[tuple]:
    """
    Run the standalone query plus sub-queries, deduplicate by chunk_pk,
    and return up to max_total results sorted by score descending.

    The ORIGINAL (literal, rewritten) question is searched with a larger
    k (k_original=10) than the generated sub-queries (k_per_query=6).
    Combined with the dedup below — which keeps whichever score was seen
    FIRST, and the original question is always processed first — any
    chunk the original question finds keeps ITS score even if a later
    sub-query also happens to find it.

    This makes the original question's own results the stable "core" of
    every answer: they depend only on the user's actual words plus the
    (unchanging) document embeddings, never on which sub-query phrasings
    an LLM happened to generate this run. Sub-queries still contribute
    genuinely useful additional chunks the literal wording might miss —
    that supplementary layer can vary a little run to run, which is fine
    (different words, same underlying facts); the core substance of the
    answer shouldn't, since it's anchored to something deterministic.

    max_total raised from 12 -> 20: up to 4 queries run (original +
    n_sub_queries) at k_original/k_per_query each, so up to ~28 unique
    candidates can be retrieved before this cap — enough room for
    multiple genuinely relevant documents to survive together rather
    than competing for very few slots.

    Parameters
    ----------
    standalone_query : already-rewritten question
    store            : DBVectorStore instance
    k_per_query      : chunks retrieved per generated sub-query
    k_original       : chunks retrieved for the literal original question
    max_total        : cap on chunks passed to the LLM
    n_sub_queries    : number of sub-queries to generate
    """
    all_queries = expand_to_sub_queries(standalone_query, n=n_sub_queries)

    seen_pks: set[int] = set()
    merged: list[tuple] = []

    for i, q in enumerate(all_queries):
        k = k_original if i == 0 else k_per_query
        for score, meta in store.search(q, k=k):
            pk = meta.get("chunk_pk")
            if pk not in seen_pks:
                seen_pks.add(pk)
                merged.append((score, meta))

    merged.sort(key=lambda x: x[0], reverse=True)
    return merged[:max_total]


# ---------------------------------------------------------------------------
# Public API
# ---------------------------------------------------------------------------

def conversational_rag_answer(question: str, conversation: "Conversation") -> dict:
    """
    Full conversational RAG pipeline (non-streaming).

    1. Load recent history.
    2. Rewrite question into standalone query.
    3. Multi-query retrieval with sub-query expansion.
    4. Build context with surrounding chunks.
    5. Generate and return answer.

    Returns:
        {
            "answer":          str,
            "sources":         [{"source_name", "source_url", "chunk_pk", "score"}],
            "rewritten_query": str,
        }
    """
    from ragbot.vectorstore_db import get_db_store

    history = conversation.get_history(max_messages=10)

    standalone_query = rewrite_query(question, history)

    store     = get_db_store()
    retrieved = multi_query_retrieve(standalone_query, store)
    
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