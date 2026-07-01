"""ChromaDB repository memory for storing and querying past fixes.

ChromaDB pulls in numpy's native extensions, which can be unavailable in locked-down
environments (e.g. Windows Application Control blocking the DLL). Memory is only an
accelerator, so the import is made optional: if it fails, the whole memory layer
degrades to a no-op and the pipeline runs normally without it.
"""

from __future__ import annotations

try:
    import chromadb
    from chromadb.config import Settings as ChromaSettings
    _CHROMA_IMPORT_ERROR: Exception | None = None
except Exception as _e:  # ImportError, DLL load failure, etc.
    chromadb = None
    ChromaSettings = None
    _CHROMA_IMPORT_ERROR = _e

from config import settings


class RepositoryMemory:
    """Semantic memory of past fixes using ChromaDB."""

    def __init__(self):
        # Memory is an optional accelerator — if ChromaDB can't initialize (e.g. the
        # embedding model can't be downloaded offline, or numpy's DLL is blocked),
        # degrade gracefully instead of crashing the whole pipeline.
        self.collection = None
        self.client = None
        if chromadb is None:
            return
        try:
            self.client = chromadb.PersistentClient(
                path=settings.chroma_db_path,
                settings=ChromaSettings(anonymized_telemetry=False),
            )
            self.collection = self.client.get_or_create_collection(
                name=settings.chroma_collection_name,
                metadata={"hnsw:space": "cosine"},
            )
        except Exception:
            self.client = None

    def store_fix(
        self,
        bug_type: str,
        root_cause: str,
        fix_strategy: str,
        affected_file: str,
        confidence_score: float,
    ) -> None:
        """Store a successfully validated fix (best-effort)."""
        if self.collection is None:
            return
        doc_id = f"{bug_type}:{affected_file}:{hash(root_cause) & 0xFFFFFFFF:08x}"
        embedding_text = f"{bug_type}: {root_cause}"
        try:
            self.collection.add(
                ids=[doc_id],
                documents=[embedding_text],
                metadatas=[{
                    "bug_type": bug_type,
                    "root_cause": root_cause,
                    "fix_strategy": fix_strategy,
                    "affected_file": affected_file,
                    "confidence_score": confidence_score,
                }],
            )
        except Exception:
            pass

    def query_similar(
        self, bug_type: str, code_snippet: str, top_k: int = 3
    ) -> list[dict]:
        """Query for semantically similar past fixes (best-effort)."""
        if self.collection is None:
            return []
        try:
            if self.collection.count() == 0:
                return []
            query_text = f"{bug_type}: {code_snippet[:500]}"
            results = self.collection.query(
                query_texts=[query_text],
                n_results=top_k,
                include=["metadatas", "distances"],
            )
        except Exception:
            return []

        matches = []
        if results.get("metadatas") and results["metadatas"][0]:
            for meta, dist in zip(results["metadatas"][0], results["distances"][0]):
                similarity = 1.0 - dist  # cosine distance to similarity
                if similarity >= settings.chroma_similarity_threshold:
                    meta["similarity"] = similarity
                    matches.append(meta)
        return matches

    def get_stats(self) -> dict:
        """Get memory statistics."""
        if self.collection is None:
            return {"total_fixes_stored": 0}
        try:
            return {"total_fixes_stored": self.collection.count()}
        except Exception:
            return {"total_fixes_stored": 0}
