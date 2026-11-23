"""Bounded data-only snapshots for video task history and retained MP4s."""

from __future__ import annotations

import hashlib
import json
import os
from pathlib import Path, PurePosixPath
import re
import shutil
import sqlite3
import stat
import tempfile
import threading
import time
import uuid
import zipfile
from typing import Any, BinaryIO, Dict, Optional


BACKUP_FORMAT = "astrbot-selfie-video-data"
BACKUP_VERSION = 1
MAX_BACKUPS = 5
MAX_ARCHIVE_BYTES = 2 * 1024 * 1024 * 1024
MAX_UNCOMPRESSED_BYTES = 2 * 1024 * 1024 * 1024
MAX_VIDEO_FILES = 10000
MAX_ARCHIVE_ENTRIES = MAX_VIDEO_FILES + 2
MAX_MANIFEST_BYTES = 4 * 1024 * 1024
CHUNK_SIZE = 1024 * 1024
_BACKUP_ID = re.compile(r"^[a-f0-9]{32}$")
_JOB_ID = re.compile(r"^[A-Za-z0-9_-]{1,64}$")


class VideoBackupError(ValueError):
    """A backup operation was rejected or failed validation."""


class VideoBackupManager:
    def __init__(self, data_root: str, video_dir: str, database_path: str) -> None:
        self.data_root = Path(data_root).expanduser().resolve()
        self.video_dir = Path(video_dir).expanduser().absolute()
        self.database_path = Path(database_path).expanduser().absolute()
        self.backup_dir = self.data_root / "backups"
        if self.backup_dir.is_symlink():
            raise VideoBackupError("视频备份目录不能是符号链接")
        self.backup_dir.mkdir(mode=0o700, parents=True, exist_ok=True)
        os.chmod(self.backup_dir, 0o700)
        self.pending_dir = self.backup_dir / ".pending-restore"
        self.transaction_dir = self.backup_dir / ".restore-transaction"
        if self.pending_dir.is_symlink() or self.transaction_dir.is_symlink():
            raise VideoBackupError("恢复暂存路径不能是符号链接")
        self._lock = threading.RLock()

    @property
    def restore_pending(self) -> bool:
        if self.pending_dir.is_symlink() or (self.pending_dir.exists() and not self.pending_dir.is_dir()):
            raise VideoBackupError("待恢复路径不是安全的目录")
        return self.pending_dir.is_dir() and not self.pending_dir.is_symlink()

    @staticmethod
    def _remove(path: Path) -> None:
        if path.is_symlink() or path.is_file():
            path.unlink(missing_ok=True)
        elif path.exists():
            shutil.rmtree(path)

    @staticmethod
    def _hash_file(path: Path) -> tuple[str, int]:
        digest = hashlib.sha256()
        size = 0
        with path.open("rb") as handle:
            while True:
                chunk = handle.read(CHUNK_SIZE)
                if not chunk:
                    break
                digest.update(chunk)
                size += len(chunk)
        return digest.hexdigest(), size

    @staticmethod
    def _hash_stream(source: BinaryIO, destination: Path, limit: int) -> tuple[str, int]:
        digest = hashlib.sha256()
        size = 0
        with destination.open("xb") as output:
            while True:
                chunk = source.read(CHUNK_SIZE)
                if not chunk:
                    break
                size += len(chunk)
                if size > limit:
                    raise VideoBackupError("视频数据超过备份体积上限")
                digest.update(chunk)
                output.write(chunk)
            output.flush()
            os.fsync(output.fileno())
        os.chmod(destination, 0o600)
        return digest.hexdigest(), size

    @staticmethod
    def _snapshot_database(source_path: Path, destination_path: Path) -> None:
        source = sqlite3.connect(source_path, timeout=30)
        destination = sqlite3.connect(destination_path)
        try:
            source.backup(destination, pages=256, sleep=0.05)
        finally:
            destination.close()
            source.close()
        os.chmod(destination_path, 0o600)

    def _safe_video_source(self, raw_path: Any) -> Optional[Path]:
        raw = Path(str(raw_path or ""))
        if not str(raw_path or "") or raw.is_symlink() or self.video_dir.is_symlink():
            return None
        try:
            resolved = raw.resolve(strict=True)
            root = self.video_dir.resolve(strict=True)
            if os.path.commonpath((str(root), str(resolved))) != str(root):
                return None
            info = resolved.stat()
        except (OSError, ValueError):
            return None
        if not stat.S_ISREG(info.st_mode):
            return None
        try:
            with resolved.open("rb") as handle:
                header = handle.read(64)
        except OSError:
            return None
        return resolved if len(header) >= 12 and b"ftyp" in header else None

    def create_backup(self) -> Dict[str, Any]:
        with self._lock:
            existing = [
                int(match.group(1)[:16], 16)
                for path in self.backup_dir.glob("video-data-*.zip")
                if (match := re.fullmatch(r"video-data-([a-f0-9]{32})\.zip", path.name))
                and not path.is_symlink()
                and path.is_file()
            ]
            created_ns = max(time.time_ns(), max(existing, default=0) + 1)
            backup_id = f"{created_ns:016x}{uuid.uuid4().hex[:16]}"
            final_path = self.backup_dir / f"video-data-{backup_id}.zip"
            staged_archive = self.backup_dir / f".publish-{backup_id}.tmp"
            created_final = False
            try:
                with tempfile.TemporaryDirectory(prefix=".create-", dir=self.backup_dir) as work:
                    work_dir = Path(work)
                    database_snapshot = work_dir / "video_jobs.sqlite3"
                    self._snapshot_database(self.database_path, database_snapshot)
                    media_rows = []
                    total_size = database_snapshot.stat().st_size
                    connection = sqlite3.connect(database_snapshot)
                    connection.row_factory = sqlite3.Row
                    try:
                        connection.execute("PRAGMA secure_delete = ON")
                        rows = connection.execute(
                            "SELECT id, video_path FROM video_jobs "
                            "WHERE status = 'succeeded' AND video_path <> ''"
                        ).fetchall()
                        for row in rows:
                            job_id = str(row["id"])
                            source = self._safe_video_source(row["video_path"])
                            if not _JOB_ID.fullmatch(job_id) or source is None:
                                connection.execute(
                                    "UPDATE video_jobs SET video_path = '', video_url = '' WHERE id = ?",
                                    (job_id,),
                                )
                                continue
                            if len(media_rows) >= MAX_VIDEO_FILES:
                                raise VideoBackupError("视频数量超过备份上限")
                            relative_path = f"videos/{job_id}.mp4"
                            staged_path = work_dir / relative_path
                            staged_path.parent.mkdir(parents=True, exist_ok=True)
                            before = source.stat()
                            remaining = MAX_UNCOMPRESSED_BYTES - total_size
                            with source.open("rb") as input_file:
                                digest, copied_size = self._hash_stream(input_file, staged_path, remaining)
                            after = source.stat()
                            if copied_size != before.st_size or (before.st_size, before.st_mtime_ns) != (after.st_size, after.st_mtime_ns):
                                raise VideoBackupError("视频文件在备份期间发生变化，请稍后重试")
                            total_size += copied_size
                            connection.execute(
                                "UPDATE video_jobs SET video_path = ?, video_url = '' WHERE id = ?",
                                (relative_path, job_id),
                            )
                            media_rows.append({
                                "job_id": job_id,
                                "path": relative_path,
                                "size": copied_size,
                                "sha256": digest,
                            })
                        connection.execute("UPDATE video_jobs SET video_url = ''")
                        connection.commit()
                        connection.execute("VACUUM")
                    finally:
                        connection.close()
                    if total_size > MAX_UNCOMPRESSED_BYTES:
                        raise VideoBackupError("数据总量超过 2 GiB 备份上限")
                    database_hash, database_size = self._hash_file(database_snapshot)
                    manifest = {
                        "format": BACKUP_FORMAT,
                        "version": BACKUP_VERSION,
                        "created_at": int(time.time()),
                        "database": {
                            "path": "video_jobs.sqlite3",
                            "size": database_size,
                            "sha256": database_hash,
                        },
                        "media": media_rows,
                    }
                    temporary_archive = work_dir / "backup.zip"
                    with zipfile.ZipFile(temporary_archive, "w", compression=zipfile.ZIP_STORED) as archive:
                        archive.writestr(
                            "manifest.json",
                            json.dumps(manifest, ensure_ascii=False, separators=(",", ":")),
                        )
                        archive.write(database_snapshot, "video_jobs.sqlite3")
                        for item in media_rows:
                            archive.write(work_dir / item["path"], item["path"])
                    archive_size = temporary_archive.stat().st_size
                    if archive_size > MAX_ARCHIVE_BYTES:
                        raise VideoBackupError("备份压缩包超过 2 GiB 上限")
                    with temporary_archive.open("rb") as source, staged_archive.open("xb") as destination:
                        shutil.copyfileobj(source, destination, CHUNK_SIZE)
                        destination.flush()
                        os.fsync(destination.fileno())
                    os.chmod(staged_archive, 0o600)
                    os.replace(staged_archive, final_path)
                    created_final = True
                self._prune_backups()
                return {
                    "id": backup_id,
                    "size": final_path.stat().st_size,
                    "created_at": manifest["created_at"],
                    "media_count": len(media_rows),
                }
            except Exception:
                staged_archive.unlink(missing_ok=True)
                if created_final:
                    final_path.unlink(missing_ok=True)
                raise

    def _prune_backups(self) -> None:
        entries = []
        for path in self.backup_dir.glob("video-data-*.zip"):
            match = re.fullmatch(r"video-data-([a-f0-9]{32})\.zip", path.name)
            if not match or path.is_symlink() or not path.is_file():
                continue
            entries.append((match.group(1), path))
        entries.sort(reverse=True)
        for _, path in entries[MAX_BACKUPS:]:
            try:
                path.unlink(missing_ok=True)
            except OSError:
                continue

    def list_backups(self) -> list[Dict[str, Any]]:
        rows = []
        for path in self.backup_dir.glob("video-data-*.zip"):
            match = re.fullmatch(r"video-data-([a-f0-9]{32})\.zip", path.name)
            if not match or path.is_symlink() or not path.is_file():
                continue
            rows.append({"id": match.group(1), "size": path.stat().st_size, "created_at": int(path.stat().st_mtime)})
        rows.sort(key=lambda item: item["id"], reverse=True)
        return rows[:MAX_BACKUPS]

    def backup_path(self, backup_id: str) -> Path:
        if not _BACKUP_ID.fullmatch(str(backup_id or "")):
            raise VideoBackupError("备份编号无效")
        path = self.backup_dir / f"video-data-{backup_id}.zip"
        if path.is_symlink() or not path.is_file():
            raise VideoBackupError("备份不存在")
        return path

    def delete_backup(self, backup_id: str) -> None:
        with self._lock:
            self.backup_path(backup_id).unlink()

    def _validate_and_extract(self, archive_path: Path, destination: Path) -> Dict[str, Any]:
        if archive_path.is_symlink() or not archive_path.is_file() or archive_path.stat().st_size > MAX_ARCHIVE_BYTES:
            raise VideoBackupError("备份文件无效或超过 2 GiB 上限")
        try:
            archive = zipfile.ZipFile(archive_path, "r")
        except (OSError, zipfile.BadZipFile) as exc:
            raise VideoBackupError("备份不是有效的 ZIP 文件") from exc
        with archive:
            infos = archive.infolist()
            if len(infos) > MAX_ARCHIVE_ENTRIES:
                raise VideoBackupError("备份文件数量超过上限")
            info_by_name = {}
            total_size = 0
            for info in infos:
                name = info.filename
                pure = PurePosixPath(name)
                mode = info.external_attr >> 16
                if (
                    not name or "\\" in name or pure.is_absolute()
                    or any(part in {"", ".", ".."} for part in pure.parts)
                    or stat.S_ISLNK(mode) or info.is_dir() or info.flag_bits & 0x1
                    or name in info_by_name
                ):
                    raise VideoBackupError("备份包含不安全或重复的文件路径")
                if name not in {"manifest.json", "video_jobs.sqlite3"} and not re.fullmatch(
                    r"videos/[A-Za-z0-9_-]{1,64}\.mp4", name
                ):
                    raise VideoBackupError("备份包含未识别的文件")
                total_size += info.file_size
                if total_size > MAX_UNCOMPRESSED_BYTES or info.file_size < 0:
                    raise VideoBackupError("备份展开后的数据超过 2 GiB 上限")
                if name == "manifest.json" and info.file_size > MAX_MANIFEST_BYTES:
                    raise VideoBackupError("备份清单超过大小上限")
                info_by_name[name] = info
            if "manifest.json" not in info_by_name or "video_jobs.sqlite3" not in info_by_name:
                raise VideoBackupError("备份缺少清单或任务数据库")
            try:
                manifest = json.loads(archive.read("manifest.json"))
            except (ValueError, OSError, zipfile.BadZipFile) as exc:
                raise VideoBackupError("备份清单无效") from exc
            if not isinstance(manifest, dict) or manifest.get("format") != BACKUP_FORMAT or manifest.get("version") != BACKUP_VERSION:
                raise VideoBackupError("不支持此视频备份格式")
            database = manifest.get("database")
            media = manifest.get("media")
            if not isinstance(database, dict) or database.get("path") != "video_jobs.sqlite3" or not isinstance(media, list):
                raise VideoBackupError("备份清单缺少数据库或媒体索引")
            expected = {"manifest.json", "video_jobs.sqlite3"}
            media_by_job = {}
            for item in media:
                if not isinstance(item, dict):
                    raise VideoBackupError("备份媒体索引无效")
                job_id = str(item.get("job_id") or "")
                relative = f"videos/{job_id}.mp4"
                if not _JOB_ID.fullmatch(job_id) or item.get("path") != relative or job_id in media_by_job:
                    raise VideoBackupError("备份媒体路径无效")
                if relative not in info_by_name or info_by_name[relative].file_size != item.get("size"):
                    raise VideoBackupError("备份媒体文件与清单不匹配")
                expected.add(relative)
                media_by_job[job_id] = item
            if set(info_by_name) != expected:
                raise VideoBackupError("备份清单与归档文件不一致")
            destination.mkdir(mode=0o700, parents=True, exist_ok=False)
            (destination / "videos").mkdir(mode=0o700)
            extracted_total = 0
            for name in ("video_jobs.sqlite3", *sorted(expected - {"manifest.json", "video_jobs.sqlite3"})):
                target = destination / name
                target.parent.mkdir(mode=0o700, parents=True, exist_ok=True)
                digest = hashlib.sha256()
                size = 0
                with archive.open(name, "r") as source, target.open("xb") as output:
                    while True:
                        chunk = source.read(CHUNK_SIZE)
                        if not chunk:
                            break
                        size += len(chunk)
                        extracted_total += len(chunk)
                        if size > info_by_name[name].file_size or extracted_total > MAX_UNCOMPRESSED_BYTES:
                            raise VideoBackupError("备份实际展开的数据超过声明大小或 2 GiB 上限")
                        digest.update(chunk)
                        output.write(chunk)
                    output.flush()
                    os.fsync(output.fileno())
                os.chmod(target, 0o600)
                expected_meta = database if name == "video_jobs.sqlite3" else media_by_job[Path(name).stem]
                if size != expected_meta.get("size") or digest.hexdigest() != expected_meta.get("sha256"):
                    raise VideoBackupError("备份文件校验失败")
                if name.startswith("videos/"):
                    with target.open("rb") as handle:
                        if b"ftyp" not in handle.read(64):
                            raise VideoBackupError("备份包含无效的 MP4 视频文件")
            database_path = destination / "video_jobs.sqlite3"
            self._rebase_database(database_path, media_by_job)
            database_hash, database_size = self._hash_file(database_path)
            manifest["database"]["sha256"] = database_hash
            manifest["database"]["size"] = database_size
            manifest_path = destination / "manifest.json"
            with manifest_path.open("x", encoding="utf-8") as handle:
                json.dump(manifest, handle, ensure_ascii=False, separators=(",", ":"))
                handle.flush()
                os.fsync(handle.fileno())
            os.chmod(manifest_path, 0o600)
            return manifest

    def _rebase_database(
        self, path: Path, media_by_job: Dict[str, Any], *, rebase: bool = True
    ) -> None:
        try:
            connection = sqlite3.connect(path)
            connection.row_factory = sqlite3.Row
            integrity = connection.execute("PRAGMA integrity_check").fetchone()[0]
            if integrity != "ok":
                raise VideoBackupError("备份任务数据库完整性检查失败")
            columns = {row[1] for row in connection.execute("PRAGMA table_info(video_jobs)")}
            required_columns = {
                "id", "owner_id", "prompt", "request_json", "status", "channel_name",
                "model", "video_path", "video_url", "error", "created_at", "updated_at",
            }
            if columns != required_columns:
                raise VideoBackupError("备份任务数据库结构不兼容")
            objects = connection.execute(
                "SELECT type, name, sql FROM sqlite_master "
                "WHERE name NOT LIKE 'sqlite_%' ORDER BY type, name"
            ).fetchall()
            if any(row["type"] not in {"table", "index"} for row in objects):
                raise VideoBackupError("备份任务数据库包含未识别的对象")
            tables = {row["name"] for row in objects if row["type"] == "table"}
            indexes = {row["name"] for row in objects if row["type"] == "index"}
            if tables != {"video_jobs"} or indexes - {"idx_video_jobs_owner_created"}:
                raise VideoBackupError("备份任务数据库包含未识别的表或索引")
            for row in objects:
                if row["type"] == "table":
                    normalized_sql = re.sub(r"\s+", " ", str(row["sql"] or "").strip().lower())
                    expected_sql = (
                        "create table video_jobs ( id text primary key, owner_id text not null, "
                        "prompt text not null, request_json text not null default '{}', status text not null, "
                        "channel_name text not null default '', model text not null default '', "
                        "video_path text not null default '', video_url text not null default '', "
                        "error text not null default '', created_at real not null, updated_at real not null )"
                    )
                    if normalized_sql != expected_sql:
                        raise VideoBackupError("备份任务数据库表结构不兼容")
                if row["type"] == "index" and row["name"] == "idx_video_jobs_owner_created":
                    normalized_sql = re.sub(r"\s+", " ", str(row["sql"] or "").strip().lower())
                    if normalized_sql != "create index idx_video_jobs_owner_created on video_jobs(owner_id, created_at desc)":
                        raise VideoBackupError("备份任务数据库索引不兼容")
            rows = connection.execute("SELECT id, status, video_path FROM video_jobs").fetchall()
            found_media = set()
            for row in rows:
                job_id = str(row["id"])
                stored = str(row["video_path"] or "")
                if not stored:
                    continue
                expected_relative = f"videos/{job_id}.mp4"
                target_path = str(self.video_dir / f"{job_id}.mp4")
                allowed_path = expected_relative if rebase else target_path
                if row["status"] != "succeeded" or stored != allowed_path or job_id not in media_by_job:
                    raise VideoBackupError("备份数据库中的媒体路径与清单不一致")
                if rebase:
                    connection.execute(
                        "UPDATE video_jobs SET video_path = ?, video_url = '' WHERE id = ?",
                        (target_path, job_id),
                    )
                found_media.add(job_id)
            if found_media != set(media_by_job):
                raise VideoBackupError("备份数据库与媒体清单不一致")
            if rebase:
                connection.execute("UPDATE video_jobs SET video_url = ''")
                connection.commit()
            connection.close()
        except sqlite3.DatabaseError as exc:
            raise VideoBackupError("备份任务数据库无效") from exc
        finally:
            if "connection" in locals():
                connection.close()

    def _verify_pending_restore(self) -> bool:
        if self.pending_dir.is_symlink() or not self.pending_dir.is_dir():
            return False
        manifest_path = self.pending_dir / "manifest.json"
        database_path = self.pending_dir / "video_jobs.sqlite3"
        videos_dir = self.pending_dir / "videos"
        if (
            manifest_path.is_symlink() or not manifest_path.is_file()
            or manifest_path.stat().st_size > MAX_MANIFEST_BYTES
            or database_path.is_symlink() or not database_path.is_file()
            or videos_dir.is_symlink() or not videos_dir.is_dir()
        ):
            raise VideoBackupError("待恢复数据结构无效")
        try:
            manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
        except (OSError, ValueError) as exc:
            raise VideoBackupError("待恢复清单无效") from exc
        if (
            not isinstance(manifest, dict)
            or manifest.get("format") != BACKUP_FORMAT
            or manifest.get("version") != BACKUP_VERSION
            or not isinstance(manifest.get("database"), dict)
            or not isinstance(manifest.get("media"), list)
            or len(manifest["media"]) > MAX_VIDEO_FILES
        ):
            raise VideoBackupError("待恢复清单格式不受支持")
        database = manifest["database"]
        database_hash, database_size = self._hash_file(database_path)
        if (
            database.get("path") != "video_jobs.sqlite3"
            or database.get("size") != database_size
            or database.get("sha256") != database_hash
        ):
            raise VideoBackupError("待恢复任务数据库校验失败")
        media_by_job = {}
        total_size = database_size
        for item in manifest["media"]:
            if not isinstance(item, dict):
                raise VideoBackupError("待恢复媒体清单无效")
            job_id = str(item.get("job_id") or "")
            name = f"{job_id}.mp4"
            if not _JOB_ID.fullmatch(job_id) or item.get("path") != f"videos/{name}" or job_id in media_by_job:
                raise VideoBackupError("待恢复媒体路径无效")
            path = videos_dir / name
            if path.is_symlink() or not path.is_file():
                raise VideoBackupError("待恢复视频文件缺失或不安全")
            digest, size = self._hash_file(path)
            if size != item.get("size") or digest != item.get("sha256"):
                raise VideoBackupError("待恢复视频文件校验失败")
            total_size += size
            if total_size > MAX_UNCOMPRESSED_BYTES:
                raise VideoBackupError("待恢复数据超过 2 GiB 上限")
            with path.open("rb") as handle:
                if b"ftyp" not in handle.read(64):
                    raise VideoBackupError("待恢复文件不是有效的 MP4")
            media_by_job[job_id] = item
        allowed_files = {"manifest.json", "video_jobs.sqlite3"}
        actual_top = {item.name for item in self.pending_dir.iterdir()}
        actual_media = set()
        for item in videos_dir.iterdir():
            if item.is_symlink() or not item.is_file() or item.suffix.lower() != ".mp4":
                raise VideoBackupError("待恢复视频目录包含未识别的文件")
            actual_media.add(item.name)
        expected_media = {f"{job_id}.mp4" for job_id in media_by_job}
        if actual_top != allowed_files | {"videos"} or actual_media != expected_media:
            raise VideoBackupError("待恢复文件与清单不一致")
        self._rebase_database(database_path, media_by_job, rebase=False)
        return True

    def prepare_restore_file(self, source_path: Path) -> Dict[str, Any]:
        with self._lock:
            self._recover_transaction()
            if self.restore_pending:
                raise VideoBackupError("已有待应用的恢复，请重启插件或取消待恢复操作")
            temporary = self.backup_dir / f".pending-{uuid.uuid4().hex}"
            try:
                manifest = self._validate_and_extract(source_path, temporary)
                os.replace(temporary, self.pending_dir)
            except Exception:
                self._remove(temporary)
                raise
            return {"created_at": manifest.get("created_at"), "media_count": len(manifest.get("media", [])), "restart_required": True}

    def prepare_restore_upload(self, source: BinaryIO) -> Dict[str, Any]:
        temporary = self.backup_dir / f".upload-{uuid.uuid4().hex}.zip"
        digest_size = 0
        try:
            with temporary.open("xb") as destination:
                while True:
                    chunk = source.read(CHUNK_SIZE)
                    if not chunk:
                        break
                    digest_size += len(chunk)
                    if digest_size > MAX_ARCHIVE_BYTES:
                        raise VideoBackupError("上传备份超过 2 GiB 上限")
                    destination.write(chunk)
                destination.flush()
                os.fsync(destination.fileno())
            os.chmod(temporary, 0o600)
            return self.prepare_restore_file(temporary)
        finally:
            temporary.unlink(missing_ok=True)

    def prepare_restore(self, backup_id: str) -> Dict[str, Any]:
        return self.prepare_restore_file(self.backup_path(backup_id))

    def _write_journal(self, phase: str, *, old_video_existed: bool, old_database_existed: bool) -> None:
        journal = self.transaction_dir / "journal.json"
        temporary = self.transaction_dir / "journal.tmp"
        with temporary.open("w", encoding="utf-8") as handle:
            json.dump({
                "phase": phase,
                "old_video_existed": old_video_existed,
                "old_database_existed": old_database_existed,
            }, handle)
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(temporary, journal)

    def _recover_transaction(self) -> None:
        if self.transaction_dir.is_symlink():
            raise VideoBackupError("恢复事务目录不能是符号链接")
        if not self.transaction_dir.exists():
            return
        journal = self.transaction_dir / "journal.json"
        if not journal.is_file() or journal.is_symlink():
            old_video = self.transaction_dir / "videos-old"
            old_database = self.transaction_dir / "video_jobs-old.sqlite3"
            pending_database = self.pending_dir / "video_jobs.sqlite3"
            if old_video.exists() or (old_database.exists() and not pending_database.exists()):
                raise VideoBackupError("恢复事务日志缺失但可能已替换数据，已停止自动恢复")
            self._remove(self.transaction_dir)
            return
        try:
            state = json.loads(journal.read_text(encoding="utf-8"))
            phase = state.get("phase")
            old_video_existed = state.get("old_video_existed")
            old_database_existed = state.get("old_database_existed")
        except (OSError, ValueError, AttributeError) as exc:
            raise VideoBackupError("恢复事务日志损坏，已停止自动恢复") from exc
        if (
            phase not in {"swapping", "committed"}
            or not isinstance(old_video_existed, bool)
            or not isinstance(old_database_existed, bool)
        ):
            raise VideoBackupError("恢复事务阶段无效，已停止自动恢复")
        if phase == "committed":
            self._remove(self.pending_dir)
            self._remove(self.transaction_dir)
            return
        previous_video = self.transaction_dir / "videos-old"
        pending_video = self.pending_dir / "videos"
        if old_video_existed and not previous_video.exists() and not self.video_dir.exists():
            raise VideoBackupError("恢复回滚缺少原视频目录")
        if previous_video.exists():
            if self.video_dir.exists():
                if pending_video.exists():
                    raise VideoBackupError("恢复回滚时视频暂存位置冲突")
                os.replace(self.video_dir, pending_video)
            os.replace(previous_video, self.video_dir)
        elif not old_video_existed and phase == "swapping":
            if self.video_dir.exists():
                if pending_video.exists():
                    raise VideoBackupError("恢复回滚时视频暂存位置冲突")
                os.replace(self.video_dir, pending_video)
        old_database = self.transaction_dir / "video_jobs-old.sqlite3"
        pending_database = self.pending_dir / "video_jobs.sqlite3"
        if not pending_database.exists() and self.database_path.exists():
            if old_database_existed and not old_database.is_file():
                raise VideoBackupError("恢复回滚缺少原任务数据库")
            os.replace(self.database_path, pending_database)
        if old_database.is_file():
            self.database_path.parent.mkdir(parents=True, exist_ok=True)
            self.database_path.unlink(missing_ok=True)
            os.replace(old_database, self.database_path)
        elif old_database_existed:
            raise VideoBackupError("恢复事务缺少原任务数据库，已停止自动恢复")
        elif not old_database_existed:
            self.database_path.unlink(missing_ok=True)
        for suffix in ("-wal", "-shm"):
            Path(f"{self.database_path}{suffix}").unlink(missing_ok=True)
        self._remove(self.transaction_dir)

    def apply_pending_restore(self) -> bool:
        with self._lock:
            self._recover_transaction()
            if not self.restore_pending:
                return False
            self._verify_pending_restore()
            pending_database = self.pending_dir / "video_jobs.sqlite3"
            pending_videos = self.pending_dir / "videos"
            if self.video_dir.is_symlink():
                raise VideoBackupError("当前视频目录不能是符号链接")
            old_video_existed = self.video_dir.exists()
            old_database_existed = self.database_path.exists()
            if self.database_path.is_symlink():
                raise VideoBackupError("当前任务数据库不能是符号链接")
            self.transaction_dir.mkdir(mode=0o700, exist_ok=False)
            try:
                if old_video_existed and (self.video_dir.is_symlink() or not self.video_dir.is_dir()):
                    raise VideoBackupError("当前视频目录不是安全的普通目录")
                if old_database_existed:
                    self._snapshot_database(self.database_path, self.transaction_dir / "video_jobs-old.sqlite3")
                self._write_journal(
                    "swapping",
                    old_video_existed=old_video_existed,
                    old_database_existed=old_database_existed,
                )
                if old_video_existed:
                    os.replace(self.video_dir, self.transaction_dir / "videos-old")
                os.replace(pending_videos, self.video_dir)
                self.database_path.parent.mkdir(parents=True, exist_ok=True)
                self.database_path.unlink(missing_ok=True)
                os.replace(pending_database, self.database_path)
                for suffix in ("-wal", "-shm"):
                    Path(f"{self.database_path}{suffix}").unlink(missing_ok=True)
                self._write_journal(
                    "committed",
                    old_video_existed=old_video_existed,
                    old_database_existed=old_database_existed,
                )
            except Exception:
                self._recover_transaction()
                raise
            self._remove(self.pending_dir)
            self._remove(self.transaction_dir)
            return True
