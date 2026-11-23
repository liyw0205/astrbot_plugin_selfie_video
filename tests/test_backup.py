import json
import hashlib
import io
import os
import sqlite3
import sys
import zipfile
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[2]))

from astrbot_plugin_selfie_video.core.backup import VideoBackupError, VideoBackupManager
from astrbot_plugin_selfie_video.core.store import VideoJobStore


MP4 = b"\x00\x00\x00\x18ftypmp42" + b"\x00" * 24


def _successful_job(store, video_dir, job_id, video_url=""):
    path = video_dir / f"video_{job_id}.mp4"
    path.write_bytes(MP4)
    store.create(job_id, "user-1", "a quiet lake")
    assert store.update_if_status(
        job_id,
        ["queued"],
        status="succeeded",
        video_path=str(path),
        video_url=video_url,
    )
    return path


def _manager(data_dir):
    return VideoBackupManager(
        str(data_dir),
        str(data_dir / "videos"),
        str(data_dir / "video_jobs.sqlite3"),
    )


def test_backup_contains_task_snapshot_and_retained_media_but_not_config_or_source_url(tmp_path):
    data_dir = tmp_path / "plugin-data"
    video_dir = data_dir / "videos"
    video_dir.mkdir(parents=True)
    store = VideoJobStore(str(data_dir / "video_jobs.sqlite3"))
    _successful_job(store, video_dir, "JOB1", "https://cdn.invalid/signed?token=secret-value")
    (data_dir / "config.json").write_text('{"api_key":"never-export"}', encoding="utf-8")

    manager = _manager(data_dir)
    backup = manager.create_backup()
    archive_path = manager.backup_path(backup["id"])

    assert backup["media_count"] == 1
    assert manager.list_backups()[0]["id"] == backup["id"]
    with zipfile.ZipFile(archive_path) as archive:
        assert set(archive.namelist()) == {"manifest.json", "video_jobs.sqlite3", "videos/JOB1.mp4"}
        manifest = json.loads(archive.read("manifest.json"))
        assert manifest["format"] == "astrbot-selfie-video-data"
        snapshot = tmp_path / "snapshot.sqlite3"
        snapshot.write_bytes(archive.read("video_jobs.sqlite3"))
    with sqlite3.connect(snapshot) as connection:
        row = connection.execute("SELECT video_path, video_url FROM video_jobs WHERE id = 'JOB1'").fetchone()
    assert row == ("videos/JOB1.mp4", "")
    raw_archive = archive_path.read_bytes()
    assert b"secret-value" not in raw_archive
    assert b"never-export" not in raw_archive
    assert manager.backup_path(backup["id"]).stat().st_mode & 0o077 == 0


def test_empty_media_backup_restores_with_an_empty_video_directory(tmp_path):
    data_dir = tmp_path / "plugin-data"
    video_dir = data_dir / "videos"
    video_dir.mkdir(parents=True)
    store = VideoJobStore(str(data_dir / "video_jobs.sqlite3"))
    store.create("NOMEDIA", "user-1", "expired clip")
    assert store.update_if_status(
        "NOMEDIA", ["queued"], status="succeeded", video_path=str(video_dir / "missing.mp4")
    )
    manager = _manager(data_dir)

    backup = manager.create_backup()
    assert backup["media_count"] == 0
    manager.prepare_restore(backup["id"])
    assert manager.apply_pending_restore() is True
    assert (video_dir / "NOMEDIA.mp4").exists() is False
    assert VideoJobStore(str(data_dir / "video_jobs.sqlite3")).get("NOMEDIA")["video_path"] == ""


def test_only_five_local_backup_archives_are_retained(tmp_path):
    data_dir = tmp_path / "plugin-data"
    (data_dir / "videos").mkdir(parents=True)
    VideoJobStore(str(data_dir / "video_jobs.sqlite3"))
    manager = _manager(data_dir)

    created = [manager.create_backup()["id"] for _ in range(7)]

    assert len(manager.list_backups()) == 5
    assert len(list(manager.backup_dir.glob("video-data-*.zip"))) == 5
    assert all(manager.backup_path(backup_id).is_file() for backup_id in created[-5:])


