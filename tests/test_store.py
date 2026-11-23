import sqlite3
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[2]))

from astrbot_plugin_selfie_video.core.store import VideoJobStore


def test_job_request_persistence_filters_and_conditional_transitions(tmp_path):
    store = VideoJobStore(str(tmp_path / "jobs.sqlite3"))
    store.create("A1", "user-1", "camera pans", {"duration": 7, "size": "16:9"})
    store.create("A2", "user-2", "a quiet street", {"duration": 5})

    row = store.get("A1", owner_id="user-1")
    assert row["request_json"] == '{"duration":7,"size":"16:9"}'
    assert store.get("A1", owner_id="user-2") is None
    assert store.update_if_status("A1", ["queued"], status="running") is True
    assert store.update_if_status("A1", ["queued"], status="cancelled") is False
    assert store.list_jobs(status="running", search="camera")[0]["id"] == "A1"
    assert store.list_jobs(status="failed") == []


def test_startup_migrates_existing_database_and_marks_interrupted_jobs(tmp_path):
    path = tmp_path / "old.sqlite3"
    connection = sqlite3.connect(path)
    connection.execute(
        """CREATE TABLE video_jobs (
            id TEXT PRIMARY KEY, owner_id TEXT NOT NULL, prompt TEXT NOT NULL,
            status TEXT NOT NULL, channel_name TEXT NOT NULL DEFAULT '',
            model TEXT NOT NULL DEFAULT '', video_path TEXT NOT NULL DEFAULT '',
            video_url TEXT NOT NULL DEFAULT '', error TEXT NOT NULL DEFAULT '',
            created_at REAL NOT NULL, updated_at REAL NOT NULL
        )"""
    )
    connection.execute(
        "INSERT INTO video_jobs(id, owner_id, prompt, status, created_at, updated_at) VALUES('B1', 'user', 'clip', 'running', 1, 1)"
    )
    connection.commit()
    connection.close()

    store = VideoJobStore(str(path))
    assert store.recover_interrupted() == 1
    row = store.get("B1")
    assert row["status"] == "failed"
    assert "状态未知" in row["error"]
    assert row["request_json"] == "{}"
