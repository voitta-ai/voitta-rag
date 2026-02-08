"""Sparse embedding service using fastembed BM25."""

import logging

from ..config import get_settings

logger = logging.getLogger(__name__)

SPARSE_VECTOR_NAME = "bm25"


class SparseEmbeddingService:
    """Service for generating BM25 sparse embeddings via fastembed."""

    def __init__(self):
        self._model = None

    @property
    def model(self):
        """Lazy load the BM25 model."""
        if self._model is None:
            from fastembed import SparseTextEmbedding

            logger.info("Loading BM25 sparse embedding model")
            self._model = SparseTextEmbedding(model_name="Qdrant/bm25")
            logger.info("BM25 model loaded")
        return self._model

    def embed_query(self, query: str) -> tuple[list[int], list[float]]:
        """Generate sparse embedding for a search query.

        Returns:
            Tuple of (indices, values) for the sparse vector.
        """
        results = list(self.model.query_embed(query))
        if not results:
            return [], []
        emb = results[0]
        return emb.indices.tolist(), emb.values.tolist()

    def embed_texts(self, texts: list[str]) -> list[tuple[list[int], list[float]]]:
        """Generate sparse embeddings for multiple texts.

        Returns:
            List of (indices, values) tuples.
        """
        if not texts:
            return []
        results = list(self.model.embed(texts))
        return [(emb.indices.tolist(), emb.values.tolist()) for emb in results]


# Global singleton
_sparse_embedding_service: SparseEmbeddingService | None = None


def get_sparse_embedding_service() -> SparseEmbeddingService:
    """Get the global sparse embedding service instance."""
    global _sparse_embedding_service
    if _sparse_embedding_service is None:
        _sparse_embedding_service = SparseEmbeddingService()
    return _sparse_embedding_service
