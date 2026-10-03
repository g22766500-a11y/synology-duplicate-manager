from contextlib import asynccontextmanager
from pathlib import Path
import os

from fastapi import FastAPI, HTTPException, Query, Request, status
from fastapi.responses import FileResponse, HTMLResponse
from fastapi.templating import Jinja2Templates

from .config import EXCLUDE_AT_PREFIX, EXCLUDE_DIRS
from .database import connect, init_db
from .scanner import get_state, start_scan
from .photo_engine import photo_engine
from .photo_scanner import photo_scanner, thumbnail_cache_name
from .photo_settings import photo_settings
from .runtime_settings import NAS_ROOT, browse_directory, runtime_settings
from PIL import Image, ImageOps
import asyncio
from .schemas import DuplicateFile, DuplicateGroup, DuplicateList, ScanRequest, ScanStartResponse, ScanState, Summary
from pydantic import BaseModel, Field

BASE_DIR = Path(__file__).resolve().parent
templates = Jinja2Templates(directory=str(BASE_DIR / "templates"))


@asynccontextmanager
async def lifespan(app: FastAPI):
    init_db()
    photo_settings.data_dir.mkdir(parents=True, exist_ok=True)
    photo_settings.thumbnail_dir.mkdir(parents=True, exist_ok=True)
    try:
        photo_engine.ensure_collection()
        if photo_settings.scan_on_start:
            photo_scanner.start()
    except Exception:
        pass
    yield


app = FastAPI(
    title="Synology Duplicate Manager API",
    version="3.6.0",
    description="Synology NAS duplicate manager + multilingual natural-language photo search.",
    lifespan=lifespan,
)


@app.get("/", response_class=HTMLResponse, include_in_schema=False)
def web_ui(request: Request):
    return templates.TemplateResponse(
        request=request,
        name="index.html",
        context={"scan_roots": runtime_settings.scan_roots},
    )


@app.get("/health", tags=["system"])
def health():
    return {"status": "ok"}


@app.get("/api/config", tags=["system"])
def config():
    current = runtime_settings.get()
    return {**current, "read_only": False}


class RootSettingsUpdate(BaseModel):
    scan_roots: list[str] = Field(min_length=1)
    photo_roots: list[str] = Field(min_length=1)


@app.get("/api/settings", tags=["settings"])
def settings_get():
    return runtime_settings.get()


@app.put("/api/settings", tags=["settings"])
def settings_update(payload: RootSettingsUpdate):
    if get_state().get("running") or photo_scanner.status().get("running"):
        raise HTTPException(status_code=409, detail="스캔 또는 사진 인덱싱이 진행 중일 때는 경로 설정을 변경할 수 없습니다.")
    try:
        return runtime_settings.set_roots(payload.scan_roots, payload.photo_roots)
    except ValueError as exc:
        raise HTTPException(status_code=422, detail=str(exc)) from exc


@app.get("/api/fs/browse", tags=["settings"])
def fs_browse(path: str | None = Query(default=None)):
    try:
        return browse_directory(path)
    except ValueError as exc:
        raise HTTPException(status_code=422, detail=str(exc)) from exc


@app.get("/api/status", response_model=ScanState, tags=["scan"])
def scan_status():
    return get_state()


@app.post("/api/scan", response_model=ScanStartResponse, status_code=status.HTTP_202_ACCEPTED, tags=["scan"])
def scan_start(payload: ScanRequest | None = None):
    # Body is optional for backward compatibility. No body means quick scan.
    mode = payload.mode if payload else "quick"
    try:
        accepted, state = start_scan(mode)
    except ValueError as exc:
        raise HTTPException(status_code=422, detail=str(exc)) from exc
    if not accepted:
        raise HTTPException(status_code=409, detail="A scan is already running")
    return {"accepted": True, "state": state}