def test_restore_replaces_history_and_rebases_media_paths(tmp_path):
    data_dir = tmp_path / "plugin-data"
    video_dir = data_dir / "videos"
    video_dir.mkdir(parents=True)
    database_path = data_dir / "video_jobs.sqlite3"
    store = VideoJobStore(str(database_path))
    _successful_job(store, video_dir, "RESTORE1")
    manager = _manager(data_dir)
    backup = manager.create_backup()

    old_video = _successful_job(store, video_dir, "OLD1")
    assert manager.prepare_restore(backup["id"])["restart_required"] is True
    assert manager.restore_pending is True
    assert manager.apply_pending_restore() is True

    restored_store = VideoJobStore(str(database_path))
    assert restored_store.get("RESTORE1")["video_path"] == str(video_dir / "RESTORE1.mp4")
    assert restored_store.get("RESTORE1")["status"] == "succeeded"
    assert (video_dir / "RESTORE1.mp4").exists()
    assert not old_video.exists()
    assert restored_store.get("OLD1") is None
    assert manager.restore_pending is False


def test_uploaded_archive_is_staged_and_applied_on_restart(tmp_path):
    source_root = tmp_path / "source"
    (source_root / "videos").mkdir(parents=True)
    source_store = VideoJobStore(str(source_root / "video_jobs.sqlite3"))
    _successful_job(source_store, source_root / "videos", "UPLOAD1")
    archive = _manager(source_root).create_backup()
    archive_bytes = _manager(source_root).backup_path(archive["id"]).read_bytes()

    target_root = tmp_path / "target"
    (target_root / "videos").mkdir(parents=True)
    manager = _manager(target_root)
    result = manager.prepare_restore_upload(io.BytesIO(archive_bytes))

    assert result["restart_required"] is True
    assert manager.apply_pending_restore() is True
    restored = VideoJobStore(str(target_root / "video_jobs.sqlite3")).get("UPLOAD1")
    assert restored["video_path"] == str(target_root / "videos" / "UPLOAD1.mp4")
    assert (target_root / "videos" / "UPLOAD1.mp4").is_file()


def test_invalid_restore_archive_does_not_replace_live_data(tmp_path):
    data_dir = tmp_path / "plugin-data"
    video_dir = data_dir / "videos"
    video_dir.mkdir(parents=True)
    store = VideoJobStore(str(data_dir / "video_jobs.sqlite3"))
    live_video = _successful_job(store, video_dir, "LIVE1")
    manager = _manager(data_dir)
    backup = manager.create_backup()
    bad_archive = tmp_path / "bad.zip"
    with zipfile.ZipFile(manager.backup_path(backup["id"])) as source, zipfile.ZipFile(bad_archive, "w") as target:
        for name in source.namelist():
            target.writestr(name, source.read(name))
        target.writestr("../escape.mp4", MP4)

    with pytest.raises(VideoBackupError, match="不安全"):
        manager.prepare_restore_file(bad_archive)

    assert manager.restore_pending is False
    assert live_video.exists()
    assert store.get("LIVE1") is not None


