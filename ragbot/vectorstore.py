# chat/vectorstore.py — FAISS index + JSONL docstore with memory optimization
import os
import json
import faiss
import numpy as np
from typing import List, Dict, Tuple
from openai import OpenAI
from django.conf import settings
import gc

class VectorStore:
    def __init__(self, index_path: str, docstore_path: str, embed_model: str):
        self.index_path = index_path
        self.docstore_path = docstore_path
        self.embed_model = embed_model
        self.client = OpenAI()
        self.dim = 1536  # text-embedding-3-small
        self.index = None
        self.metadatas: List[Dict] = []

        os.makedirs(os.path.dirname(index_path), exist_ok=True)
        os.makedirs(os.path.dirname(docstore_path), exist_ok=True)

        self._load()

    def _load(self):
        if os.path.exists(self.index_path) and os.path.exists(self.docstore_path):
            try:
                self.index = faiss.read_index(self.index_path)
                # load metadatas in order
                self.metadatas = []
                with open(self.docstore_path, "r", encoding="utf-8") as f:
                    for line in f:
                        line = line.strip()
                        if line:
                            self.metadatas.append(json.loads(line))
                print(f"Loaded existing index with {self.index.ntotal} vectors and {len(self.metadatas)} metadata entries")
            except Exception as e:
                print(f"Error loading existing index: {e}")
                print("Creating new index...")
                self.index = faiss.IndexFlatIP(self.dim)
                self.metadatas = []
        else:
            print("Creating new index...")
            self.index = faiss.IndexFlatIP(self.dim)
            self.metadatas = []

    def save(self):
        try:
            faiss.write_index(self.index, self.index_path)
            with open(self.docstore_path, "w", encoding="utf-8") as f:
                for m in self.metadatas:
                    f.write(json.dumps(m, ensure_ascii=False) + "\n")
            print(f"Saved index with {self.index.ntotal} vectors")
        except Exception as e:
            print(f"Error saving index: {e}")
            raise

    def embed(self, texts: List[str]) -> np.ndarray:
        # Process embeddings in smaller batches to avoid memory issues
        batch_size = 20  # Reduce batch size for embedding requests
        all_vectors = []
        
        for i in range(0, len(texts), batch_size):
            batch_texts = texts[i:i + batch_size]
            try:
                resp = self.client.embeddings.create(model=self.embed_model, input=batch_texts)
                vectors = np.array([d.embedding for d in resp.data], dtype="float32")
                # Normalize for cosine similarity via Inner Product
                faiss.normalize_L2(vectors)
                all_vectors.append(vectors)
                
                # Force garbage collection between batches
                del resp
                gc.collect()
                
            except Exception as e:
                print(f"Error creating embeddings for batch {i//batch_size + 1}: {e}")
                raise
        
        if all_vectors:
            return np.vstack(all_vectors)
        else:
            return np.array([]).reshape(0, self.dim)

    def add_texts(self, texts: List[str], metadatas: List[Dict]):
        if not texts:
            return
            
        print(f"Creating embeddings for {len(texts)} texts...")
        try:
            vectors = self.embed(texts)
            print(f"Adding {len(vectors)} vectors to index...")
            self.index.add(vectors)
            self.metadatas.extend(metadatas)
            
            # Clean up memory
            del vectors
            gc.collect()
            
        except Exception as e:
            print(f"Error adding texts to index: {e}")
            raise

    def search(self, query: str, k: int = 5) -> List[Tuple[float, Dict]]:
        if self.index.ntotal == 0:
            return []
            
        try:
            qv = self.embed([query])
            D, I = self.index.search(qv, min(k, self.index.ntotal))
            results = []
            for score, idx in zip(D[0], I[0]):
                if idx == -1 or idx >= len(self.metadatas):
                    continue
                meta = self.metadatas[idx]
                results.append((float(score), meta))
            return results
        except Exception as e:
            print(f"Error searching: {e}")
            return []

# Helper to get a singleton store per process
_store_cache = None

def get_store() -> VectorStore:
    global _store_cache
    if _store_cache is None:
        _store_cache = VectorStore(
            index_path=settings.VECTORSTORE_PATH,
            docstore_path=settings.DOCSTORE_PATH,
            embed_model=settings.OPENAI_EMBED_MODEL,
        )
    return _store_cache

def clear_store_cache():
    """Clear the store cache to free memory"""
    global _store_cache
    _store_cache = None
    gc.collect()