@app.get("/api/summary", response_model=Summary, tags=["duplicates"])
def summary():
    with connect() as conn:
        base = conn.execute(
            """
            SELECT
              COUNT(*) AS total_files,
              SUM(CASE WHEN md5 IS NOT NULL THEN 1 ELSE 0 END) AS hashed_files,
              SUM(CASE WHEN error IS NOT NULL THEN 1 ELSE 0 END) AS error_files
            FROM files
            """
        ).fetchone()
        dup = conn.execute(
            """
            SELECT
              COUNT(*) AS duplicate_groups,
              COALESCE(SUM(cnt),0) AS duplicate_files,
              COALESCE(SUM((cnt-1)*size),0) AS reclaimable_bytes
            FROM (
              SELECT size,COUNT(*) AS cnt
              FROM files
              WHERE md5 IS NOT NULL AND error IS NULL
              GROUP BY md5,size
              HAVING COUNT(*) > 1
            )
            """
        ).fetchone()
        return {
            "total_files": base["total_files"] or 0,
            "hashed_files": base["hashed_files"] or 0,
            "error_files": base["error_files"] or 0,
            "duplicate_groups": dup["duplicate_groups"] or 0,
            "duplicate_files": dup["duplicate_files"] or 0,
            "reclaimable_bytes": dup["reclaimable_bytes"] or 0,
        }


def _load_group(conn, md5: str, size: int) -> DuplicateGroup:
    rows = conn.execute(
        "SELECT path,root,size,mtime_ns FROM files WHERE md5=? AND size=? AND error IS NULL ORDER BY path",
        (md5, size),
    ).fetchall()
    if len(rows) < 2:
        raise HTTPException(status_code=404, detail="Duplicate group not found")
    return DuplicateGroup(
        md5=md5,
        size=size,
        count=len(rows),
        reclaimable_bytes=(len(rows) - 1) * size,
        files=[DuplicateFile(**dict(row)) for row in rows],
    )


@app.get("/api/duplicates", response_model=DuplicateList, tags=["duplicates"])
def duplicates(
    limit: int = Query(100, ge=1, le=500),
    offset: int = Query(0, ge=0),
    min_size: int = Query(0, ge=0, description="Minimum file size in bytes"),
):
    with connect() as conn:
        total = conn.execute(
            """
            SELECT COUNT(*) AS c FROM (
              SELECT md5,size FROM files
              WHERE md5 IS NOT NULL AND error IS NULL AND size>=?
              GROUP BY md5,size HAVING COUNT(*) > 1
            )
            """,
            (min_size,),
        ).fetchone()["c"]
        groups = conn.execute(
            """
            SELECT md5,size,COUNT(*) AS cnt,(COUNT(*)-1)*size AS reclaimable
            FROM files
            WHERE md5 IS NOT NULL AND error IS NULL AND size>=?
            GROUP BY md5,size
            HAVING COUNT(*) > 1
            ORDER BY reclaimable DESC, size DESC
            LIMIT ? OFFSET ?
            """,
            (min_size, limit, offset),
        ).fetchall()
        items = [_load_group(conn, row["md5"], row["size"]) for row in groups]
        return DuplicateList(total_groups=total, limit=limit, offset=offset, items=items)


@app.get("/api/duplicates/{md5}", response_model=list[DuplicateGroup], tags=["duplicates"])
def duplicate_by_md5(md5: str):
    if len(md5) != 32 or any(c not in "0123456789abcdefABCDEF" for c in md5):
        raise HTTPException(status_code=422, detail="Invalid MD5")
    md5 = md5.lower()
    with connect() as conn:
        sizes = conn.execute(
            "SELECT size FROM files WHERE md5=? AND error IS NULL GROUP BY size HAVING COUNT(*)>1 ORDER BY size DESC",
            (md5,),
        ).fetchall()
        if not sizes:
            raise HTTPException(status_code=404, detail="Duplicate group not found")
        return [_load_group(conn, md5, row["size"]) for row in sizes]


class DeleteFileRequest(BaseModel):
    path: str = Field(min_length=1)


class BatchDeleteFilesRequest(BaseModel):
    # 화면 제한은 중복 "그룹" 기준(최대 500개)이라 실제 선택 파일 수는 500개를 넘을 수 있다.
    # 일괄 삭제는 현재 화면에서 선택된 파일 전체를 받을 수 있도록 파일 개수 500 제한을 두지 않는다.
    paths: list[str] = Field(min_length=1)


