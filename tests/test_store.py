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


def test_video_retention_selects_old_success_paths_and_clears_conditionally(tmp_path):
    store = VideoJobStore(str(tmp_path / "jobs.sqlite3"))
    for index in range(3):
        job_id = f"V{index}"
        path = str(tmp_path / f"video-{index}.mp4")
        store.create(job_id, "user", f"clip {index}")
        assert store.update_if_status(
            job_id, ["queued"], status="succeeded", video_path=path
        ) is True

    with sqlite3.connect(store.path) as connection:
        for index in range(3):
            connection.execute(
                "UPDATE video_jobs SET updated_at = ? WHERE id = ?",
                (index + 1, f"V{index}"),
            )

    stale = store.list_video_paths_to_prune(keep=1)
    assert [row["id"] for row in stale] == ["V1", "V0"]
    assert store.clear_video_path("V0", stale[-1]["video_path"]) is True
    assert store.clear_video_path("V0", stale[-1]["video_path"]) is False
    assert store.get("V0")["video_path"] == ""
