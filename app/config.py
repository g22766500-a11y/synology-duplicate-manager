import os


def _env_bool(name: str, default: bool) -> bool:
    value = os.getenv(name)
    if value is None:
        return default
    return value.strip().lower() in {"1", "true", "yes", "on"}


SCAN_ROOTS = [p.strip() for p in (os.getenv("SCAN_ROOTS", "").strip() or "/volume1").split(",") if p.strip()]
DB_PATH = os.getenv("DB_PATH", "/data/duplicates.db")
EXCLUDE_DIRS = {
    x.strip()
    for x in os.getenv("EXCLUDE_DIRS", "#recycle,#snapshot,docker").split(",")
    if x.strip()
}
EXCLUDE_AT_PREFIX = _env_bool("EXCLUDE_AT_PREFIX", True)
HASH_CHUNK_SIZE = int(os.getenv("HASH_CHUNK_SIZE", str(4 * 1024 * 1024)))