def test_restore_rejects_unrecognized_sqlite_triggers(tmp_path):
    data_dir = tmp_path / "plugin-data"
    video_dir = data_dir / "videos"
    video_dir.mkdir(parents=True)
    store = VideoJobStore(str(data_dir / "video_jobs.sqlite3"))
    _successful_job(store, video_dir, "SAFE1")
    manager = _manager(data_dir)
    backup = manager.create_backup()
    source_path = manager.backup_path(backup["id"])
    malicious_database = tmp_path / "malicious.sqlite3"
    with zipfile.ZipFile(source_path) as source:
        malicious_database.write_bytes(source.read("video_jobs.sqlite3"))
        manifest = json.loads(source.read("manifest.json"))
        media_payload = source.read("videos/SAFE1.mp4")
    connection = sqlite3.connect(malicious_database)
    try:
        connection.execute(
            "CREATE TRIGGER unexpected_trigger AFTER INSERT ON video_jobs BEGIN SELECT 1; END"
        )
        assert connection.execute(
            "SELECT type, name FROM sqlite_master WHERE name = 'unexpected_trigger'"
        ).fetchone() == ("trigger", "unexpected_trigger")
        connection.commit()
    finally:
        connection.close()
    database_payload = malicious_database.read_bytes()
    assert b"unexpected_trigger" in database_payload
    manifest["database"]["size"] = len(database_payload)
    manifest["database"]["sha256"] = hashlib.sha256(database_payload).hexdigest()
    crafted = tmp_path / "crafted.zip"
    with zipfile.ZipFile(crafted, "w", compression=zipfile.ZIP_STORED) as archive:
        archive.writestr("manifest.json", json.dumps(manifest))
        archive.writestr("video_jobs.sqlite3", database_payload)
        archive.writestr("videos/SAFE1.mp4", media_payload)

    with pytest.raises(VideoBackupError, match="未识别的对象"):
        manager.prepare_restore_file(crafted)
    assert manager.restore_pending is False
    assert VideoJobStore(str(data_dir / "video_jobs.sqlite3")).get("SAFE1") is not None


@pytest.mark.parametrize("fail_at", [2, 3, 4, 5])
def test_restore_transaction_recovers_original_data_after_interrupted_swap(tmp_path, monkeypatch, fail_at):
    data_dir = tmp_path / "plugin-data"
    video_dir = data_dir / "videos"
    video_dir.mkdir(parents=True)
    database_path = data_dir / "video_jobs.sqlite3"
    store = VideoJobStore(str(database_path))
    old_video = _successful_job(store, video_dir, "OLD1")
    manager = _manager(data_dir)
    backup = manager.create_backup()
    _successful_job(store, video_dir, "NEW1")
    manager.prepare_restore(backup["id"])

    original_replace = os.replace
    calls = 0

    def fail_after_video_install(source, destination):
        nonlocal calls
        calls += 1
        if calls == fail_at:
            raise OSError("simulated interruption")
        return original_replace(source, destination)

    monkeypatch.setattr("astrbot_plugin_selfie_video.core.backup.os.replace", fail_after_video_install)
    with pytest.raises(OSError, match="simulated"):
        manager.apply_pending_restore()
    monkeypatch.setattr("astrbot_plugin_selfie_video.core.backup.os.replace", original_replace)

    assert old_video.exists()
    assert (video_dir / "video_NEW1.mp4").exists()
    assert VideoJobStore(str(database_path)).get("NEW1") is not None
    assert manager.restore_pending is True
    assert manager.apply_pending_restore() is True
    assert VideoJobStore(str(database_path)).get("OLD1") is not None
    assert VideoJobStore(str(database_path)).get("NEW1") is None
    assert (video_dir / "OLD1.mp4").is_file()


def test_tampered_pending_restore_is_rejected_before_live_data_changes(tmp_path):
    data_dir = tmp_path / "plugin-data"
    video_dir = data_dir / "videos"
    video_dir.mkdir(parents=True)
    store = VideoJobStore(str(data_dir / "video_jobs.sqlite3"))
    live_video = _successful_job(store, video_dir, "LIVE1")
    manager = _manager(data_dir)
    backup = manager.create_backup()
    _successful_job(store, video_dir, "BACKUP1")
    manager.prepare_restore(backup["id"])
    (manager.pending_dir / "videos" / "LIVE1.mp4").write_bytes(b"tampered")

    with pytest.raises(VideoBackupError, match="校验失败"):
        manager.apply_pending_restore()

    assert live_video.exists()
    assert store.get("LIVE1") is not None
    assert store.get("BACKUP1") is not None
