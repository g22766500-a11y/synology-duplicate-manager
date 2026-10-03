import hashlib
import os
import threading
import time
from datetime import datetime, timezone
from typing import Iterator, Literal

from .config import EXCLUDE_AT_PREFIX, EXCLUDE_DIRS, HASH_CHUNK_SIZE
from .database import connect
from .runtime_settings import runtime_settings

ScanMode = Literal["quick", "full", "force_full"]
VALID_SCAN_MODES: set[str] = {"quick", "full", "force_full"}

_scan_lock = threading.Lock()
_state_lock = threading.Lock()
_state = {
    "running": False,
    "mode": None,
    "run_id": None,
    "started_at": None,
    "finished_at": None,
    "phase": "idle",
    "files_seen": 0,
    "files_hashed": 0,
    "hash_candidates": 0,
    "dirs_skipped": 0,
    "errors": 0,
    "current_path": None,
    "message": "idle",
}


def _iso_now() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


def get_state() -> dict:
    with _state_lock:
        return dict(_state)


def _update(**kwargs) -> None:
    with _state_lock:
        _state.update(kwargs)


def _inc(key: str, amount: int = 1) -> None:
    with _state_lock:
        _state[key] += amount


def _should_skip_dir(name: str) -> bool:
    if EXCLUDE_AT_PREFIX and name.startswith("@"):
        return True
    return name in EXCLUDE_DIRS


def iter_files(root: str) -> Iterator[str]:
    for current, dirs, files in os.walk(root, topdown=True, followlinks=False):
        before = len(dirs)
        dirs[:] = [d for d in dirs if not _should_skip_dir(d)]
        skipped = before - len(dirs)
        if skipped:
            _inc("dirs_skipped", skipped)

        for name in files:
            yield os.path.join(current, name)


def md5_file(path: str) -> str:
    digest = hashlib.md5()
    with open(path, "rb", buffering=0) as fh:
        while chunk := fh.read(HASH_CHUNK_SIZE):
            digest.update(chunk)
    return digest.hexdigest()


def start_scan(mode: ScanMode = "quick") -> tuple[bool, dict]:
    if mode not in VALID_SCAN_MODES:
        raise ValueError(f"unsupported scan mode: {mode}")

    # Prevent two requests from both starting a scan before the worker updates state.
    with _state_lock:
        if _state["running"]:
            return False, dict(_state)

        _state.update(
            running=True,
            mode=mode,
            run_id=None,
            started_at=None,
            finished_at=None,
            phase="starting",
            files_seen=0,
            files_hashed=0,
            hash_candidates=0,
            dirs_skipped=0,
            errors=0,
            current_path=None,
            message="starting scan",
        )
        state = dict(_state)

    thread = threading.Thread(
        target=_scan_worker,
        args=(mode,),
        daemon=True,
        name=f"duplicate-scan-{mode}",
    )
    thread.start()
    return True, state


def _candidate_rows(conn, mode: ScanMode):
    if mode == "quick":
        _update(
            phase="hashing",
            message="hashing same-size duplicate candidates",
            current_path=None,
        )
        return conn.execute(
            """
            SELECT f.path
            FROM files f
            JOIN (
                SELECT size
                FROM files
                WHERE error IS NULL
                GROUP BY size
                HAVING COUNT(*) > 1
            ) d ON d.size = f.size
            WHERE f.error IS NULL
              AND f.md5 IS NULL
            ORDER BY f.size DESC, f.path
            """
        ).fetchall()

    if mode == "full":
        _update(
            phase="hashing",
            message="hashing all files without MD5",
            current_path=None,
        )
        return conn.execute(
            """
            SELECT path
            FROM files
            WHERE error IS NULL
              AND md5 IS NULL
            ORDER BY size DESC, path
            """
        ).fetchall()

    _update(
        phase="hashing",
        message="recalculating MD5 for all files",
        current_path=None,
    )
    return conn.execute(
        """
        SELECT path
        FROM files
        WHERE error IS NULL
        ORDER BY size DESC, path
        """
    ).fetchall()


