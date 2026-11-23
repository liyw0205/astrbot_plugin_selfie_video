import copy
import io
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[2]))

from astrbot_plugin_selfie_video.core.config import normalize_config
from astrbot_plugin_selfie_video import main as plugin_module
from astrbot_plugin_selfie_video.webui.server import VideoWebServer


class FakeStore:
    def list_all_recent(self, limit):
        return [{"id": "A1", "status": "succeeded", "video_path": "/private/video.mp4", "video_url": "https://cdn.invalid/private.mp4"}]

    def get(self, job_id):
        return None

    def list_jobs(self, **kwargs):
        rows = self.list_all_recent(kwargs.get("limit", 100))
        status = kwargs.get("status")
        search = kwargs.get("search", "").lower()
        return [
            row for row in rows
            if (not status or row.get("status") == status)
            and (not search or search in row.get("id", "").lower() or search in row.get("prompt", "").lower())
        ]

    def cancel_job(self, *args, **kwargs):
        return True


class FakePlugin:
    def __init__(self, tmp_path):
        self.plugin_root = str(Path(__file__).resolve().parents[1])
        self.video_dir = str(tmp_path)
        self.settings = normalize_config({
            "web": {"token": "panel-secret"},
            "video_channels": [{
                "name": "primary",
                "enabled": True,
                "provider_type": "agnes",
                "base_url": "https://api.example/v1",
                "api_key": "test-api-key",
                "model": "agnes-video-2.0",
            }],
        })
        self.store = FakeStore()
        self.video_backup = FakeVideoBackup()

    def get_settings_snapshot(self):
        return copy.deepcopy(self.settings)

    def replace_settings(self, settings):
        self.settings = normalize_config(settings)

    def cancel_job(self, job_id):
        return job_id == "A1"

    def retry_job(self, job_id, **kwargs):
        if job_id != "A1":
            raise ValueError("missing")
        return "B2"

    def submit_job(self, owner_id, request, targets):
        self.submitted = (owner_id, request, targets)
        return "C3"

    def prepare_video_backup_restore(self, *, backup_id="", upload=None):
        self.restore_request = (backup_id, upload)
        return {"restart_required": True, "media_count": 1}


class FakeVideoBackup:
    restore_pending = False

    def list_backups(self):
        return [{"id": "a" * 32, "size": 2048, "created_at": 1700000000}]

    def create_backup(self):
        return {"id": "b" * 32, "size": 2048, "created_at": 1700000001, "media_count": 1}

    def backup_path(self, backup_id):
        if backup_id != "a" * 32:
            raise ValueError("备份不存在")
        return Path(__file__)

    def delete_backup(self, backup_id):
        if backup_id != "a" * 32:
            raise ValueError("备份不存在")


def test_config_api_requires_token_and_masks_secrets(tmp_path):
    client = VideoWebServer(FakePlugin(tmp_path)).app.test_client()
    assert client.get("/api/config").status_code == 401
    assert client.get("/api/config?token=panel-secret").status_code == 401
    response = client.get("/api/config", headers={"Authorization": "Bearer panel-secret"})
    assert response.status_code == 200
    body = response.get_json()
    assert body["video"]["max_saved_videos"] == 100
    assert body["video_channels"][0]["has_api_key"] is True
    assert body["video_channels"][0]["api_key"] == ""
    assert "test-api-key" not in response.get_data(as_text=True)
    assert "panel-secret" not in response.get_data(as_text=True)


def test_dashboard_cross_origin_preflight_allows_bearer_auth_headers(tmp_path):
    client = VideoWebServer(FakePlugin(tmp_path)).app.test_client()
    response = client.options(
        "/api/config",
        headers={
            "Origin": "https://dashboard.example",
            "Access-Control-Request-Method": "GET",
            "Access-Control-Request-Headers": "authorization,content-type",
        },
    )
    assert response.status_code == 204
    assert response.headers["Access-Control-Allow-Origin"] == "https://dashboard.example"
    assert "Authorization" in response.headers["Access-Control-Allow-Headers"]


