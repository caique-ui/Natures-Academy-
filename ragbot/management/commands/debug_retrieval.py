# ragbot/management/commands/debug_retrieval.py
"""
Django management command for diagnosing retrieval quality.

Usage:
    python manage.py debug_retrieval "terminate a child's enrolment"
    python manage.py debug_retrieval "terminate a child's enrolment" --k 20
    python manage.py debug_retrieval "pricing" --expand   # also show sub-queries
    python manage.py debug_retrieval "pricing" --surround 2  # show wider context window

This helps answer: "Did the vector store actually find the right chunks?"
If the correct chunk appears at score >= 0.70, retrieval is fine and the
problem is in the prompt.  If it's missing entirely, investigate chunking
or re-index with the markdown-cleaning fix.
"""

from __future__ import annotations

from django.core.management.base import BaseCommand

from ragbot.vectorstore_db import get_db_store


class Command(BaseCommand):
    help = "Debug vector-store retrieval for a query"

    def add_arguments(self, parser):
        parser.add_argument("query", type=str, help="The query to test")
        parser.add_argument(
            "--k", type=int, default=15,
            help="Number of chunks to retrieve (default: 15)",
        )
        parser.add_argument(
            "--expand", action="store_true",
            help="Also run sub-query expansion and show merged results",
        )
        parser.add_argument(
            "--surround", type=int, default=0,
            help="Show N surrounding chunks for each result (default: 0)",
        )
        parser.add_argument(
            "--min-score", type=float, default=0.0,
            help="Override minimum score filter (default: store.MIN_SCORE)",
        )

    def handle(self, *args, **options):
        query   = options["query"]
        k       = options["k"]
        expand  = options["expand"]
        surround = options["surround"]

        store = get_db_store()

        # Optionally override min score for diagnostics
        original_min = store.MIN_SCORE
        if options["min_score"] > 0.0:
            store.MIN_SCORE = options["min_score"]

        self.stdout.write(self.style.NOTICE(f"\n{'='*70}"))
        self.stdout.write(self.style.NOTICE(f"Query: {query!r}"))
        self.stdout.write(self.style.NOTICE(f"{'='*70}\n"))

        if expand:
            from ragbot.conversation_rag import expand_to_sub_queries, multi_query_retrieve
            sub_queries = expand_to_sub_queries(query)
            self.stdout.write(self.style.WARNING("Sub-queries generated:"))
            for i, q in enumerate(sub_queries):
                self.stdout.write(f"  {i+1}. {q}")
            self.stdout.write("")

            results = multi_query_retrieve(query, store, k_per_query=k // 3 or 5)
        else:
            results = store.search(query, k=k)

        if not results:
            self.stdout.write(self.style.ERROR("No results returned (all below min_score or index empty)."))
            store.MIN_SCORE = original_min
            return

        self.stdout.write(self.style.SUCCESS(f"{len(results)} result(s) returned:\n"))

        for rank, (score, meta) in enumerate(results, 1):
            colour = (
                self.style.SUCCESS if score >= 0.80 else
                self.style.WARNING if score >= 0.60 else
                self.style.ERROR
            )
            self.stdout.write(colour(
                f"[{rank:02d}] score={score:.4f}  |  "
                f"{meta['source_name']}  |  chunk {meta['chunk']}  |  pk={meta['chunk_pk']}"
            ))

            # First 300 chars of the matched chunk
            preview = meta["text"][:300].replace("\n", " ")
            self.stdout.write(f"     {preview}")

            if surround > 0:
                window = store.fetch_surrounding_chunks(meta["chunk_pk"], window=surround)
                self.stdout.write(self.style.NOTICE(
                    f"     --- surrounding context (window={surround}) ---"
                ))
                for wc in window:
                    marker = ">>>" if wc["is_anchor"] else "   "
                    preview_w = wc["text"][:200].replace("\n", " ")
                    self.stdout.write(f"     {marker} chunk {wc['chunk_index']}: {preview_w}")

            self.stdout.write("")

        store.MIN_SCORE = original_min