def _ensure_delete_idle() -> None:
    if get_state().get("running"):
        raise HTTPException(status_code=409, detail="중복 스캔 중에는 파일을 삭제할 수 없습니다.")
    if photo_scanner.status().get("running"):
        raise HTTPException(status_code=409, detail="사진 인덱싱 중에는 파일을 삭제할 수 없습니다.")


def _resolve_deletable_file(raw_path: str) -> Path:
    requested = Path(raw_path)
    if not requested.is_absolute():
        raise HTTPException(status_code=422, detail="파일 경로는 절대경로여야 합니다.")
    try:
        resolved = requested.resolve(strict=True)
        resolved.relative_to(NAS_ROOT)
    except FileNotFoundError as exc:
        raise HTTPException(status_code=404, detail=f"파일을 찾을 수 없습니다: {raw_path}") from exc
    except ValueError as exc:
        raise HTTPException(status_code=403, detail=f"{NAS_ROOT} 아래 파일만 삭제할 수 있습니다: {raw_path}") from exc

    if requested.is_symlink() or not resolved.is_file():
        raise HTTPException(status_code=422, detail=f"일반 파일만 삭제할 수 있습니다: {raw_path}")
    return resolved


@app.delete("/api/files", tags=["duplicates"])
def delete_duplicate_file(payload: DeleteFileRequest):
    _ensure_delete_idle()
    resolved = _resolve_deletable_file(payload.path)
    path = str(resolved)
    with connect() as conn:
        row = conn.execute(
            "SELECT path,size,md5,error FROM files WHERE path=?",
            (path,),
        ).fetchone()
        if not row or row["error"] is not None or not row["md5"]:
            raise HTTPException(status_code=404, detail="스캔 DB에 등록된 중복 파일이 아닙니다.")

        duplicate_rows = conn.execute(
            "SELECT path FROM files WHERE md5=? AND size=? AND error IS NULL AND path<>?",
            (row["md5"], row["size"], path),
        ).fetchall()
        other_existing = False
        for duplicate_row in duplicate_rows:
            try:
                other_path = Path(duplicate_row["path"]).resolve(strict=True)
                other_path.relative_to(NAS_ROOT)
                if other_path.is_file():
                    other_existing = True
                    break
            except (FileNotFoundError, OSError, ValueError):
                continue
        if not other_existing:
            raise HTTPException(status_code=409, detail="실제 디스크에 다른 중복 파일이 확인되지 않아 삭제하지 않았습니다.")

        try:
            resolved.unlink()
        except PermissionError as exc:
            raise HTTPException(status_code=403, detail="파일 삭제 권한이 없습니다.") from exc
        except OSError as exc:
            raise HTTPException(status_code=500, detail=f"파일 삭제 실패: {exc}") from exc

        conn.execute("DELETE FROM files WHERE path=?", (path,))
        conn.commit()

    return {"deleted": True, "path": path}


