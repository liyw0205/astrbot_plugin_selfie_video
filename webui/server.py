"""Authenticated Flask management API for channels, jobs, and MP4 results."""

from __future__ import annotations

import copy
import hashlib
import hmac
import os
import threading
from typing import Any, Dict
from urllib.parse import urlsplit

from flask import Flask, jsonify, request, send_file
from werkzeug.serving import BaseWSGIServer, make_server

from ..core.config import build_video_targets, normalize_config
from ..generation.video import VideoGenerateRequest, normalize_video_provider_type


class VideoWebServer:
    def __init__(self, plugin: Any) -> None:
        self.plugin = plugin
        self.app = Flask(__name__)
        self._server: BaseWSGIServer | None = None
        self._thread: threading.Thread | None = None
        self._register_routes()

    def _token(self) -> str:
        return str(self.plugin.get_settings_snapshot().get("web", {}).get("token") or "")

    def _authorized(self) -> bool:
        expected = self._token()
        supplied = str(request.headers.get("Authorization") or "")
        if supplied.lower().startswith("bearer "):
            supplied = supplied[7:].strip()
        else:
            supplied = str(request.headers.get("X-Web-Token") or "")
        return bool(expected) and hmac.compare_digest(expected, supplied)

    def _require_auth(self):
        if not self._authorized():
            return jsonify({"error": "需要有效的 Web Token。"}), 401
        return None

    @staticmethod
    def _masked_settings(settings: Dict[str, Any]) -> Dict[str, Any]:
        result = copy.deepcopy(settings)
        for channel in result.get("video_channels", []):
            if not isinstance(channel, dict):
                continue
            if not channel.get("channel_id"):
                name = str(channel.get("name") or "")
                channel["channel_id"] = hashlib.sha256(name.encode("utf-8")).hexdigest()[:16]
            keys = channel.pop("api_keys", []) or []
            key = str(channel.pop("api_key", "") or "")
            channel["has_api_key"] = bool(keys or key)
            channel["api_key"] = ""
            channel["api_keys"] = []
        result.get("web", {}).pop("token", None)
        return result

    def _merge_channel_keys(self, incoming: Any, previous: Any) -> list[dict[str, Any]]:
        if not isinstance(incoming, list) or len(incoming) > 50:
            raise ValueError("视频渠道必须是最多 50 项的数组。")
        previous_rows = {}
        for item in previous:
            if not isinstance(item, dict):
                continue
            name = str(item.get("name") or "").strip()
            channel_id = str(item.get("channel_id") or "").strip()
            if not channel_id and name:
                channel_id = hashlib.sha256(name.encode("utf-8")).hexdigest()[:16]
            if channel_id:
                previous_rows[channel_id] = item
            if name:
                previous_rows[f"name:{name}"] = item
        merged: list[dict[str, Any]] = []
        names: set[str] = set()
        channel_ids: set[str] = set()
        for item in incoming:
            if not isinstance(item, dict):
                raise ValueError("渠道配置项必须是对象。")
            row = dict(item)
            name = str(row.get("name") or "").strip()
            if not name or name in names:
                raise ValueError("每个渠道都需要唯一名称。")
            names.add(name)
            channel_id = str(row.get("channel_id") or "").strip()
            if not channel_id:
                channel_id = hashlib.sha256(name.encode("utf-8")).hexdigest()[:16]
            if channel_id in channel_ids:
                raise ValueError("渠道标识必须唯一。")
            channel_ids.add(channel_id)
            row["channel_id"] = channel_id
            provider = str(row.get("provider_type") or "").strip().lower()
            if provider not in {"agnes", "openai_video"}:
                raise ValueError(f"渠道 {name} 的 provider_type 只能是 agnes 或 openai_video。")
            base_url = str(row.get("base_url") or "").strip()
            parsed_base = urlsplit(base_url)
            if parsed_base.scheme not in {"https", "http"} or not parsed_base.netloc:
                raise ValueError(f"渠道 {name} 的 Base URL 必须以 http:// 或 https:// 开头。")
            models = row.get("enabled_models") or row.get("models") or row.get("model")
            model_names = (
                [str(value).strip() for value in models if str(value).strip()]
                if isinstance(models, list)
                else [value.strip() for value in str(models or "").replace(",", "\n").splitlines() if value.strip()]
            )
            if not model_names:
                raise ValueError(f"渠道 {name} 至少需要一个模型名。")
            overrides = row.get("model_provider_types") or {}
            if not isinstance(overrides, dict):
                raise ValueError(f"渠道 {name} 的逐模型协议覆盖必须是 JSON 对象。")
            for model_name, model_provider in overrides.items():
                if str(model_name) not in model_names or not normalize_video_provider_type(model_provider):
                    raise ValueError(f"渠道 {name} 的逐模型协议覆盖包含未知模型或协议。")
            supplied_key = str(row.get("api_key") or "").strip()
            supplied_keys = row.get("api_keys") if isinstance(row.get("api_keys"), list) else []
            if not supplied_key and not supplied_keys:
                old = previous_rows.get(channel_id) or previous_rows.get(f"name:{name}") or {}
                row["api_key"] = old.get("api_key") or ""
                row["api_keys"] = old.get("api_keys") or []
            else:
                row["api_key"] = supplied_key
                row["api_keys"] = supplied_keys if supplied_keys else ([supplied_key] if supplied_key else [])
            if not row.get("api_key") and not row.get("api_keys"):
                raise ValueError(f"渠道 {name} 尚未配置 API Key。")
            row.pop("has_api_key", None)
            merged.append(row)
        return merged

    def _register_routes(self) -> None:
        @self.app.before_request
        def preflight():
            if request.method == "OPTIONS":
                return "", 204
            return None

        @self.app.after_request
        def cors(response):
            if request.path.startswith("/api/"):
                response.headers["Cache-Control"] = "no-store"
            origin = str(request.headers.get("Origin") or "")
            parsed_origin = urlsplit(origin)
            if parsed_origin.scheme in {"http", "https"} and parsed_origin.netloc:
                response.headers["Access-Control-Allow-Origin"] = origin
                response.headers["Access-Control-Allow-Methods"] = "GET, PUT, POST, OPTIONS"
                response.headers["Access-Control-Allow-Headers"] = "Authorization, Content-Type, X-Web-Token"
                response.headers["Vary"] = "Origin"
            return response

        @self.app.get("/")
        def index():
            path = os.path.join(self.plugin.plugin_root, "pages", "dashboard", "index.html")
            return send_file(path)

        @self.app.get("/api/config")
        def get_config():
            unauthorized = self._require_auth()
            if unauthorized:
                return unauthorized
            return jsonify(self._masked_settings(self.plugin.get_settings_snapshot()))

        @self.app.put("/api/config")
        def put_config():
            unauthorized = self._require_auth()
            if unauthorized:
                return unauthorized
            incoming = request.get_json(silent=True)
            if not isinstance(incoming, dict):
                return jsonify({"error": "请求体必须是 JSON 对象。"}), 400
            try:
                current = self.plugin.get_settings_snapshot()
                channels = self._merge_channel_keys(
                    incoming.get("video_channels", current.get("video_channels", [])),
                    current.get("video_channels", []),
                )
                updated = normalize_config({
                    **current,
                    "video": {**current.get("video", {}), **(incoming.get("video") or {})},
                    "video_channels": channels,
                    "enabled_video_model_priority": incoming.get("enabled_video_model_priority", current.get("enabled_video_model_priority", [])),
                    "permission": {**current.get("permission", {}), **(incoming.get("permission") or {})},
                })
                self.plugin.replace_settings(updated)
            except (TypeError, ValueError) as exc:
                return jsonify({"error": str(exc)}), 400
            return jsonify({"ok": True, "config": self._masked_settings(self.plugin.get_settings_snapshot())})

        @self.app.get("/api/jobs")
        def get_jobs():
            unauthorized = self._require_auth()
            if unauthorized:
                return unauthorized
            status = str(request.args.get("status") or "").strip().lower()
            if status not in {"queued", "running", "succeeded", "failed", "cancelled"}:
                status = ""
            try:
                limit = max(1, min(200, int(request.args.get("limit", 100))))
                offset = max(0, int(request.args.get("offset", 0)))
            except (TypeError, ValueError):
                return jsonify({"error": "limit 和 offset 必须为整数。"}), 400
            rows = self.plugin.store.list_jobs(
                status=status,
                search=str(request.args.get("q") or ""),
                limit=limit,
                offset=offset,
            )
            for row in rows:
                row.pop("video_path", None)
                row.pop("video_url", None)
                row.pop("request_json", None)
            return jsonify({"jobs": rows, "limit": limit, "offset": offset})

        @self.app.post("/api/jobs/<job_id>/cancel")
        def cancel_job(job_id: str):
            unauthorized = self._require_auth()
            if unauthorized:
                return unauthorized
            if not self.plugin.cancel_job(job_id.upper()):
                return jsonify({"error": "任务不存在或已结束。"}), 404
            return jsonify({"ok": True, "job_id": job_id.upper()})

        @self.app.post("/api/jobs/<job_id>/retry")
        def retry_job(job_id: str):
            unauthorized = self._require_auth()
            if unauthorized:
                return unauthorized
            try:
                new_id = self.plugin.retry_job(job_id.upper(), owner_id=None)
            except ValueError as exc:
                return jsonify({"error": str(exc)}), 400
            except RuntimeError as exc:
                return jsonify({"error": str(exc)}), 503
            return jsonify({"ok": True, "job_id": new_id}), 202

        @self.app.get("/api/media/<job_id>")
        def get_media(job_id: str):
            unauthorized = self._require_auth()
            if unauthorized:
                return unauthorized
            row = self.plugin.store.get(job_id)
            if not row or row.get("status") != "succeeded":
                return jsonify({"error": "视频记录不存在。"}), 404
            path = os.path.realpath(str(row.get("video_path") or ""))
            root = os.path.realpath(self.plugin.video_dir)
            try:
                inside_root = os.path.commonpath([root, path]) == root
            except ValueError:
                inside_root = False
            if not inside_root or not os.path.isfile(path):
                return jsonify({"error": "视频文件不存在。"}), 404
            download = request.args.get("download") == "1"
            return send_file(
                path,
                mimetype="video/mp4",
                as_attachment=download,
                download_name=os.path.basename(path),
            )

        @self.app.post("/api/test")
        def test_channel():
            unauthorized = self._require_auth()
            if unauthorized:
                return unauthorized
            payload = request.get_json(silent=True) or {}
            channel_name = str(payload.get("channel_name") or "").strip()
            model = str(payload.get("model") or "").strip()
            prompt = str(payload.get("prompt") or "").strip()
            if not prompt:
                return jsonify({"error": "测试提示词不能为空。"}), 400
            if len(prompt) > 4000:
                return jsonify({"error": "测试提示词不能超过 4000 个字符。"}), 400
            target = next(
                (item for item in build_video_targets(self.plugin.get_settings_snapshot())
                 if item.channel_name == channel_name and (not model or item.model == model)),
                None,
            )
            if not target:
                return jsonify({"error": "没有找到所选启用渠道和模型。"}), 404
            try:
                duration = int(payload.get("duration") or 5)
            except (TypeError, ValueError):
                return jsonify({"error": "视频时长必须为整数。"}), 400
            duration = max(1, min(60, duration))
            try:
                request_data = VideoGenerateRequest(
                    prompt=prompt,
                    duration=duration,
                    size=str(payload.get("size") or ""),
                    first_frame=str(payload.get("first_frame") or ""),
                )
                job_id = self.plugin.submit_job("web-admin", request_data, [target])
            except (RuntimeError, ValueError) as exc:
                return jsonify({"error": str(exc)}), 503
            return jsonify({"ok": True, "job_id": job_id}), 202

    def start(self, host: str, port: int) -> None:
        if self._thread and self._thread.is_alive():
            return
        self._server = make_server(host, int(port), self.app, threaded=True)
        self._thread = threading.Thread(target=self._server.serve_forever, name="selfie-video-web", daemon=True)
        self._thread.start()

    def stop(self) -> None:
        if self._server:
            self._server.shutdown()
            self._server = None
        if self._thread and self._thread.is_alive():
            self._thread.join(timeout=3)
        self._thread = None
