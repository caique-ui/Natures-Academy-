from __future__ import annotations

import gc
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
        self._faiss_loaded_version_id: Optional[int] = None

        # Staging buffers used during ingestion (before save())
        self._pending_version: Optional[IndexVersion] = None
        self._pending_chunks: List[dict] = []   # {"source_doc": ..., "chunk_index": ..., "text": ..., "embedding": ...}

        if version is not None:
            # Attach to an existing version (e.g. for read-only search)
            self._pending_version = version

    # ------------------------------------------------------------------
    # ingestion
    # ------------------------------------------------------------------

    def begin_version(self, folder_name: str = "") -> IndexVersion:
        """
        Create a new IndexVersion row and return it.
        Call this once before add_texts().
        """
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
        """
        if not texts:
            return
        if self._pending_version is None:
            raise RuntimeError("Call begin_version() before add_texts()")

        print(f"Embedding {len(texts)} chunks...")
        vectors = self.embed(texts)

        for text, meta, vec in zip(texts, metadatas, vectors):
            self._pending_chunks.append({
                "drive_file_id":   meta["source_id"],
                "drive_file_name": meta["source_name"],
                "mime_type":       meta["mime"],
                "chunk_index":     meta["chunk"],
                "text":            text,
                "embedding":       vec,
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
            # Group chunks by source document
            docs_map: Dict[str, SourceDocument] = {}
            doc_chunk_counts: Dict[str, int] = {}

            for c in chunks:
                fid = c["drive_file_id"]
                if fid not in docs_map:
                    doc = SourceDocument.objects.create(
                        version=version,
                        drive_file_id=fid,
                        drive_file_name=c["drive_file_name"],
                        mime_type=c["mime_type"],
                    )
                    docs_map[fid] = doc
                    doc_chunk_counts[fid] = 0
                doc_chunk_counts[fid] += 1

            # Bulk-create DocumentChunk rows
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

            # Update char/chunk counts on SourceDocument rows
            for fid, doc in docs_map.items():
                doc.chunk_count = doc_chunk_counts[fid]
                doc.save(update_fields=["chunk_count"])

            # Finalise the version record
            version.status         = IndexVersion.Status.COMPLETED
            version.files_processed = len(docs_map)
            version.chunks_indexed  = len(chunks)
            version.completed_at    = timezone.now()
            version.save()

            if activate:
                version.activate()

        print(f"✅ Saved version v{version.version_number}: "
              f"{version.files_processed} docs, {version.chunks_indexed} chunks.")

        # Reset staging buffers
        self._pending_chunks = []
        # Invalidate cached FAISS index so next search reloads from DB
        self._faiss_index = None

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
        Semantic search over the active version (or the version specified by
        version_id).

        Returns list of (score, metadata_dict) sorted descending by score.
        """
        t0 = time.time()

        if _use_pgvector():
            return self._search_pgvector(query, k, version_id)
        else:
            return self._search_faiss(query, k, version_id)

    def _get_target_version(self, version_id: Optional[int]) -> Optional[IndexVersion]:
        if version_id is not None:
            return IndexVersion.objects.filter(pk=version_id).first()
        return IndexVersion.objects.filter(
            folder_id=self.folder_id, is_active=True
        ).first()

    def _search_faiss(
        self,
        query: str,
        k: int,
        version_id: Optional[int],
    ) -> List[Tuple[float, Dict]]:
        """Load embeddings into a local FAISS index, then search."""
        version = self._get_target_version(version_id)
        if version is None:
            print("No active version found.")
            return []

        with self._lock:
            # Rebuild FAISS index if the active version changed
            if self._faiss_loaded_version_id != version.pk:
                self._build_faiss_index(version)

            if self._faiss_index is None or self._faiss_index.ntotal == 0:
                return []

            qv = self.embed([query])
            D, I = self._faiss_index.search(qv, min(k, self._faiss_index.ntotal))

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
                    "chunk":       chunk.chunk_index,
                    "text":        chunk.text,
                    "version":     version.version_number,
                }))
            except DocumentChunk.DoesNotExist:
                pass

        return results

    def _build_faiss_index(self, version: IndexVersion):
        """(Re-)build the in-memory FAISS index from DB rows for a version."""
        print(f"Loading embeddings for version v{version.version_number} into FAISS…")
        chunks = list(
            DocumentChunk.objects.filter(version=version)
            .order_by("pk")
            .only("pk", "embedding")
        )
        if not chunks:
            self._faiss_index = faiss.IndexFlatIP(self.dim)
            self._faiss_chunk_ids = []
            self._faiss_loaded_version_id = version.pk
            return

        vectors = np.vstack([
            _bytes_to_vec(bytes(c.embedding), self.dim) for c in chunks
        ]).astype("float32")

        index = faiss.IndexFlatIP(self.dim)
        index.add(vectors)

        self._faiss_index = index
        self._faiss_chunk_ids = [c.pk for c in chunks]
        self._faiss_loaded_version_id = version.pk
        print(f"FAISS index ready: {index.ntotal} vectors")
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

        version = self._get_target_version(version_id)
        if version is None:
            return []

        qv = self.embed([query])[0].tolist()

        qs = (
            DocumentChunk.objects
            .filter(version=version)
            .select_related("document")
            .annotate(distance=CosineDistance("embedding", qv))
            .order_by("distance")[:k]
        )

        results = []
        for chunk in qs:
            score = 1.0 - float(chunk.distance)  # cosine similarity
            results.append((score, {
                "chunk_pk":    chunk.pk,
                "source_id":   chunk.document.drive_file_id,
                "source_name": chunk.document.drive_file_name,
                "mime":        chunk.document.mime_type,
                "chunk":       chunk.chunk_index,
                "text":        chunk.text,
                "version":     version.version_number,
            }))
        return results

    # ------------------------------------------------------------------
    # stats / utils
    # ------------------------------------------------------------------

    def get_stats(self) -> Dict:
        active = IndexVersion.objects.filter(
            folder_id=self.folder_id, is_active=True
        ).first()
        return {
            "folder_id":      self.folder_id,
            "active_version": active.version_number if active else None,
            "active_version_pk": active.pk if active else None,
            "total_chunks":   active.chunks_indexed if active else 0,
            "embed_model":    self.embed_model,
            "backend":        "pgvector" if _use_pgvector() else "faiss-in-memory",
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
        # Invalidate FAISS cache
        self._faiss_index = None
        print(f"Rolled back to version v{version_number}")
        return version


# ---------------------------------------------------------------------------
# singleton factory (mirrors get_store() from vectorstore.py)
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
