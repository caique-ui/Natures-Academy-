from __future__ import annotations

import gc
import re
import threading
import time
from typing import Dict, List, Optional, Tuple

import faiss
import numpy as np
from django.conf import settings
from django.db import transaction
from django.utils import timezone
from openai import OpenAI

from .models import DocumentChunk, IndexVersion, SourceDocument

# ---------------------------------------------------------------------------
# helpers
# ---------------------------------------------------------------------------

def _use_pgvector() -> bool:
    return getattr(settings, "VECTORSTORE_USE_PGVECTOR", False)


def _embed_dim_for_model(model: str) -> int:
    return {
        "text-embedding-3-small": 1536,
        "text-embedding-3-large": 3072,
        "text-embedding-ada-002": 1536,
    }.get(model, 1536)


def _vec_to_bytes(vec: np.ndarray) -> bytes:
    return vec.astype("float32").tobytes()


def _bytes_to_vec(b: bytes, dim: int) -> np.ndarray:
    return np.frombuffer(b, dtype="float32").copy()


def clean_chunk_for_embedding(text: str) -> str:
    """
    Return a cleaner version of a chunk suitable for embedding.

    Markdown pipe-tables embed poorly because the tokeniser treats '|' as noise.
    This converts them to prose so the embedding captures the actual content.
    The original (formatted) text is kept in DocumentChunk.text for display;
    only the cleaned version is sent to the embedding API.

    Transformations:
    - Separator rows  (| --- | --- |)  → dropped entirely
    - Data rows       (| cell | cell |) → "cell. cell" prose
    - Multiple blank lines             → collapsed to one
    """
    lines = text.splitlines()
    cleaned: List[str] = []

    for line in lines:
        stripped = line.strip()
        # Drop markdown table separator rows: | :--- | ---: |
        if re.match(r"^\|[-| :]+\|$", stripped):
            continue
        # Convert table data rows to prose
        if stripped.startswith("|") and stripped.endswith("|"):
            cells = [c.strip() for c in stripped.strip("|").split("|") if c.strip()]
            if cells:
                cleaned.append(". ".join(cells))
            continue
        cleaned.append(line)

    result = re.sub(r"\n{3,}", "\n\n", "\n".join(cleaned))
    return result.strip()


def _versions_cache_key(version: Optional[IndexVersion], version_web: Optional[IndexVersion]) -> str:
    """
    A cache key that changes whenever EITHER the Drive or web version changes.
    Storing only the Drive version pk (original code) meant a web-only update
    never triggered a FAISS rebuild.
    """
    drive_pk = version.pk if version else 0
    web_pk   = version_web.pk if version_web else 0
    return f"{drive_pk}:{web_pk}"


# ---------------------------------------------------------------------------
# main class
# ---------------------------------------------------------------------------

