from __future__ import annotations

import os
from dataclasses import dataclass
from pathlib import Path


def _bool(name: str, default: bool) -> bool:
    return os.getenv(name, str(default)).strip().lower() in {"1", "true", "yes", "on"}


@dataclass(frozen=True)
class PhotoSettings:
    data_dir: Path = Path(os.getenv("PHOTO_DATA_DIR", "/data/photo-search")).resolve()
    qdrant_url: str = os.getenv("QDRANT_URL", "http://qdrant:6333")
    collection: str = os.getenv("QDRANT_COLLECTION", "nas_photos")
    image_model: str = os.getenv("IMAGE_MODEL", "sentence-transformers/clip-ViT-B-32")
    text_model: str = os.getenv("TEXT_MODEL", "sentence-transformers/clip-ViT-B-32-multilingual-v1")
    vector_size: int = int(os.getenv("VECTOR_SIZE", "512"))
    batch_size: int = int(os.getenv("PHOTO_BATCH_SIZE", "16"))
    search_limit: int = int(os.getenv("PHOTO_SEARCH_LIMIT", "60"))
    thumbnail_size: int = int(os.getenv("THUMBNAIL_SIZE", "480"))
    scan_on_start: bool = _bool("PHOTO_SCAN_ON_START", False)
    exclude_at_prefix: bool = _bool("EXCLUDE_AT_PREFIX", True)
    excluded_dirs: frozenset[str] = frozenset(
        part.strip().lower()
        for part in os.getenv("EXCLUDE_DIRS", "#recycle,#snapshot,docker").split(",")
        if part.strip()
    )

    @property
    def thumbnail_dir(self) -> Path:
        return self.data_dir / "thumbnails"


photo_settings = PhotoSettings()