def test_blank_key_update_preserves_secret_and_validates_protocol(tmp_path):
    plugin = FakePlugin(tmp_path)
    client = VideoWebServer(plugin).app.test_client()
    headers = {"Authorization": "Bearer panel-secret"}
    body = {
        "video": {"default_duration": 7},
        "video_channels": [{
            "name": "primary",
            "enabled": True,
            "provider_type": "agnes",
            "base_url": "https://api.example/v1",
            "api_key": "",
            "api_keys": [],
            "enabled_models": ["agnes-video-2.0"],
        }],
    }
    response = client.put("/api/config", headers=headers, json=body)
    assert response.status_code == 200
    assert plugin.settings["video_channels"][0]["api_key"] == "test-api-key"
    assert plugin.settings["video"]["default_duration"] == 7

    channel_id = response.get_json()["config"]["video_channels"][0]["channel_id"]
    body["video_channels"][0]["channel_id"] = channel_id
    body["video_channels"][0]["name"] = "renamed"
    response = client.put("/api/config", headers=headers, json=body)
    assert response.status_code == 200
    assert plugin.settings["video_channels"][0]["api_key"] == "test-api-key"

    body["video_channels"][0]["provider_type"] = "unknown"
    response = client.put("/api/config", headers=headers, json=body)
    assert response.status_code == 400

    body["video_channels"][0]["provider_type"] = "agnes"
    body["video_channels"][0]["model_provider_types"] = {"missing-model": "openai_video"}
    response = client.put("/api/config", headers=headers, json=body)
    assert response.status_code == 400


def test_permission_settings_can_be_updated_without_replacing_channels(tmp_path):
    plugin = FakePlugin(tmp_path)
    client = VideoWebServer(plugin).app.test_client()
    response = client.put(
        "/api/config",
        headers={"Authorization": "Bearer panel-secret"},
        json={"permission": {"usable_users": "user-1,user-2", "blocked_users": "user-3"}},
    )
    assert response.status_code == 200
    assert plugin.settings["permission"] == {
        "usable_users": "user-1,user-2",
        "blocked_users": "user-3",
    }
    assert plugin.settings["video_channels"][0]["api_key"] == "test-api-key"


def test_job_api_does_not_return_local_paths_or_source_urls(tmp_path):
    client = VideoWebServer(FakePlugin(tmp_path)).app.test_client()
    response = client.get("/api/jobs", headers={"Authorization": "Bearer panel-secret"})
    assert response.status_code == 200
    text = response.get_data(as_text=True)
    assert "/private/video.mp4" not in text
    assert "cdn.invalid" not in text
    assert "request_json" not in text
    assert response.get_json()["jobs"][0]["media_available"] is False


def test_job_actions_require_token_and_return_state(tmp_path):
    client = VideoWebServer(FakePlugin(tmp_path)).app.test_client()
    assert client.post("/api/jobs/A1/cancel").status_code == 401
    headers = {"Authorization": "Bearer panel-secret"}
    assert client.post("/api/jobs/A1/cancel", headers=headers).status_code == 200
    retry = client.post("/api/jobs/A1/retry", headers=headers)
    assert retry.status_code == 202
    assert retry.get_json()["job_id"] == "B2"
    assert client.get("/api/jobs?status=failed", headers=headers).get_json()["jobs"] == []


def test_web_test_endpoint_enqueues_instead_of_blocking_for_generation(tmp_path):
    plugin = FakePlugin(tmp_path)
    client = VideoWebServer(plugin).app.test_client()
    response = client.post(
        "/api/test",
        headers={"Authorization": "Bearer panel-secret"},
        json={"channel_name": "primary", "model": "agnes-video-2.0", "prompt": "a short clip", "duration": 5},
    )
    assert response.status_code == 202
    assert response.get_json()["job_id"] == "C3"
    owner, request_data, targets = plugin.submitted
    assert owner == "web-admin"
    assert request_data.prompt == "a short clip"
    assert targets[0].provider_type == "agnes"


