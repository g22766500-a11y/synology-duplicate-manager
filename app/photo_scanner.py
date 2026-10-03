from __future__ import annotations

import hashlib
import logging
import os
import threading
import time
import uuid
from dataclasses import asdict, dataclass
from pathlib import Path

from PIL import Image
from qdrant_client import models

from .photo_engine import photo_engine
from .photo_settings import photo_settings
from .runtime_settings import runtime_settings, should_skip_dir

logger = logging.getLogger(__name__)
SUPPORTED_EXTENSIONS = {".jpg", ".jpeg", ".png", ".webp", ".bmp", ".gif", ".tif", ".tiff"}


@dataclass
class PhotoScanState:
    running: bool = False
    phase: str = "idle"
    total: int = 0
    processed: int = 0
    indexed: int = 0
    skipped: int = 0
    failed: int = 0
    deleted: int = 0
    dirs_skipped: int = 0
    current_file: str = ""
    started_at: float | None = None
    finished_at: float | None = None
    error: str | None = None
    last_error: str | None = None


class PhotoScanner:
    def __init__(self) -> None:
        self.state = PhotoScanState()
        self._lock = threading.Lock()

    def status(self) -> dict:
        with self._lock:
            result = asdict(self.state)
        if result["started_at"]:
            end = result["finished_at"] or time.time()
            result["elapsed_seconds"] = round(end - result["started_at"], 1)
        else:
            result["elapsed_seconds"] = 0
        return result

    def start(self) -> bool:
        with self._lock:
            if self.state.running:
                return False
            self.state = PhotoScanState(running=True, phase="사진 목록 확인 중", started_at=time.time())
        threading.Thread(target=self._run, daemon=True, name="photo-scanner").start()
        return True

    def _set(self, **values) -> None:
        with self._lock:
            for key, value in values.items():
                setattr(self.state, key, value)

    def _inc(self, key: str, amount: int = 1) -> None:
        with self._lock:
            setattr(self.state, key, getattr(self.state, key) + amount)

    def _iter_photos(self, roots: list[Path]) -> list[tuple[Path, str, str]]:
        photos: list[tuple[Path, str, str]] = []
        seen: set[str] = set()
        for root in roots:
            for current, dirs, files in os.walk(root, topdown=True, followlinks=False):
                before = len(dirs)
                dirs[:] = [d for d in dirs if not should_skip_dir(d)]
                self._inc("dirs_skipped", before - len(dirs))
                for name in files:
                    path = Path(current) / name
                    if path.is_symlink() or path.suffix.lower() not in SUPPORTED_EXTENSIONS:
                        continue
                    absolute_key = str(path.resolve())
                    if absolute_key in seen:
                        continue
                    seen.add(absolute_key)
                    relative = path.relative_to(root).as_posix()
                    photos.append((path, str(root), relative))
        return photos

    @staticmethod
    def _point_id(absolute_path: str) -> str:
        return str(uuid.uuid5(uuid.NAMESPACE_URL, f"nas-photo:{absolute_path}"))

    def _existing(self) -> dict[str, dict]:
        found: dict[str, dict] = {}
        offset = None
        while True:
            points, offset = photo_engine.client.scroll(
                collection_name=photo_settings.collection,
                limit=256,
                offset=offset,
                with_payload=True,
                with_vectors=False,
            )
            for point in points:
                payload = point.payload or {}
                path = payload.get("path")
                if path:
                    found[str(path)] = {
                        "id": point.id,
                        "mtime_ns": payload.get("mtime_ns"),
                        "size": payload.get("size"),
                    }
            if offset is None:
                break
        return found

    def _run(self) -> None:
        try:
            roots = [Path(x).resolve() for x in runtime_settings.photo_roots]
            missing = [str(root) for root in roots if not root.is_dir()]
            if missing:
                raise RuntimeError("사진 폴더를 찾을 수 없습니다: " + ", ".join(missing))

            self._set(phase="Qdrant 연결 확인 중")
            photo_engine.ensure_collection()
            paths = self._iter_photos(roots)
            self._set(total=len(paths), phase="변경 사항 확인 중")
            existing = self._existing()
            current_paths = {str(path.resolve()) for path, _, _ in paths}
            stale_ids = [item["id"] for absolute, item in existing.items() if absolute not in current_paths]
            if stale_ids:
                photo_engine.client.delete(
                    collection_name=photo_settings.collection,
                    points_selector=models.PointIdsList(points=stale_ids),
                    wait=True,
                )
                self._set(deleted=len(stale_ids))

            pending = []
            for path, source_root, relative in paths:
                absolute = str(path.resolve())
                try:
                    stat = path.stat()
                    old = existing.get(absolute)
                    if old and old.get("mtime_ns") == stat.st_mtime_ns and old.get("size") == stat.st_size:
                        self._inc("skipped")
                        self._inc("processed")
                    else:
                        pending.append((path, source_root, relative, absolute, stat))
                except OSError:
                    self._inc("failed")
                    self._inc("processed")

            if pending:
                self._set(phase="AI 모델 로딩 중")
                try:
                    photo_engine.load_models()
                except Exception as exc:
                    raise RuntimeError(f"CLIP 모델 로딩 실패: {exc}") from exc

            self._set(phase="AI 인덱싱 중")
            for start in range(0, len(pending), photo_settings.batch_size):
                batch = pending[start:start + photo_settings.batch_size]
                valid = []
                for item in batch:
                    try:
                        with Image.open(item[0]) as image:
                            image.verify()
                        valid.append(item)
                    except Exception:
                        logger.exception("지원하지 않는 이미지: %s", item[0])
                        self._inc("failed")
                        self._inc("processed")
                if not valid:
                    continue

                self._set(current_file=valid[-1][3])
                try:
                    vectors = photo_engine.encode_images([item[0] for item in valid])
                    points = []
                    for (path, source_root, relative, absolute, stat), vector in zip(valid, vectors):
                        points.append(
                            models.PointStruct(
                                id=self._point_id(absolute),
                                vector=vector,
                                payload={
                                    "path": absolute,
                                    "source_root": source_root,
                                    "relative_path": relative,
                                    "name": path.name,
                                    "extension": path.suffix.lower(),
                                    "size": stat.st_size,
                                    "mtime_ns": stat.st_mtime_ns,
                                    "modified_at": stat.st_mtime,
                                },
                            )
                        )
                    photo_engine.upsert(points)
                    self._inc("indexed", len(points))
                    self._inc("processed", len(points))
                except Exception as exc:
                    logger.exception("사진 배치 인덱싱 실패")
                    self._set(last_error=f"{type(exc).__name__}: {exc}")
                    self._inc("failed", len(valid))
                    self._inc("processed", len(valid))

            phase = "완료" if self.state.failed == 0 else "완료 (일부 실패)"
            self._set(phase=phase, running=False, current_file="", finished_at=time.time())
        except Exception as exc:
            logger.exception("사진 스캔 실패")
            message = f"{type(exc).__name__}: {exc}"
            self._set(
                phase="오류",
                running=False,
                error=message,
                last_error=message,
                current_file="",
                finished_at=time.time(),
            )


photo_scanner = PhotoScanner()


def thumbnail_cache_name(point_id: str, mtime_ns: int) -> str:
    digest = hashlib.sha256(f"{point_id}:{mtime_ns}".encode()).hexdigest()
    return f"{digest}.jpg"