def _scan_worker(mode: ScanMode) -> None:
    if not _scan_lock.acquire(blocking=False):
        _update(
            running=False,
            phase="idle",
            message="another scan worker is already running",
        )
        return

    started_at = _iso_now()
    _update(
        running=True,
        mode=mode,
        run_id=None,
        started_at=started_at,
        finished_at=None,
        phase="metadata",
        files_seen=0,
        files_hashed=0,
        hash_candidates=0,
        dirs_skipped=0,
        errors=0,
        current_path=None,
        message="collecting file metadata",
    )

    conn = connect()
    try:
        cur = conn.execute(
            "INSERT INTO scan_runs(started_at,status,message) VALUES(?,?,?)",
            (started_at, "running", f"{mode}: collecting file metadata"),
        )
        run_id = cur.lastrowid
        conn.commit()
        _update(run_id=run_id)

        scan_token = time.time_ns()
        scan_roots = runtime_settings.scan_roots

        missing = [root for root in scan_roots if not os.path.isdir(root)]
        if missing:
            raise RuntimeError("스캔 폴더를 찾을 수 없습니다. 마운트와 폴더 설정을 확인하세요: " + ", ".join(missing))

        for root in scan_roots:
            if not os.path.isdir(root):
                _inc("errors")
                continue

            for path in iter_files(root):
                _inc("files_seen")
                _update(current_path=path)

                try:
                    st = os.stat(path, follow_symlinks=False)
                    if not os.path.isfile(path):
                        continue

                    row = conn.execute(
                        "SELECT size,mtime_ns FROM files WHERE path=?",
                        (path,),
                    ).fetchone()

                    if (
                        row
                        and row["size"] == st.st_size
                        and row["mtime_ns"] == st.st_mtime_ns
                    ):
                        # Unchanged file: preserve existing MD5.
                        conn.execute(
                            "UPDATE files SET root=?,last_scanned=?,error=NULL WHERE path=?",
                            (root, scan_token, path),
                        )
                    else:
                        # New or changed file: clear MD5 so it can be recalculated.
                        conn.execute(
                            """
                            INSERT INTO files(path,root,size,mtime_ns,md5,last_scanned,error)
                            VALUES(?,?,?,?,NULL,?,NULL)
                            ON CONFLICT(path) DO UPDATE SET
                                root=excluded.root,
                                size=excluded.size,
                                mtime_ns=excluded.mtime_ns,
                                md5=NULL,
                                last_scanned=excluded.last_scanned,
                                error=NULL
                            """,
                            (path, root, st.st_size, st.st_mtime_ns, scan_token),
                        )
                except Exception as exc:
                    _inc("errors")
                    conn.execute(
                        """
                        INSERT INTO files(path,root,size,mtime_ns,md5,last_scanned,error)
                        VALUES(?,?,0,0,NULL,?,?)
                        ON CONFLICT(path) DO UPDATE SET
                            root=excluded.root,
                            last_scanned=excluded.last_scanned,
                            error=excluded.error
                        """,
                        (path, root, scan_token, str(exc)),
                    )

                if get_state()["files_seen"] % 500 == 0:
                    conn.commit()

        conn.commit()

        placeholders = ",".join("?" for _ in scan_roots)
        if scan_roots:
            conn.execute(
                f"DELETE FROM files WHERE root IN ({placeholders}) AND last_scanned<>?",
                (*scan_roots, scan_token),
            )
            conn.execute(
                f"DELETE FROM files WHERE root NOT IN ({placeholders})",
                tuple(scan_roots),
            )
            conn.commit()

        candidates = _candidate_rows(conn, mode)
        _update(hash_candidates=len(candidates))

        for row in candidates:
            path = row["path"]
            _update(current_path=path)

            try:
                before = os.stat(path, follow_symlinks=False)
                digest = md5_file(path)
                after = os.stat(path, follow_symlinks=False)

                if (
                    before.st_size != after.st_size
                    or before.st_mtime_ns != after.st_mtime_ns
                ):
                    raise RuntimeError("file changed while hashing")

                conn.execute(
                    "UPDATE files SET md5=?,size=?,mtime_ns=?,error=NULL WHERE path=?",
                    (digest, after.st_size, after.st_mtime_ns, path),
                )
                _inc("files_hashed")
            except Exception as exc:
                _inc("errors")
                conn.execute(
                    "UPDATE files SET md5=NULL,error=? WHERE path=?",
                    (str(exc), path),
                )

            if get_state()["files_hashed"] % 25 == 0:
                conn.commit()

        conn.commit()

        finished_at = _iso_now()
        state = get_state()
        conn.execute(
            """
            UPDATE scan_runs
            SET finished_at=?,status='completed',files_seen=?,files_hashed=?,errors=?,message=?
            WHERE id=?
            """,
            (
                finished_at,
                state["files_seen"],
                state["files_hashed"],
                state["errors"],
                f"{mode}: done",
                run_id,
            ),
        )
        conn.commit()

        _update(
            running=False,
            finished_at=finished_at,
            phase="idle",
            current_path=None,
            message="done",
        )
    except Exception as exc:
        finished_at = _iso_now()
        state = get_state()

        if state.get("run_id"):
            conn.execute(
                """
                UPDATE scan_runs
                SET finished_at=?,status='failed',files_seen=?,files_hashed=?,errors=?,message=?
                WHERE id=?
                """,
                (
                    finished_at,
                    state["files_seen"],
                    state["files_hashed"],
                    state["errors"] + 1,
                    str(exc),
                    state["run_id"],
                ),
            )
            conn.commit()

        _update(
            running=False,
            finished_at=finished_at,
            phase="idle",
            current_path=None,
            errors=state["errors"] + 1,
            message=f"failed: {exc}",
        )
    finally:
        conn.close()
        _scan_lock.release()
