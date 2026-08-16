"""ChromaDB repository memory for storing and querying past fixes.

ChromaDB pulls in numpy's native extensions, which can be unavailable in locked-down
environments (e.g. Windows Application Control blocking the DLL). Memory is only an
accelerator, so the import is made optional: if it fails, the whole memory layer
degrades to a no-op and the pipeline runs normally without it.
"""

from __future__ import annotations

import hashlib
from datetime import datetime, timezone

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
        repo_key: str = "global",
        finding_id: str = "",
        files_changed: list[str] | None = None,
        validation_summary: str = "",
    ) -> None:
        """Store a validated fix with a stable, repository-scoped identity."""
        if self.collection is None:
            return
        normalized_repo = (repo_key or "global").strip().lower()
        doc_id = self._fix_id(
            normalized_repo, bug_type, affected_file, root_cause,
        )
        embedding_text = (
            f"Issue type: {bug_type}. Root cause: {root_cause}. "
            f"Validated fix: {fix_strategy}"
        )
        try:
            # Upsert makes retries idempotent. The same validated fix updates its
            # evidence rather than producing duplicate memories each process run.
            self.collection.upsert(
                ids=[doc_id],
                documents=[embedding_text],
                metadatas=[{
                    "schema_version": 2,
                    "repo_key": normalized_repo,
                    "bug_type": bug_type,
                    "root_cause": root_cause,
                    "fix_strategy": fix_strategy,
                    "affected_file": affected_file,
                    "confidence_score": confidence_score,
                    "finding_id": finding_id,
                    "files_changed": ",".join(files_changed or [affected_file]),
                    "validation_summary": validation_summary,
                    "validated_at": datetime.now(timezone.utc).isoformat(),
                }],
            )
        except Exception:
            pass

    @staticmethod
    def _fix_id(repo_key: str, bug_type: str, affected_file: str, root_cause: str) -> str:
        """Stable identity used to de-duplicate the same validated fix."""
        fingerprint = "|".join([
            (repo_key or "global").strip().lower(),
            bug_type.strip().lower(),
            affected_file.replace("\\", "/").lower(),
            root_cause.strip().lower(),
        ])
        return "fix:" + hashlib.sha256(fingerprint.encode("utf-8")).hexdigest()[:24]

    def query_similar(
        self,
        bug_type: str,
        code_snippet: str,
        top_k: int = 3,
        repo_key: str = "global",
    ) -> list[dict]:
        """Query semantically similar validated fixes from the same repository."""
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
                where={"repo_key": (repo_key or "global").strip().lower()},
            )
        except Exception:
            return []

        matches = []
        if results.get("metadatas") and results["metadatas"][0]:
            for meta, dist in zip(results["metadatas"][0], results["distances"][0]):
                if not meta:
                    continue
                similarity = 1.0 - dist  # cosine distance to similarity
                if similarity >= settings.chroma_similarity_threshold:
                    match = dict(meta)
                    match["similarity"] = similarity
                    matches.append(match)
        return matches

    def get_stats(self) -> dict:
        """Get memory statistics."""
        if self.collection is None:
            return {"total_fixes_stored": 0}
        try:
            return {"total_fixes_stored": self.collection.count()}
        except Exception:
            return {"total_fixes_stored": 0}