@app.delete("/api/files/batch", tags=["duplicates"])
def delete_duplicate_files_batch(payload: BatchDeleteFilesRequest):
    _ensure_delete_idle()

    # 중복 경로를 제거하되 사용자가 보낸 순서는 유지한다.
    raw_paths = list(dict.fromkeys(payload.paths))
    resolved_paths = [_resolve_deletable_file(raw) for raw in raw_paths]
    paths = [str(path) for path in resolved_paths]
    selected = set(paths)

    with connect() as conn:
        # SQLite는 한 SQL 문에서 사용할 수 있는 바인딩 변수 개수에 제한이 있을 수 있으므로
        # 500개가 넘는 파일도 안전하게 조회할 수 있도록 작은 청크로 나눠 읽는다.
        rows = []
        query_chunk_size = 400
        for start in range(0, len(paths), query_chunk_size):
            chunk = paths[start:start + query_chunk_size]
            placeholders = ",".join("?" for _ in chunk)
            rows.extend(
                conn.execute(
                    f"SELECT path,size,md5,error FROM files WHERE path IN ({placeholders})",
                    chunk,
                ).fetchall()
            )
        by_path = {row["path"]: row for row in rows}

        missing = [path for path in paths if path not in by_path]
        if missing:
            raise HTTPException(status_code=404, detail=f"스캔 DB에 등록되지 않은 파일이 포함되어 있습니다: {missing[0]}")

        groups: dict[tuple[str, int], list[str]] = {}
        for path in paths:
            row = by_path[path]
            if row["error"] is not None or not row["md5"]:
                raise HTTPException(status_code=404, detail=f"중복 파일로 확인되지 않은 항목이 포함되어 있습니다: {path}")
            groups.setdefault((row["md5"], row["size"]), []).append(path)

        # 파일을 하나도 지우기 전에 모든 그룹을 검증한다.
        # 선택 삭제 후에도 실제 디스크에 동일 파일이 최소 1개 남아 있어야 한다.
        for (md5, size), group_selected in groups.items():
            duplicate_rows = conn.execute(
                "SELECT path FROM files WHERE md5=? AND size=? AND error IS NULL",
                (md5, size),
            ).fetchall()
            survivors = []
            for duplicate_row in duplicate_rows:
                candidate = duplicate_row["path"]
                if candidate in selected:
                    continue
                try:
                    candidate_path = Path(candidate).resolve(strict=True)
                    candidate_path.relative_to(NAS_ROOT)
                    if candidate_path.is_file():
                        survivors.append(candidate)
                except (FileNotFoundError, OSError, ValueError):
                    continue
            if not survivors:
                raise HTTPException(
                    status_code=409,
                    detail=f"한 중복 그룹의 파일을 모두 삭제할 수 없습니다. 최소 1개는 남겨야 합니다: {group_selected[0]}",
                )

        deleted_paths = []
        deleted_bytes = 0
        for resolved in resolved_paths:
            path = str(resolved)
            try:
                resolved.unlink()
            except PermissionError as exc:
                raise HTTPException(
                    status_code=403,
                    detail=f"일괄 삭제 중 파일 권한 오류가 발생했습니다. 이미 삭제된 파일은 복구되지 않습니다: {path}",
                ) from exc
            except OSError as exc:
                raise HTTPException(
                    status_code=500,
                    detail=f"일괄 삭제 중 오류가 발생했습니다. 이미 삭제된 파일은 복구되지 않습니다: {path} ({exc})",
                ) from exc

            deleted_paths.append(path)
            deleted_bytes += int(by_path[path]["size"] or 0)
            conn.execute("DELETE FROM files WHERE path=?", (path,))
            conn.commit()

    return {
        "deleted": True,
        "deleted_count": len(deleted_paths),
        "deleted_bytes": deleted_bytes,
        "paths": deleted_paths,
    }


@app.get("/api/photos/preflight", tags=["photos"])
def photo_preflight():
    roots = [Path(x).resolve() for x in runtime_settings.photo_roots]
    checks = [
        {
            "path": str(root),
            "exists": root.is_dir(),
            "readable": root.is_dir() and os.access(root, os.R_OK),
        }
        for root in roots
    ]
    result = {
        "photo_roots": [str(x) for x in roots],
        "roots": checks,
        "qdrant": False,
        "collection": photo_settings.collection,
        "image_model": photo_settings.image_model,
        "text_model": photo_settings.text_model,
    }
    try:
        photo_engine.ensure_collection()
        result["qdrant"] = True
    except Exception as exc:
        result["qdrant_error"] = f"{type(exc).__name__}: {exc}"
    result["ok"] = bool(checks and all(x["exists"] and x["readable"] for x in checks) and result["qdrant"])
    return result


@app.get("/api/photos/health", tags=["photos"])
def photo_health():
    try:
        photo_engine.ensure_collection()
        return {"ok": True, "photo_roots": runtime_settings.photo_roots}
    except Exception as exc:
        raise HTTPException(status_code=503, detail=str(exc)) from exc


