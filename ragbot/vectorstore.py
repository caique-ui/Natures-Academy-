import os
import json
import faiss
import numpy as np
from typing import List, Dict, Tuple, Optional
from openai import OpenAI
from django.conf import settings
import gc
import threading
import time
from functools import lru_cache

class VectorStore:
    def __init__(self, index_path: str, docstore_path: str, embed_model: str):
        self.index_path = index_path
        self.docstore_path = docstore_path
        self.embed_model = embed_model
        
        self.client = OpenAI()
        
        if embed_model == "text-embedding-3-small":
            self.dim = 1536
        elif embed_model == "text-embedding-ada-002":
            self.dim = 1536
        else:
            self.dim = 1536  # default
            
        self.index = None
        self.metadatas: List[Dict] = []
        self._lock = threading.Lock()
        self._is_trained = False
        
        # Cache for recent embeddings
        self._embedding_cache = {}
        self._cache_max_size = 1000
        
        os.makedirs(os.path.dirname(index_path), exist_ok=True)
        os.makedirs(os.path.dirname(docstore_path), exist_ok=True)

        self._load()

    def _load(self):
        """Load existing index and metadata with better error handling"""
        if os.path.exists(self.index_path) and os.path.exists(self.docstore_path):
            try:
                self.index = faiss.read_index(self.index_path)
                
                # Check if it's trained (for IVF indices)
                self._is_trained = getattr(self.index, 'is_trained', True)
                
                self.metadatas = []
                with open(self.docstore_path, "r", encoding="utf-8") as f:
                    content = f.read().strip()
                    if content:
                        lines = content.split('\n')
                        for line in lines:
                            if line.strip():
                                self.metadatas.append(json.loads(line))
                
                print(f"Loaded existing index with {self.index.ntotal} vectors and {len(self.metadatas)} metadata entries")
                print(f"Index type: {type(self.index).__name__}")
                print(f"Index trained: {self._is_trained}")
                
                # Set search parameters for IVF
                if hasattr(self.index, 'nprobe'):
                    self.index.nprobe = min(10, getattr(self.index, 'nlist', 10))
                    
            except Exception as e:
                print(f"Error loading existing index: {e}")
                print("Creating new index...")
                self._create_new_index()
        else:
            print("Creating new index...")
            self._create_new_index()

    def _create_new_index(self):
        """Create a new IVF index - always use IVF for production readiness"""
        nlist = 10  # Start with minimum clusters for small datasets
        quantizer = faiss.IndexFlatIP(self.dim)
        self.index = faiss.IndexIVFFlat(quantizer, self.dim, nlist)
        self.metadatas = []
        self._is_trained = False
        print(f"Created new IVF index with {nlist} clusters")

    def save(self):
        """Save index and metadata with better error handling"""
        with self._lock:
            try:
                if os.path.exists(self.index_path):
                    backup_path = self.index_path + ".backup"
                    os.rename(self.index_path, backup_path)
                
                faiss.write_index(self.index, self.index_path)
                
                with open(self.docstore_path, "w", encoding="utf-8") as f:
                    for m in self.metadatas:
                        f.write(json.dumps(m, ensure_ascii=False) + "\n")
                
                print(f"Saved index with {self.index.ntotal} vectors")
                
                backup_path = self.index_path + ".backup"
                if os.path.exists(backup_path):
                    os.remove(backup_path)
                    
            except Exception as e:
                print(f"Error saving index: {e}")
                backup_path = self.index_path + ".backup"
                if os.path.exists(backup_path):
                    os.rename(backup_path, self.index_path)
                raise

    def embed(self, texts: List[str]) -> np.ndarray:
        """Optimized embedding with caching and batching"""
        if not texts:
            return np.array([]).reshape(0, self.dim)
        
        batch_size = 100
        all_vectors = []
        
        for i in range(0, len(texts), batch_size):
            batch_texts = texts[i:i + batch_size]
            
            # Check cache first
            cached_vectors = []
            uncached_texts = []
            uncached_indices = []
            
            for j, text in enumerate(batch_texts):
                text_hash = str(hash(text))
                if text_hash in self._embedding_cache:
                    cached_vectors.append((i + j, self._embedding_cache[text_hash]))
                else:
                    uncached_texts.append(text)
                    uncached_indices.append(i + j)
            
            # Get embeddings for uncached texts
            batch_vectors = np.zeros((len(batch_texts), self.dim), dtype="float32")
            
            # Fill in cached vectors
            for idx, vector in cached_vectors:
                local_idx = idx - i
                batch_vectors[local_idx] = vector
            
            # Get uncached embeddings
            if uncached_texts:
                try:
                    resp = self.client.embeddings.create(model=self.embed_model, input=uncached_texts)
                    new_vectors = np.array([d.embedding for d in resp.data], dtype="float32")
                    faiss.normalize_L2(new_vectors)
                    
                    # Fill in new vectors and cache them
                    for j, (global_idx, vector) in enumerate(zip(uncached_indices, new_vectors)):
                        local_idx = global_idx - i
                        batch_vectors[local_idx] = vector
                        
                        # Cache the embedding
                        text_hash = str(hash(uncached_texts[j]))
                        if len(self._embedding_cache) < self._cache_max_size:
                            self._embedding_cache[text_hash] = vector.copy()
                    
                    del resp, new_vectors
                    
                except Exception as e:
                    print(f"Error creating embeddings for batch {i//batch_size + 1}: {e}")
                    raise
            
            all_vectors.append(batch_vectors)
            
            if i % (batch_size * 5) == 0:
                gc.collect()
        
        if all_vectors:
            result = np.vstack(all_vectors)
            return result
        else:
            return np.array([]).reshape(0, self.dim)

    def _train_index_if_needed(self, vectors: np.ndarray):
        """Train IVF index if not already trained"""
        if self._is_trained or not hasattr(self.index, 'is_trained'):
            return
            
        if vectors.shape[0] < 10:
            print(f"Need more vectors to train IVF index (have {vectors.shape[0]}, need 10+)")
            return
            
        try:
            print(f"Training IVF index with {vectors.shape[0]} vectors...")
            self.index.train(vectors)
            self._is_trained = True
            
            if hasattr(self.index, 'nprobe'):
                self.index.nprobe = min(5, getattr(self.index, 'nlist', 5))
            
            print("IVF index training completed!")
            
        except Exception as e:
            print(f"Error training IVF index: {e}")
            if vectors.shape[0] < 50:
                print("Recreating index with fewer clusters for small dataset...")
                try:
                    nlist = max(2, vectors.shape[0] // 5)
                    quantizer = faiss.IndexFlatIP(self.dim)
                    new_index = faiss.IndexIVFFlat(quantizer, self.dim, nlist)
                    new_index.train(vectors)
                    
                    if self.index.ntotal > 0:
                        existing_vectors = np.zeros((self.index.ntotal, self.dim), dtype='float32')
                        for i in range(self.index.ntotal):
                            existing_vectors[i] = self.index.reconstruct(i)
                        new_index.add(existing_vectors)
                    
                    self.index = new_index
                    self._is_trained = True
                    print(f"Recreated IVF index with {nlist} clusters")
                    
                except Exception as e2:
                    print(f"Failed to recreate index: {e2}")
                    raise

    def add_texts(self, texts: List[str], metadatas: List[Dict]):
        """Add texts to IVF index with training as needed"""
        if not texts:
            return
        
        with self._lock:
            print(f"Creating embeddings for {len(texts)} texts...")
            
            chunk_size = 1000
            all_vectors_for_training = []
            
            for i in range(0, len(texts), chunk_size):
                chunk_texts = texts[i:i + chunk_size]
                chunk_metadatas = metadatas[i:i + chunk_size]
                
                try:
                    vectors = self.embed(chunk_texts)
                    
                    # Collect vectors for training if needed
                    if not self._is_trained:
                        all_vectors_for_training.append(vectors.copy())
                    
                    # Train index if we have enough vectors and not yet trained
                    if not self._is_trained and len(all_vectors_for_training) > 0:
                        all_training_vectors = np.vstack(all_vectors_for_training)
                        if all_training_vectors.shape[0] >= 10:
                            self._train_index_if_needed(all_training_vectors)
                    
                    # Add vectors to index (only if trained)
                    if self._is_trained:
                        print(f"Adding {len(vectors)} vectors to index (chunk {i//chunk_size + 1})...")
                        self.index.add(vectors)
                        self.metadatas.extend(chunk_metadatas)
                    else:
                        print(f"Waiting for more vectors to train index (chunk {i//chunk_size + 1})...")
                        self.metadatas.extend(chunk_metadatas)
                    
                    del vectors
                    gc.collect()
                    
                except Exception as e:
                    print(f"Error adding texts chunk {i//chunk_size + 1}: {e}")
                    raise
            
            # If we just trained, add all the vectors we collected
            if self._is_trained and all_vectors_for_training:
                all_vectors = np.vstack(all_vectors_for_training)
                if self.index.ntotal == 0:
                    print(f"Adding {len(all_vectors)} training vectors to newly trained index...")
                    self.index.add(all_vectors)
            
            print(f"Total vectors in index: {self.index.ntotal}")
            print(f"Index trained: {self._is_trained}")

    '''def search(self, query: str, k: int = 5) -> List[Tuple[float, Dict]]:
        """Fast search without debug output"""
        if self.index.ntotal == 0:
            return []
            
        if not self._is_trained:
            return []
        
        # Set IVF search parameters
        if hasattr(self.index, 'nprobe'):
            self.index.nprobe = min(3, getattr(self.index, 'nlist', 10))  # Lower nprobe for speed
        
        try:
            # Use cached embedding if available
            query_hash = str(hash(query))
            if query_hash in self._embedding_cache:
                qv = self._embedding_cache[query_hash].reshape(1, -1)
            else:
                qv = self.embed([query])
                if len(self._embedding_cache) < self._cache_max_size:
                    self._embedding_cache[query_hash] = qv[0].copy()
            
            search_k = min(k, self.index.ntotal)
            D, I = self.index.search(qv, search_k)
            
            results = []
            for score, idx in zip(D[0], I[0]):
                if idx == -1 or idx >= len(self.metadatas):
                    continue
                meta = self.metadatas[idx]
                results.append((float(score), meta))
            
            return results
            
        except Exception as e:
            print(f"Error searching: {e}")
            return []'''

    
    def search(self, query: str, k: int = 5) -> List[Tuple[float, Dict]]:
        """Search using IVF index with detailed timing"""
        import time
        
        total_start = time.time()
        print(f"=== SEARCH TIMING DEBUG ===")
        print(f"Query: '{query[:100]}...' (length: {len(query)} chars)")
        
        if self.index.ntotal == 0:
            print("Index is empty")
            return []
            
        if not self._is_trained:
            print("Index not trained yet, cannot search")
            return []
        
        # Set IVF search parameters
        if hasattr(self.index, 'nprobe'):
            nprobe = min(max(1, self.index.nprobe), getattr(self.index, 'nlist', 10))
            self.index.nprobe = nprobe
        
        try:
            # TIMING: Check cache
            cache_start = time.time()
            query_hash = str(hash(query))
            if query_hash in self._embedding_cache:
                qv = self._embedding_cache[query_hash].reshape(1, -1)
                cache_time = time.time() - cache_start
                print(f"✓ Used cached embedding: {cache_time:.3f}s")
            else:
                cache_time = time.time() - cache_start
                print(f"✗ Cache miss: {cache_time:.3f}s")
                
                # TIMING: Create embedding
                embed_start = time.time()
                qv = self.embed([query])
                embed_time = time.time() - embed_start
                print(f"✓ Created embedding: {embed_time:.3f}s")
                
                # Cache it
                if len(self._embedding_cache) < self._cache_max_size:
                    self._embedding_cache[query_hash] = qv[0].copy()
            
            # TIMING: Vector search
            search_start = time.time()
            search_k = min(k, self.index.ntotal)
            D, I = self.index.search(qv, search_k)
            search_time = time.time() - search_start
            print(f"✓ Vector search ({type(self.index).__name__}): {search_time:.3f}s")
            if hasattr(self.index, 'nprobe'):
                print(f"  - nprobe: {self.index.nprobe}")
            
            # TIMING: Build results
            results_start = time.time()
            results = []
            for score, idx in zip(D[0], I[0]):
                if idx == -1 or idx >= len(self.metadatas):
                    continue
                meta = self.metadatas[idx]
                results.append((float(score), meta))
            results_time = time.time() - results_start
            print(f"✓ Build results: {results_time:.3f}s")
            
            total_time = time.time() - total_start
            print(f"✓ TOTAL TIME: {total_time:.3f}s")
            print(f"Found {len(results)} results")
            if results:
                print(f"Top score: {results[0][0]:.4f}")
            print("=== END TIMING DEBUG ===")
            
            return results
            
        except Exception as e:
            total_time = time.time() - total_start
            print(f"✗ ERROR after {total_time:.3f}s: {e}")
            import traceback
            traceback.print_exc()
            return []



    def get_stats(self) -> Dict:
        """Get statistics about the vector store"""
        return {
            "total_vectors": self.index.ntotal if self.index else 0,
            "dimension": self.dim,
            "cache_size": len(self._embedding_cache),
            "index_type": type(self.index).__name__ if self.index else None,
            "is_trained": self._is_trained
        }

# Thread-safe singleton store
_store_cache = None
_store_lock = threading.Lock()

def get_store() -> VectorStore:
    global _store_cache
    if _store_cache is None:
        with _store_lock:
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
    with _store_lock:
        if _store_cache:
            _store_cache._embedding_cache.clear()
        _store_cache = None
    gc.collect()

# Fallback simple version for debugging
class SimpleVectorStore:
    """Minimal changes to your original code - for debugging"""
    def __init__(self, index_path: str, docstore_path: str, embed_model: str):
        self.index_path = index_path
        self.docstore_path = docstore_path
        self.embed_model = embed_model
        self.client = OpenAI()
        self.dim = 1536
        self.index = None
        self.metadatas: List[Dict] = []

        os.makedirs(os.path.dirname(index_path), exist_ok=True)
        os.makedirs(os.path.dirname(docstore_path), exist_ok=True)
        self._load()

    def _load(self):
        if os.path.exists(self.index_path) and os.path.exists(self.docstore_path):
            try:
                self.index = faiss.read_index(self.index_path)
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
        batch_size = 50
        all_vectors = []
        
        for i in range(0, len(texts), batch_size):
            batch_texts = texts[i:i + batch_size]
            try:
                resp = self.client.embeddings.create(model=self.embed_model, input=batch_texts)
                vectors = np.array([d.embedding for d in resp.data], dtype="float32")
                faiss.normalize_L2(vectors)
                all_vectors.append(vectors)
                print(f"Processed batch {i//batch_size + 1}/{(len(texts) + batch_size - 1)//batch_size}")
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
            print(f"Total vectors in index: {self.index.ntotal}")
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

def get_simple_store() -> SimpleVectorStore:
    """Use this for debugging - returns the simpler version"""
    return SimpleVectorStore(
        index_path=settings.VECTORSTORE_PATH,
        docstore_path=settings.DOCSTORE_PATH,
        embed_model=settings.OPENAI_EMBED_MODEL,
    )