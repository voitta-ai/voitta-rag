"""Embedding service using sentence-transformers."""

import logging
from functools import lru_cache

import torch
from sentence_transformers import SentenceTransformer

from ..config import get_settings

logger = logging.getLogger(__name__)


class EmbeddingService:
    """Service for generating text embeddings using sentence-transformers."""

    def __init__(self, model_name: str | None = None):
        settings = get_settings()
        self.model_name = model_name or settings.embedding_model
        self.dimension = settings.embedding_dimension
        self._model: SentenceTransformer | None = None

    @property
    def model(self) -> SentenceTransformer:
        """Lazy load the model."""
        if self._model is None:
            settings = get_settings()
            logger.info(f"Loading embedding model: {self.model_name}")

            # Determine device based on configuration
            device_setting = settings.embedding_device.lower()
            if device_setting == "cpu":
                device = "cpu"
            elif device_setting == "cuda":
                device = "cuda"
            else:  # "auto" or any other value
                device = "cuda" if torch.cuda.is_available() else "cpu"

            logger.info(f"Using device: {device} (config: {device_setting})")
            self._model = SentenceTransformer(self.model_name, device=device)
            logger.info(f"Model loaded successfully on {device}")
        return self._model

    def embed_text(self, text: str) -> list[float]:
        """Generate embedding for a single text.

        For e5 models, we prepend 'passage: ' for documents.
        """
        # e5 models expect specific prefixes
        if "e5" in self.model_name.lower():
            text = f"passage: {text}"

        embedding = self.model.encode(text, convert_to_numpy=True)
        return embedding.tolist()

    def embed_texts(self, texts: list[str], batch_size: int = 32) -> list[list[float]]:
        """Generate embeddings for multiple texts.

        For e5 models, we prepend 'passage: ' for documents.
        """
        if not texts:
            return []

        # e5 models expect specific prefixes
        if "e5" in self.model_name.lower():
            texts = [f"passage: {text}" for text in texts]

        embeddings = self.model.encode(
            texts,
            batch_size=batch_size,
            convert_to_numpy=True,
            show_progress_bar=len(texts) > 100,
        )
        return embeddings.tolist()

    def embed_query(self, query: str) -> list[float]:
        """Generate embedding for a search query.

        For e5 models, we prepend 'query: ' for queries.
        """
        # e5 models expect specific prefixes
        if "e5" in self.model_name.lower():
            query = f"query: {query}"

        embedding = self.model.encode(query, convert_to_numpy=True)
        return embedding.tolist()


# Global singleton instance
_embedding_service: EmbeddingService | None = None


def get_embedding_service() -> EmbeddingService:
    """Get the global embedding service instance."""
    global _embedding_service
    if _embedding_service is None:
        _embedding_service = EmbeddingService()
    return _embedding_service