class DBVectorStore:
    """
    Versioned vector store backed by PostgreSQL.

    Typical usage
    -------------
    store = DBVectorStore(folder_id="<drive-folder-id>")
    store.add_texts(texts, metadatas)   # during ingestion
    store.save()                        # commits version to DB & activates it

    results = store.search("my query")  # at query time
    """

    # Minimum cosine similarity to include a result.
    # Chunks below this threshold are likely off-topic.
    # 0.35 is permissive enough for paraphrased/indirect queries while
    # still filtering out genuinely unrelated content.
    MIN_SCORE: float = 0.35
    def __init__(
        self,
        folder_id: str,
        embed_model: Optional[str] = None,
        chunk_max_chars: int = 1500,
        chunk_overlap: int = 150,
        version: Optional[IndexVersion] = None,
    ):
        self.folder_id = folder_id
        self.embed_model = embed_model or settings.OPENAI_EMBED_MODEL
        self.dim = _embed_dim_for_model(self.embed_model)
        self.chunk_max_chars = chunk_max_chars
        self.chunk_overlap = chunk_overlap

        self.client = OpenAI()
        self._lock = threading.Lock()
        self._embedding_cache: Dict[str, np.ndarray] = {}
        self._cache_max_size = 1000

        # In-memory FAISS index (populated lazily for search)
        self._faiss_index: Optional[faiss.Index] = None
        self._faiss_chunk_ids: List[int] = []   # DB pk → position in FAISS
        # FIX: was a single version pk; now a "drive_pk:web_pk" string so that
        # a web-only update correctly triggers a rebuild.
        self._faiss_loaded_cache_key: Optional[str] = None

        # Staging buffers used during ingestion (before save())
        self._pending_version: Optional[IndexVersion] = None
        self._pending_chunks: List[dict] = []

        if version is not None:
            self._pending_version = version

    # ------------------------------------------------------------------
    # ingestion
    # ------------------------------------------------------------------

    def begin_version(self, folder_name: str = "") -> IndexVersion:
        """Create a new IndexVersion row. Call once before add_texts()."""
        version_number = IndexVersion.next_version_number(self.folder_id)
        version = IndexVersion.objects.create(
            version_number=version_number,
            folder_id=self.folder_id,
            folder_name=folder_name,
            status=IndexVersion.Status.RUNNING,
            embed_model=self.embed_model,
            chunk_max_chars=self.chunk_max_chars,
            chunk_overlap=self.chunk_overlap,
        )
        self._pending_version = version
        print(f"Started IndexVersion v{version_number} (pk={version.pk}) for folder {self.folder_id}")
        return version

    def add_texts(self, texts: List[str], metadatas: List[Dict]):
        """
        Embed texts and stage them for bulk insertion.

        metadatas must contain: source_id, source_name, mime, chunk, text.
        Optional keys: source_url, source_type (defaults: "", "drive").

        The raw texts are cleaned before embedding (markdown tables → prose)
        but the original text is stored in DocumentChunk.text so the LLM
        sees properly formatted content.
        """
        if not texts:
            return
        if self._pending_version is None:
            raise RuntimeError("Call begin_version() before add_texts()")

        print(f"Embedding {len(texts)} chunks...")

        # Clean for embedding only; originals stored for display
        texts_for_embedding = [clean_chunk_for_embedding(t) for t in texts]
        vectors = self.embed(texts_for_embedding)

        for text, meta, vec in zip(texts, metadatas, vectors):
            self._pending_chunks.append({
                "drive_file_id":   meta["source_id"],
                "drive_file_name": meta["source_name"],
                "mime_type":       meta["mime"],
                "source_url":      meta.get("source_url", ""),
                "source_type":     meta.get("source_type", "drive"),
                "parent_url":      meta.get("parent_url"),   # None for Drive & root pages
                "chunk_index":     meta["chunk"],
                "text":            text,      # original preserved for LLM
                "embedding":       vec,       # from cleaned text
            })

    def save(self, activate: bool = True):
        """
        Bulk-write all staged chunks to the database.
        If activate=True, marks this version as the active one for its folder.
        """
        if self._pending_version is None:
            raise RuntimeError("Nothing to save – call begin_version() first.")

        version = self._pending_version
        chunks  = self._pending_chunks

        print(f"Writing {len(chunks)} chunks to database for version v{version.version_number}…")

        with transaction.atomic():
            docs_map: Dict[str, SourceDocument] = {}
            doc_chunk_counts: Dict[str, int] = {}

            # ── Pass 1: create all SourceDocument rows without parent set yet ──
            # We need every doc to exist before we can resolve parent FKs,
            # because a parent page may appear later in the chunk list than
            # one of its children (BFS order isn't guaranteed end-to-end).
            for c in chunks:
                fid = c["drive_file_id"]
                if fid not in docs_map:
                    doc = SourceDocument.objects.create(
                        version=version,
                        drive_file_id=fid[:2048],
                        drive_file_name=c["drive_file_name"][:512],
                        mime_type=c["mime_type"],
                        source_url=c.get("source_url", ""),
                        source_type=c.get("source_type", "drive"),
                        parent=None,   # resolved in Pass 2
                    )
                    docs_map[fid] = doc
                    doc_chunk_counts[fid] = 0
                doc_chunk_counts[fid] += 1

            # ── Pass 2: resolve parent_url → SourceDocument FK ────────────────
            # Build a lookup from source_url → SourceDocument for web docs only.
            # Drive docs always have parent=None (they live in a flat folder).
            url_to_doc: Dict[str, SourceDocument] = {
                doc.source_url: doc
                for doc in docs_map.values()
                if doc.source_url
            }
            # Collect docs that need their parent set (avoid redundant UPDATE calls)
            need_parent_update: list[SourceDocument] = []
            # Track which parent_url each doc needs (keyed by drive_file_id / URL)
            parent_url_map: Dict[str, str | None] = {}
            for c in chunks:
                fid = c["drive_file_id"]
                if fid not in parent_url_map:
                    parent_url_map[fid] = c.get("parent_url")

            for fid, parent_url in parent_url_map.items():
                if not parent_url:
                    continue  # root page or Drive doc — leave parent=None
                parent_doc = url_to_doc.get(parent_url)
                if parent_doc is None:
                    # Parent URL was skipped (e.g. empty content) — leave as root
                    logger.warning(
                        f"[{self.folder_id}] parent_url {parent_url!r} not found "
                        f"in this version's docs — treating {fid!r} as root."
                    )
                    continue
                doc = docs_map[fid]
                doc.parent = parent_doc
                need_parent_update.append(doc)

            if need_parent_update:
                SourceDocument.objects.bulk_update(need_parent_update, ["parent"], batch_size=500)
                print(f"  🌳 Parent links set for {len(need_parent_update)} web page(s).")

            db_chunks = [
                DocumentChunk(
                    version=version,
                    document=docs_map[c["drive_file_id"]],
                    chunk_index=c["chunk_index"],
                    text=c["text"],
                    embedding=_vec_to_bytes(c["embedding"]),
                )
                for c in chunks
            ]
            DocumentChunk.objects.bulk_create(db_chunks, batch_size=500)

            for fid, doc in docs_map.items():
                doc.chunk_count = doc_chunk_counts[fid]
                doc.save(update_fields=["chunk_count"])

            version.status          = IndexVersion.Status.COMPLETED
            version.files_processed = len(docs_map)
            version.chunks_indexed  = len(chunks)
            version.completed_at    = timezone.now()
            version.save()

            if activate:
                version.activate()

        print(f"✅ Saved version v{version.version_number}: "
              f"{version.files_processed} docs, {version.chunks_indexed} chunks.")

        self._pending_chunks = []
        # Invalidate FAISS cache so next search reloads from DB
        self._faiss_index = None
        self._faiss_loaded_cache_key = None

    def mark_failed(self, error: str):
        """Mark the current pending version as failed."""
        if self._pending_version:
            self._pending_version.status = IndexVersion.Status.FAILED
            self._pending_version.error_message = error
            self._pending_version.save(update_fields=["status", "error_message"])

    # ------------------------------------------------------------------
    # embedding
    # ------------------------------------------------------------------

    def embed(self, texts: List[str]) -> np.ndarray:
        """Return L2-normalised float32 embeddings, with caching and batching."""
        if not texts:
            return np.array([]).reshape(0, self.dim)

        batch_size = 100
        all_vectors: List[np.ndarray] = []

        for i in range(0, len(texts), batch_size):
            batch = texts[i:i + batch_size]
            batch_vectors = np.zeros((len(batch), self.dim), dtype="float32")
            uncached_texts: List[str] = []
            uncached_positions: List[int] = []

            for j, text in enumerate(batch):
                h = str(hash(text))
                if h in self._embedding_cache:
                    batch_vectors[j] = self._embedding_cache[h]
                else:
                    uncached_texts.append(text)
                    uncached_positions.append(j)

            if uncached_texts:
                resp = self.client.embeddings.create(
                    model=self.embed_model, input=uncached_texts
                )
                new_vecs = np.array([d.embedding for d in resp.data], dtype="float32")
                faiss.normalize_L2(new_vecs)

                for pos, vec in zip(uncached_positions, new_vecs):
                    batch_vectors[pos] = vec
                    if len(self._embedding_cache) < self._cache_max_size:
                        self._embedding_cache[str(hash(batch[pos]))] = vec.copy()

                del resp, new_vecs

            all_vectors.append(batch_vectors)

            if i % (batch_size * 5) == 0:
                gc.collect()

        return np.vstack(all_vectors) if all_vectors else np.array([]).reshape(0, self.dim)

    # ------------------------------------------------------------------
    # search
    # ------------------------------------------------------------------

    def search(
        self,
        query: str,
        k: int = 5,
        version_id: Optional[int] = None,
    ) -> List[Tuple[float, Dict]]:
        """
        Semantic search over the active version(s).

        Returns list of (score, metadata_dict) sorted descending by score.
        Results below MIN_SCORE are filtered out.
        """
        t0 = time.time()

        if _use_pgvector():
            results = self._search_pgvector(query, k, version_id)
        else:
            results = self._search_faiss(query, k, version_id)

        results = [(score, meta) for score, meta in results if score >= self.MIN_SCORE]

        # Fallback: if nothing passes threshold, widen to top-3 results
        # regardless of score so the LLM always has something to work with.
        # The prompt instructs it to say "not available" only when truly irrelevant.
        if not results:
            if _use_pgvector():
                raw = self._search_pgvector(query, 3, version_id)
            else:
                raw = self._search_faiss(query, 3, version_id)
            results = raw[:3]
            if results:
                print(f"search: threshold fallback triggered, best score={results[0][0]:.3f}")

        elapsed = time.time() - t0
        print(f"search({query!r:.50}, k={k}) → {len(results)} results in {elapsed:.3f}s")
        return results

    def _get_target_version(self, version_id: Optional[int]) -> Optional[IndexVersion]:
        if version_id is not None:
            return IndexVersion.objects.filter(pk=version_id).first()
        return IndexVersion.objects.filter(
            folder_id=self.folder_id, is_active=True
        ).first()

    def _get_target_version_web(self) -> Optional[IndexVersion]:
        """
        Find the active web IndexVersion.
        Web versions are stored with a folder_id starting with 'web:'
        (e.g. 'web:nsw-regs-0653') — never a Drive folder UUID.
        Using folder_name=SCRAPING_URL was unreliable because folder_name
        stores a human label, not the raw URL.
        """
        return IndexVersion.objects.filter(
            folder_name=settings.SCRAPING_URL,
            is_active=True,
        ).order_by("-created_at").first()

    def _search_faiss(
        self,
        query: str,
        k: int,
        version_id: Optional[int],
    ) -> List[Tuple[float, Dict]]:
        """Load embeddings into a local FAISS index, then search."""
        version     = self._get_target_version(version_id)
        version_web = self._get_target_version_web()
        #version_web = self._get_target_version(version_id)
        #version = self._get_target_version_web()
        if version is None and version_web is None:
            print("No active version found.")
            return []

        cache_key = _versions_cache_key(version, version_web)
        
        with self._lock:
            # FIX: previously compared only the Drive version pk, so a
            # web-only update never triggered a FAISS rebuild.  Now we use a
            # combined "drive_pk:web_pk" key so either change forces a reload.
            if self._faiss_loaded_cache_key != cache_key:
                versions_to_load = [v for v in [version, version_web] if v is not None]
                
                self._build_faiss_index(versions_to_load, cache_key)

            if self._faiss_index is None or self._faiss_index.ntotal == 0:
                return []

            qv = self.embed([query])
            D, I = self._faiss_index.search(qv, min(k, self._faiss_index.ntotal))
        canonical = version if version is not None else version_web
        results = []
        for score, idx in zip(D[0], I[0]):
            if idx == -1:
                continue
            chunk_pk = self._faiss_chunk_ids[idx]
            try:
                chunk = DocumentChunk.objects.select_related("document").get(pk=chunk_pk)
                results.append((float(score), {
                    "chunk_pk":    chunk.pk,
                    "source_id":   chunk.document.drive_file_id,
                    "source_name": chunk.document.drive_file_name,
                    "mime":        chunk.document.mime_type,
                    "source_url":  chunk.document.source_url,
                    "source_type": chunk.document.source_type,
                    "chunk":       chunk.chunk_index,
                    "text":        chunk.text,
                    "version":     canonical.version_number,
                }))
            except DocumentChunk.DoesNotExist:
                pass
        return results

    def _build_faiss_index(self, versions: List[IndexVersion], cache_key: str):
        """
        (Re-)build the in-memory FAISS index from DB rows for one or more
        versions.  Accepts a list so Drive + Web chunks are merged into a
        single index.
        """
        version_nums = [v.version_number for v in versions]
        version_pks  = [v.pk for v in versions]
        print(f"Loading embeddings for versions {version_nums} into FAISS… pks={version_pks}")

        chunks = list(
            DocumentChunk.objects
            .filter(version__in=version_pks)
            .order_by("pk")
            .only("pk", "embedding")
        )
        
        
        if not chunks:
            self._faiss_index = faiss.IndexFlatIP(self.dim)
            self._faiss_chunk_ids = []
            self._faiss_loaded_cache_key = cache_key
            return

        vectors = np.vstack([
            _bytes_to_vec(bytes(c.embedding), self.dim) for c in chunks
        ]).astype("float32")
        
        index = faiss.IndexFlatIP(self.dim)
        index.add(vectors)

        self._faiss_index = index
        self._faiss_chunk_ids = [c.pk for c in chunks]
        self._faiss_loaded_cache_key = cache_key
        print(f"FAISS index ready: {index.ntotal} vectors across {len(versions)} version(s)")
        del vectors
        gc.collect()

    def _search_pgvector(
        self,
        query: str,
        k: int,
        version_id: Optional[int],
    ) -> List[Tuple[float, Dict]]:
        """
        Uses pgvector's <=> operator for cosine distance directly in SQL.
        Requires:  pip install pgvector django-pgvector
        And the DocumentChunk.embedding field swapped for pgvector.VectorField.
        """
        from pgvector.django import CosineDistance  # type: ignore

        version     = self._get_target_version(version_id)
        version_web = self._get_target_version_web()

        if version is None and version_web is None:
            return []

        active_versions = [v for v in [version, version_web] if v is not None]
        canonical = version if version is not None else version_web
        qv = self.embed([query])[0].tolist()

        qs = (
            DocumentChunk.objects
            .filter(version__in=active_versions)
            .select_related("document")
            .annotate(distance=CosineDistance("embedding", qv))
            .order_by("distance")[:k]
        )

        results = []
        for chunk in qs:
            score = 1.0 - float(chunk.distance)
            results.append((score, {
                "chunk_pk":    chunk.pk,
                "source_id":   chunk.document.drive_file_id,
                "source_name": chunk.document.drive_file_name,
                "mime":        chunk.document.mime_type,
                "source_url":  chunk.document.source_url,
                "source_type": chunk.document.source_type,
                "chunk":       chunk.chunk_index,
                "text":        chunk.text,
                "version":     canonical.version_number,
            }))
        return results

    # ------------------------------------------------------------------
    # surrounding chunk fetch
    # ------------------------------------------------------------------

    def fetch_surrounding_chunks(
        self,
        chunk_pk: int,
        window: int = 1,
    ) -> List[Dict]:
        """
        Return chunk_pk plus `window` neighbours on each side from the same
        source document, in sequential order.

        Gives the LLM context immediately before/after the matched passage
        without loading the full document.

        Returns list of dicts with keys:
            chunk_pk, source_name, source_url, source_type,
            chunk_index, text, is_anchor
        """
        try:
            anchor = DocumentChunk.objects.select_related("document").get(pk=chunk_pk)
        except DocumentChunk.DoesNotExist:
            return []

        lo = max(0, anchor.chunk_index - window)
        hi = anchor.chunk_index + window

        neighbours = (
            DocumentChunk.objects
            .filter(
                document=anchor.document,
                chunk_index__gte=lo,
                chunk_index__lte=hi,
            )
            .select_related("document")
            .order_by("chunk_index")
        )

        return [
            {
                "chunk_pk":    c.pk,
                "source_name": c.document.drive_file_name,
                "source_url":  c.document.source_url,
                "source_type": c.document.source_type,
                "chunk_index": c.chunk_index,
                "text":        c.text,
                "is_anchor":   c.pk == chunk_pk,
            }
            for c in neighbours
        ]

    # ------------------------------------------------------------------
    # stats / utils
    # ------------------------------------------------------------------

    def get_stats(self) -> Dict:
        active = IndexVersion.objects.filter(
            folder_id=self.folder_id, is_active=True
        ).first()
        return {
            "folder_id":         self.folder_id,
            "active_version":    active.version_number if active else None,
            "active_version_pk": active.pk if active else None,
            "total_chunks":      active.chunks_indexed if active else 0,
            "embed_model":       self.embed_model,
            "backend":           "pgvector" if _use_pgvector() else "faiss-in-memory",
        }

    def list_versions(self) -> List[Dict]:
        return list(
            IndexVersion.objects
            .filter(folder_id=self.folder_id)
            .values(
                "pk", "version_number", "status", "is_active",
                "files_processed", "chunks_indexed",
                "created_at", "completed_at",
            )
            .order_by("-version_number")
        )

    def rollback_to_version(self, version_number: int) -> IndexVersion:
        """Activate an older version by its version_number."""
        version = IndexVersion.objects.get(
            folder_id=self.folder_id,
            version_number=version_number,
            status=IndexVersion.Status.COMPLETED,
        )
        version.activate()
        self._faiss_index = None
        self._faiss_loaded_cache_key = None
        print(f"Rolled back to version v{version_number}")
        return version


# ---------------------------------------------------------------------------
# singleton factory
# ---------------------------------------------------------------------------

_db_store_cache: Dict[str, DBVectorStore] = {}
_db_store_lock = threading.Lock()


def get_db_store(folder_id: Optional[str] = None) -> DBVectorStore:
    """
    Return a thread-safe singleton DBVectorStore for the given folder_id.
    Falls back to settings.GDRIVE_DEFAULT_FOLDER_ID if not specified.
    """
    fid = folder_id or getattr(settings, "GDRIVE_DEFAULT_FOLDER_ID", "default")
    global _db_store_cache
    if fid not in _db_store_cache:
        with _db_store_lock:
            if fid not in _db_store_cache:
                _db_store_cache[fid] = DBVectorStore(
                    folder_id=fid,
                    embed_model=settings.OPENAI_EMBED_MODEL,
                )
    return _db_store_cache[fid]


def invalidate_db_store_cache(folder_id: Optional[str] = None):
    """Call after activating a new version so searches reload from DB."""
    global _db_store_cache
    with _db_store_lock:
        if folder_id:
            _db_store_cache.pop(folder_id, None)
        else:
            _db_store_cache.clear()
    gc.collect()