from typing import Literal

from pydantic import BaseModel


class ScanRequest(BaseModel):
    mode: Literal["quick", "full", "force_full"] = "quick"


class ScanState(BaseModel):
    running: bool
    mode: Literal["quick", "full", "force_full"] | None = None
    run_id: int | None = None
    started_at: str | None = None
    finished_at: str | None = None
    phase: str
    files_seen: int
    files_hashed: int
    hash_candidates: int
    dirs_skipped: int
    errors: int
    current_path: str | None = None
    message: str


class ScanStartResponse(BaseModel):
    accepted: bool
    state: ScanState


class DuplicateFile(BaseModel):
    path: str
    root: str
    size: int
    mtime_ns: int


class DuplicateGroup(BaseModel):
    md5: str
    size: int
    count: int
    reclaimable_bytes: int
    files: list[DuplicateFile]


class DuplicateList(BaseModel):
    total_groups: int
    limit: int
    offset: int
    items: list[DuplicateGroup]


class Summary(BaseModel):
    total_files: int
    hashed_files: int
    error_files: int
    duplicate_groups: int
    duplicate_files: int
    reclaimable_bytes: int
