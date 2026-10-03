from __future__ import annotations

import threading
from pathlib import Path

import numpy as np
from PIL import Image, ImageOps
from qdrant_client import QdrantClient, models
from sentence_transformers import SentenceTransformer

from .photo_settings import photo_settings


class SearchEngine:
    def __init__(self) -> None:
        self.client = QdrantClient(url=photo_settings.qdrant_url, timeout=60)
        self._image_model: SentenceTransformer | None = None
        self._text_model: SentenceTransformer | None = None
        self._model_lock = threading.Lock()
        self._inference_lock = threading.Lock()

    def ensure_collection(self) -> None:
        if not self.client.collection_exists(photo_settings.collection):
            self.client.create_collection(
                collection_name=photo_settings.collection,
                vectors_config=models.VectorParams(size=photo_settings.vector_size, distance=models.Distance.COSINE),
            )
            self.client.create_payload_index(
                collection_name=photo_settings.collection,
                field_name="path",
                field_schema=models.PayloadSchemaType.KEYWORD,
            )

    def _load_models(self) -> None:
        if self._image_model is not None and self._text_model is not None:
            return
        with self._model_lock:
            if self._image_model is None:
                self._image_model = SentenceTransformer(photo_settings.image_model, device="cpu")
            if self._text_model is None:
                self._text_model = SentenceTransformer(photo_settings.text_model, device="cpu")

    def load_models(self) -> None:
        """Load both CLIP models eagerly so startup/indexing errors are visible."""
        self._load_models()

    def encode_images(self, paths: list[Path]) -> list[list[float]]:
        self._load_models()
        images: list[Image.Image] = []
        try:
            for path in paths:
                with Image.open(path) as original:
                    image = ImageOps.exif_transpose(original).convert("RGB")
                    images.append(image.copy())
            assert self._image_model is not None
            with self._inference_lock:
                vectors = self._image_model.encode(images, batch_size=photo_settings.batch_size, normalize_embeddings=True, show_progress_bar=False)
            return np.asarray(vectors, dtype=np.float32).tolist()
        finally:
            for image in images:
                image.close()

    def encode_text(self, query: str) -> list[float]:
        self._load_models()
        assert self._text_model is not None
        with self._inference_lock:
            vector = self._text_model.encode([query], normalize_embeddings=True, show_progress_bar=False)[0]
        return np.asarray(vector, dtype=np.float32).tolist()

    def upsert(self, points: list[models.PointStruct]) -> None:
        if points:
            self.client.upsert(collection_name=photo_settings.collection, points=points, wait=True)

    def search(self, query: str, limit: int) -> list[dict]:
        vector = self.encode_text(query)
        response = self.client.query_points(collection_name=photo_settings.collection, query=vector, limit=limit, with_payload=True)
        results = []
        for point in response.points:
            payload = point.payload or {}
            results.append({
                "id": str(point.id),
                "score": round(float(point.score), 4),
                "name": payload.get("name", ""),
                "relative_path": payload.get("relative_path", ""),
                "modified_at": payload.get("modified_at"),
            })
        return results


photo_engine = SearchEngine()
