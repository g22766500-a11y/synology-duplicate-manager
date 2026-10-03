from __future__ import annotations

import json
import logging
import os
import re
import threading
from pathlib import Path

from .config import EXCLUDE_AT_PREFIX, EXCLUDE_DIRS, SCAN_ROOTS

SETTINGS_PATH = Path(os.getenv("SETTINGS_PATH", "/data/settings.json"))
_nas_root_value = os.getenv("NAS_ROOT", "").strip() or "/"
NAS_ROOT = Path(_nas_root_value).resolve()
logger = logging.getLogger(__name__)


def _legacy_path(value: str) -> str:
    # Only migrate the old Synology mapping when browsing the container root.
    if _nas_root_value == "/" and re.match(r"^/nas/volume[0-9]+(?:/|$)", value):
        return value[len("/nas"):]
    return value


def _env_paths(name: str) -> list[str]:
    return [x.strip() for x in os.getenv(name, "").split(",") if x.strip()]


def _default_photo_roots() -> list[str]:
    explicit = _env_paths("PHOTO_ROOTS")
    if explicit:
        return explicit
    legacy = os.getenv("PHOTO_ROOT", "").strip()
    if legacy:
        return [legacy]
    return list(SCAN_ROOTS)


class RuntimeSettings:
    def __init__(self) -> None:
        self._lock = threading.RLock()
        self._data = {
            "scan_roots": [self._normalize_path(x, require_exists=False) for x in SCAN_ROOTS],
            "photo_roots": [self._normalize_path(x, require_exists=False) for x in _default_photo_roots()],
        }
        self.load()

    def _normalize_path(self, value: str, require_exists: bool = True) -> str:
        value = value.strip()
        raw = Path(value)
        if not value or not raw.is_absolute():
            raise ValueError(f"절대경로를 입력해야 합니다: {value}")
        path = raw.resolve()
        try:
            path.relative_to(NAS_ROOT)
        except ValueError as exc:
            raise ValueError(f"{NAS_ROOT} 아래 경로만 사용할 수 있습니다: {value}") from exc
        if require_exists and not path.is_dir():
            raise ValueError(f"폴더를 찾을 수 없습니다: {value}")
        return str(path)

    def _normalize_many(self, values: list[str]) -> list[str]:
        result: list[str] = []
        seen: set[str] = set()
        for value in values:
            path = self._normalize_path(value)
            if path not in seen:
                result.append(path)
                seen.add(path)
        if not result:
            raise ValueError("폴더를 하나 이상 선택해야 합니다.")
        return result

    def load(self) -> None:
        with self._lock:
            if not SETTINGS_PATH.exists():
                return
            try:
                raw = json.loads(SETTINGS_PATH.read_text(encoding="utf-8"))
                migrated = False
                for key in ("scan_roots", "photo_roots"):
                    values = raw.get(key)
                    if isinstance(values, list) and values:
                        # Preserve absolute paths if a NAS mount is temporarily unavailable.
                        normalized = []
                        for value in values:
                            try:
                                original = str(value).strip()
                                updated = _legacy_path(original)
                                normalized.append(self._normalize_path(updated, require_exists=False))
                                migrated = migrated or updated != original
                            except ValueError as exc:
                                logger.warning("저장된 경로를 무시합니다: %s", exc)
                        if normalized:
                            self._data[key] = normalized
                if migrated:
                    backup = SETTINGS_PATH.with_name("settings.pre-path-migration.json")
                    if not backup.exists():
                        backup.write_bytes(SETTINGS_PATH.read_bytes())
                    self.save()
                    logger.info("기존 /nas 경로를 변환했습니다. 백업: %s", backup)
            except Exception as exc:
                # Bad settings should not prevent the service from starting.
                logger.warning("설정 파일을 읽거나 변환하지 못했습니다: %s", exc)
                return

    def save(self) -> None:
        with self._lock:
            SETTINGS_PATH.parent.mkdir(parents=True, exist_ok=True)
            temp = SETTINGS_PATH.with_suffix(".tmp")
            temp.write_text(json.dumps(self._data, ensure_ascii=False, indent=2), encoding="utf-8")
            temp.replace(SETTINGS_PATH)

    def get(self) -> dict:
        with self._lock:
            return {
                "scan_roots": list(self._data["scan_roots"]),
                "photo_roots": list(self._data["photo_roots"]),
                "nas_root": str(NAS_ROOT),
                "exclude_at_prefix": EXCLUDE_AT_PREFIX,
                "exclude_dirs": sorted(EXCLUDE_DIRS),
            }

    def set_roots(self, scan_roots: list[str], photo_roots: list[str]) -> dict:
        scan = self._normalize_many(scan_roots)
        photos = self._normalize_many(photo_roots)
        with self._lock:
            self._data["scan_roots"] = scan
            self._data["photo_roots"] = photos
            self.save()
            return self.get()

    @property
    def scan_roots(self) -> list[str]:
        return self.get()["scan_roots"]

    @property
    def photo_roots(self) -> list[str]:
        return self.get()["photo_roots"]


runtime_settings = RuntimeSettings()


def should_skip_dir(name: str) -> bool:
    if EXCLUDE_AT_PREFIX and name.startswith("@"):
        return True
    return name in EXCLUDE_DIRS or name.lower() in {x.lower() for x in EXCLUDE_DIRS}


def browse_directory(path_value: str | None = None) -> dict:
    current = runtime_settings._normalize_path(path_value or str(NAS_ROOT))
    current_path = Path(current)
    if not current_path.is_dir():
        raise ValueError(f"폴더를 찾을 수 없습니다: {current}")

    children = []
    try:
        entries = sorted(current_path.iterdir(), key=lambda p: p.name.lower())
    except OSError as exc:
        raise ValueError(f"폴더를 읽을 수 없습니다: {current}: {exc}") from exc

    for item in entries:
        try:
            if item.is_symlink() or not item.is_dir() or should_skip_dir(item.name):
                continue
            resolved = item.resolve()
            resolved.relative_to(NAS_ROOT)
            children.append({"name": item.name, "path": str(resolved)})
        except (OSError, ValueError):
            continue

    parent = None
    if current_path != NAS_ROOT:
        candidate = current_path.parent.resolve()
        try:
            candidate.relative_to(NAS_ROOT)
            parent = str(candidate)
        except ValueError:
            parent = str(NAS_ROOT)

    return {"current": current, "parent": parent, "children": children}