def test_web_test_endpoint_respects_global_video_disable_setting(tmp_path):
    plugin = FakePlugin(tmp_path)
    plugin.settings["video"]["enable"] = False
    client = VideoWebServer(plugin).app.test_client()
    response = client.post(
        "/api/test",
        headers={"Authorization": "Bearer panel-secret"},
        json={"channel_name": "primary", "model": "agnes-video-2.0", "prompt": "a short clip", "duration": 5},
    )
    assert response.status_code == 409
    assert response.get_json()["error"] == "视频生成当前已关闭。"
    assert not hasattr(plugin, "submitted")


def test_media_download_mode_is_authenticated_and_confined(tmp_path):
    plugin = FakePlugin(tmp_path)
    video = Path(tmp_path) / "clip.mp4"
    video.write_bytes(b"\x00\x00\x00\x18ftypmp42")

    class MediaStore(FakeStore):
        def get(self, job_id):
            return {"status": "succeeded", "video_path": str(video)}

    plugin.store = MediaStore()
    client = VideoWebServer(plugin).app.test_client()
    url = "/api/media/A1?download=1"
    assert client.get(url).status_code == 401
    response = client.get(url, headers={"Authorization": "Bearer panel-secret"})
    assert response.status_code == 200
    assert response.headers["Content-Disposition"].startswith("attachment;")


def test_backup_api_authenticates_listing_create_download_delete_and_restore(tmp_path):
    plugin = FakePlugin(tmp_path)
    client = VideoWebServer(plugin).app.test_client()
    backup_id = "a" * 32
    assert client.get("/api/backups").status_code == 401
    assert client.post("/api/backups").status_code == 401
    assert client.post(f"/api/backups/{backup_id}/restore").status_code == 401

    headers = {"Authorization": "Bearer panel-secret"}
    listing = client.get("/api/backups", headers=headers)
    assert listing.get_json()["items"][0]["id"] == backup_id
    created = client.post("/api/backups", headers=headers, json={})
    assert created.status_code == 201
    assert created.get_json()["backup"]["media_count"] == 1
    downloaded = client.get(f"/api/backups/{backup_id}/download", headers=headers)
    assert downloaded.status_code == 200
    assert downloaded.mimetype == "application/zip"
    restored = client.post(f"/api/backups/{backup_id}/restore", headers=headers)
    assert restored.status_code == 202
    assert restored.get_json()["restore"]["restart_required"] is True
    assert plugin.restore_request[0] == backup_id
    uploaded = client.post(
        "/api/backups/upload/restore",
        headers=headers,
        data={"file": (io.BytesIO(b"zip"), "backup.zip")},
        content_type="multipart/form-data",
    )
    assert uploaded.status_code == 202
    assert plugin.restore_request[1] is not None
    assert client.delete(f"/api/backups/{backup_id}", headers=headers).status_code == 200


def test_backup_upload_route_prepares_real_plugin_restore_for_startup(tmp_path, monkeypatch):
    monkeypatch.setattr(plugin_module, "get_astrbot_data_path", lambda: str(tmp_path))
    config = {"web": {"enable": False, "token": "panel-secret"}}
    plugin = plugin_module.SelfieVideoPlugin(context=None, config=config)
    client = VideoWebServer(plugin).app.test_client()
    headers = {"Authorization": "Bearer panel-secret"}

    created = client.post("/api/backups", headers=headers, json={})
    assert created.status_code == 201
    backup_id = created.get_json()["backup"]["id"]
    downloaded = client.get(f"/api/backups/{backup_id}/download", headers=headers)
    assert downloaded.status_code == 200
    upload = client.post(
        "/api/backups/upload/restore",
        headers=headers,
        data={"file": (io.BytesIO(downloaded.data), "video-backup.zip")},
        content_type="multipart/form-data",
    )
    assert upload.status_code == 202
    assert plugin.video_backup.restore_pending is True

    reloaded = plugin_module.SelfieVideoPlugin(context=None, config=config)

    assert reloaded.video_backup.restore_pending is False
    assert reloaded.store.list_all_recent() == []