@app.post("/api/photos/scan", tags=["photos"], status_code=status.HTTP_202_ACCEPTED)
def photo_scan_start():
    roots = [Path(x).resolve() for x in runtime_settings.photo_roots]
    missing = [str(root) for root in roots if not root.is_dir()]
    unreadable = [str(root) for root in roots if root.is_dir() and not os.access(root, os.R_OK)]
    if missing:
        raise HTTPException(status_code=422, detail=f"사진 폴더를 찾을 수 없습니다: {', '.join(missing)}")
    if unreadable:
        raise HTTPException(status_code=422, detail=f"사진 폴더를 읽을 수 없습니다: {', '.join(unreadable)}")
    try:
        photo_engine.ensure_collection()
    except Exception as exc:
        raise HTTPException(status_code=503, detail=f"Qdrant 연결 실패: {exc}") from exc
    started = photo_scanner.start()
    if not started:
        raise HTTPException(status_code=409, detail="A photo indexing scan is already running")
    return {"accepted": True, "state": photo_scanner.status()}


@app.get("/api/photos/status", tags=["photos"])
def photo_scan_status():
    return photo_scanner.status()


@app.get("/api/photos/stats", tags=["photos"])
def photo_stats():
    try:
        photo_engine.ensure_collection()
        count = photo_engine.client.count(photo_settings.collection, exact=True).count
        return {
            "photos": count,
            "photo_roots": runtime_settings.photo_roots,
            "collection": photo_settings.collection,
            "scan": photo_scanner.status(),
        }
    except Exception as exc:
        raise HTTPException(status_code=503, detail=str(exc)) from exc


@app.get("/api/photos/search", tags=["photos"])
async def photo_search(q: str = Query(min_length=1, max_length=300), limit: int = Query(default=30, ge=1, le=100)):
    limit = min(limit, photo_settings.search_limit)
    try:
        results = await asyncio.to_thread(photo_engine.search, q.strip(), limit)
        return {"query": q, "count": len(results), "results": results}
    except Exception as exc:
        raise HTTPException(status_code=503, detail=f"검색 실패: {exc}") from exc


def _photo_point(point_id: str):
    try:
        points = photo_engine.client.retrieve(collection_name=photo_settings.collection, ids=[point_id], with_payload=True, with_vectors=False)
    except Exception as exc:
        raise HTTPException(status_code=404, detail="사진을 찾을 수 없습니다.") from exc
    if not points:
        raise HTTPException(status_code=404, detail="사진을 찾을 수 없습니다.")
    return points[0]


def _safe_photo_path(raw_path: str) -> Path:
    if not Path(raw_path).is_absolute():
        raise HTTPException(status_code=403, detail="사진 경로는 절대경로여야 합니다.")
    path = Path(raw_path).resolve()
    allowed = False
    for root_value in runtime_settings.photo_roots:
        try:
            path.relative_to(Path(root_value).resolve())
            allowed = True
            break
        except ValueError:
            continue
    if not allowed:
        raise HTTPException(status_code=403, detail="허용되지 않은 경로입니다.")
    if not path.is_file():
        raise HTTPException(status_code=404, detail="원본 파일이 없습니다.")
    return path


@app.get("/api/photos/thumbnail/{point_id}", include_in_schema=False)
def photo_thumbnail(point_id: str):
    point = _photo_point(point_id)
    payload = point.payload or {}
    source = _safe_photo_path(str(payload.get("path", "")))
    cache_path = photo_settings.thumbnail_dir / thumbnail_cache_name(point_id, int(payload.get("mtime_ns", 0)))
    if not cache_path.exists():
        try:
            with Image.open(source) as original:
                image = ImageOps.exif_transpose(original).convert("RGB")
                image.thumbnail((photo_settings.thumbnail_size, photo_settings.thumbnail_size))
                image.save(cache_path, "JPEG", quality=84, optimize=True)
        except Exception as exc:
            raise HTTPException(status_code=415, detail="미리보기를 만들 수 없습니다.") from exc
    return FileResponse(cache_path, media_type="image/jpeg", headers={"Cache-Control": "public, max-age=31536000, immutable"})


@app.get("/api/photos/original/{point_id}", include_in_schema=False)
def photo_original(point_id: str):
    point = _photo_point(point_id)
    source = _safe_photo_path(str((point.payload or {}).get("path", "")))
    return FileResponse(source)
