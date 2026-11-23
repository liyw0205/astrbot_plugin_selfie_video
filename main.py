"""AstrBot video generation plugin."""

from __future__ import annotations

import asyncio
import json
import logging
import os
import re
import secrets
import shlex
import threading
import time
from contextlib import asynccontextmanager
from typing import Any, AsyncGenerator, Dict, Optional, Tuple

try:
    from astrbot.api.star import Context, Star, register
    from astrbot.api.event import AstrMessageEvent, filter
    from astrbot.api import logger
except ImportError:  # Keep provider logic importable in a standalone test environment.
    Context = Any
    AstrMessageEvent = Any
    logger = logging.getLogger("selfie_video")

    class Star:
        def __init__(self, context: Any = None) -> None:
            self.context = context

    def register(*args: Any, **kwargs: Any):
        return lambda cls: cls

    class _Filter:
        @staticmethod
        def command(*args: Any, **kwargs: Any):
            return lambda fn: fn

    filter = _Filter()

try:
    from astrbot.core.utils.astrbot_path import get_astrbot_data_path
except Exception:
    def get_astrbot_data_path() -> str:
        return os.path.join(os.getcwd(), "data")

from .core.config import build_video_targets, config_to_dict, normalize_config
from .core.constants import (
    PLUGIN_AUTHOR,
    PLUGIN_CONFIG_FILENAME,
    PLUGIN_DISPLAY_NAME,
    PLUGIN_NAME,
    PLUGIN_VERSION,
)
from .core.store import VideoJobStore
from .generation.video import (
    VideoGenerateRequest,
    VideoGenerationError,
    _redact,
    generate_video_with_fallback,
)
from .webui.server import VideoWebServer


def _load_json(path: str) -> Dict[str, Any]:
    def preserve_invalid_file() -> None:
        backup_path = f"{path}.corrupt.{time.time_ns()}.bak"
        os.replace(path, backup_path)
        os.chmod(backup_path, 0o600)

    try:
        with open(path, "r", encoding="utf-8") as handle:
            value = json.load(handle)
    except FileNotFoundError:
        return {}
    except json.JSONDecodeError:
        preserve_invalid_file()
        return {}
    if isinstance(value, dict):
        return value
    preserve_invalid_file()
    return {}


def _save_json(path: str, value: Dict[str, Any]) -> None:
    temp_path = f"{path}.tmp"
    fd = os.open(temp_path, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
    with os.fdopen(fd, "w", encoding="utf-8") as handle:
        json.dump(value, handle, ensure_ascii=False, indent=2)
        handle.write("\n")
    os.replace(temp_path, path)
    os.chmod(path, 0o600)


def _split_values(value: Any) -> set[str]:
    if isinstance(value, (list, tuple, set)):
        items = value
    else:
        items = re.split(r"[\s,;]+", str(value or ""))
    return {str(item).strip() for item in items if str(item).strip()}


def _bounded_int(value: Any, default: int, minimum: int, maximum: int) -> int:
    try:
        parsed = int(value)
    except (TypeError, ValueError):
        parsed = default
    return max(minimum, min(maximum, parsed))


def _owner_id(event: Any) -> str:
    for method_name in ("get_sender_id", "get_session_id", "get_unified_msg_origin"):
        method = getattr(event, method_name, None)
        if callable(method):
            try:
                value = method()
            except Exception:
                value = ""
            if value:
                return str(value)
    return "unknown"


def _message_text(event: Any, *args: str) -> str:
    method = getattr(event, "get_message_str", None)
    try:
        raw = str(method() if callable(method) else "")
    except Exception:
        raw = ""
    if not raw.strip():
        raw = " ".join(str(item or "").strip() for item in args if str(item or "").strip())
    return re.sub(r"^\s*/?视频(?:\s+|$)", "", raw, count=1).strip()


def _request_data(request: VideoGenerateRequest) -> Dict[str, Any]:
    return {
        "duration": request.duration,
        "size": request.size,
        "first_frame": request.first_frame,
        "extra": request.extra,
    }


def _request_from_row(row: Dict[str, Any]) -> VideoGenerateRequest:
    try:
        saved = json.loads(str(row.get("request_json") or "{}"))
    except (TypeError, ValueError):
        saved = {}
    if not isinstance(saved, dict):
        saved = {}
    try:
        duration = max(1, min(60, int(saved.get("duration", 5))))
    except (TypeError, ValueError):
        duration = 5
    extra = saved.get("extra") if isinstance(saved.get("extra"), dict) else {}
    return VideoGenerateRequest(
        prompt=str(row.get("prompt") or ""),
        duration=duration,
        size=str(saved.get("size") or ""),
        first_frame=str(saved.get("first_frame") or ""),
        extra=extra,
    )


def _parse_request(text: str, default_duration: int) -> Tuple[str, VideoGenerateRequest]:
    try:
        tokens = shlex.split(text)
    except ValueError:
        tokens = str(text or "").split()
    prompt: list[str] = []
    duration = default_duration
    size = ""
    first_frame = ""
    extra: Dict[str, Any] = {}
    index = 0
    while index < len(tokens):
        token = tokens[index]
        key, separator, inline_value = token.partition("=")
        if key in {"--duration", "-d"}:
            if separator:
                value = inline_value
            elif index + 1 < len(tokens):
                index += 1
                value = tokens[index]
            else:
                value = ""
            try:
                duration = max(1, min(60, int(re.sub(r"s$", "", value, flags=re.I))))
            except ValueError:
                pass
        elif key == "--size":
            if separator:
                size = inline_value
            elif index + 1 < len(tokens):
                index += 1
                size = tokens[index]
        elif key in {"--image", "--first-frame"}:
            if separator:
                first_frame = inline_value
            elif index + 1 < len(tokens):
                index += 1
                first_frame = tokens[index]
        elif token.startswith("--") and separator:
            extra[key[2:]] = inline_value
        else:
            prompt.append(token)
        index += 1
    request_prompt = " ".join(prompt).strip()
    return request_prompt, VideoGenerateRequest(
        prompt=request_prompt,
        duration=duration,
        size=size,
        first_frame=first_frame,
        extra=extra,
    )


@register(PLUGIN_NAME, PLUGIN_AUTHOR, f"{PLUGIN_DISPLAY_NAME} v{PLUGIN_VERSION}", PLUGIN_VERSION)
class SelfieVideoPlugin(Star):
    def __init__(self, context: Context, config: Optional[dict] = None):
        super().__init__(context)
        native = config_to_dict(config)
        data_root = str(get_astrbot_data_path())
        self.data_dir = os.path.join(data_root, "plugin_data", PLUGIN_NAME)
        self.video_dir = os.path.join(self.data_dir, "videos")
        os.makedirs(self.video_dir, exist_ok=True)
        self.plugin_root = os.path.dirname(os.path.abspath(__file__))
        self.config_path = os.path.join(self.data_dir, PLUGIN_CONFIG_FILENAME)
        self._config_lock = threading.RLock()
        self._submit_lock = threading.Lock()
        existing = _load_json(self.config_path)
        if not existing:
            existing = normalize_config({})
            _save_json(self.config_path, existing)
        native_web = native.get("web") if isinstance(native.get("web"), dict) else {}
        if native_web:
            existing["web"] = {**existing.get("web", {}), **native_web}
        self.settings = normalize_config(existing)
        self.store = VideoJobStore(os.path.join(self.data_dir, "video_jobs.sqlite3"))
        self.store.recover_interrupted()
        self._slot_condition = asyncio.Condition()
        self._active_job_count = 0
        self._loop: Optional[asyncio.AbstractEventLoop] = None
        self._running: Dict[str, asyncio.Task] = {}
        self._web_server: Optional[VideoWebServer] = None

    def get_settings_snapshot(self) -> Dict[str, Any]:
        with self._config_lock:
            return json.loads(json.dumps(self.settings))

    def replace_settings(self, settings: Dict[str, Any]) -> None:
        normalized = normalize_config(settings)
        with self._config_lock:
            _save_json(self.config_path, normalized)
            self.settings = normalized
        if self._loop and self._loop.is_running():
            self._loop.call_soon_threadsafe(self._wake_slot_waiters)

    def _concurrency_limit(self) -> int:
        try:
            value = int(self.get_settings_snapshot()["video"].get("max_concurrent_tasks", 1))
        except (TypeError, ValueError):
            value = 1
        return max(1, min(5, value))

    async def _notify_slot_waiters(self) -> None:
        async with self._slot_condition:
            self._slot_condition.notify_all()

    def _wake_slot_waiters(self) -> None:
        if self._loop and self._loop.is_running():
            self._loop.create_task(self._notify_slot_waiters())

    @asynccontextmanager
    async def _job_slot(self):
        async with self._slot_condition:
            await self._slot_condition.wait_for(
                lambda: self._active_job_count < self._concurrency_limit()
            )
            self._active_job_count += 1
        try:
            yield
        finally:
            async with self._slot_condition:
                self._active_job_count -= 1
                self._slot_condition.notify_all()

    def submit_job(
        self,
        owner_id: str,
        request: VideoGenerateRequest,
        targets: list,
        event: Any = None,
    ) -> str:
        try:
            current_loop = asyncio.get_running_loop()
        except RuntimeError:
            current_loop = None
        if current_loop is not None and (self._loop is None or not self._loop.is_running()):
            self._loop = current_loop
        loop = current_loop if current_loop is self._loop else self._loop
        if loop is None or not loop.is_running():
            raise RuntimeError("视频任务调度器尚未就绪，请稍后重试。")
        if not targets:
            raise ValueError("没有可用视频渠道，请先配置 video_channels。")

        owner_id = str(owner_id)
        video_settings = self.get_settings_snapshot().get("video", {})
        max_pending = _bounded_int(video_settings.get("max_pending_tasks", 20), 20, 1, 1000)
        cooldown = _bounded_int(video_settings.get("user_cooldown_seconds", 5), 5, 0, 86400)
        with self._submit_lock:
            if self.store.active_count() >= max_pending:
                raise RuntimeError(f"视频任务队列已满（最多 {max_pending} 个待处理任务），请稍后重试。")
            last_created = self.store.latest_created_at(owner_id)
            remaining = cooldown - (time.time() - last_created) if last_created else 0
            if remaining > 0:
                raise RuntimeError(f"提交过于频繁，请在 {int(remaining) + 1} 秒后重试。")
            job_id = secrets.token_hex(6).upper()
            self.store.create(job_id, owner_id, request.prompt, _request_data(request))
            self.store.update(
                job_id,
                channel_name=targets[0].channel_name,
                model=targets[0].model,
            )
        if current_loop is loop:
            self._start_job(job_id, owner_id, event, request, targets)
            return job_id
        loop.call_soon_threadsafe(
            self._start_job, job_id, owner_id, event, request, targets
        )
        return job_id

    def _start_job(
        self,
        job_id: str,
        owner_id: str,
        event: Any,
        request: VideoGenerateRequest,
        targets: list,
    ) -> None:
        row = self.store.get(job_id)
        if not row or row.get("status") != "queued":
            return
        self._running[job_id] = asyncio.create_task(
            self._run_job(job_id, owner_id, event, request, targets)
        )

    def cancel_job(self, job_id: str, *, owner_id: Optional[str] = None) -> bool:
        row = self.store.get(job_id, owner_id=owner_id)
        if not row or row.get("status") not in {"queued", "running"}:
            return False
        if not self.store.update_if_status(
            job_id, ["queued", "running"], status="cancelled", error="用户取消"
        ):
            return False
        loop = self._loop
        if loop and loop.is_running():
            def cancel_running() -> None:
                task = self._running.get(job_id)
                if task and not task.done():
                    task.cancel()
            loop.call_soon_threadsafe(cancel_running)
        return True

    def retry_job(self, job_id: str, *, owner_id: Optional[str] = None, event: Any = None) -> str:
        row = self.store.get(job_id, owner_id=owner_id)
        if not row:
            raise ValueError("视频记录不存在或无权访问。")
        if row.get("status") in {"queued", "running"}:
            raise ValueError("任务仍在运行，不能重复提交。")
        targets = build_video_targets(self.get_settings_snapshot())
        if not targets:
            raise ValueError("没有可用视频渠道，请先配置 video_channels。")
        return self.submit_job(
            str(row.get("owner_id") or owner_id or "unknown"),
            _request_from_row(row),
            targets,
            event,
        )

    async def initialize(self) -> None:
        self._loop = asyncio.get_running_loop()
        web = self.settings.get("web", {})
        if not web.get("enable", True):
            return
        token = str(web.get("token") or "").strip()
        if not token or token.lower() == "changeme":
            token = secrets.token_urlsafe(24)
            self.settings["web"] = {**web, "token": token}
            _save_json(self.config_path, self.settings)
            logger.info("Selfie Video 管理面板已生成随机 Token；可在插件配置文件中查看。")
        host = str(web.get("host") or "127.0.0.1").strip() or "127.0.0.1"
        try:
            port = max(1, min(65535, int(web.get("port", 14515))))
            self._web_server = VideoWebServer(self)
            self._web_server.start(host, port)
            logger.info("Selfie Video 管理面板已启动: http://%s:%s/", host, port)
        except Exception:
            logger.exception("Selfie Video 管理面板启动失败")

    def _authorized(self, event: Any) -> bool:
        sender = ""
        method = getattr(event, "get_sender_id", None)
        try:
            sender = str(method() if callable(method) else "")
        except Exception:
            pass
        permission = self.get_settings_snapshot().get("permission", {})
        blocked = _split_values(permission.get("blocked_users"))
        usable = _split_values(permission.get("usable_users"))
        if sender and sender in blocked:
            return False
        return not usable or sender in usable

    async def _send(self, event: Any, text: str) -> None:
        try:
            await event.send(event.plain_result(text))
        except Exception:
            logger.exception("视频任务通知发送失败")

    async def _send_video(self, event: Any, path: str, prompt: str) -> None:
        if event is None:
            return
        await self._send(event, f"视频生成完成：{prompt[:80]}")
        try:
            from astrbot.api.message_components import Video

            await event.send(event.chain_result([Video(file=path)]))
        except Exception as exc:
            logger.warning("视频消息段发送失败: %s", exc)
            await self._send(event, f"视频文件已保存：{path}")

    async def _run_job(
        self,
        job_id: str,
        owner_id: str,
        event: Any,
        request: VideoGenerateRequest,
        targets: list,
    ) -> None:
        try:
            async with self._job_slot():
                if not self.store.update_if_status(job_id, ["queued"], status="running"):
                    return
                result = await generate_video_with_fallback(
                    targets,
                    request,
                    output_dir=self.video_dir,
                )
            completed = self.store.update_if_status(
                job_id,
                ["running"],
                status="succeeded",
                channel_name=result.channel_name,
                model=result.used_model,
                video_path=result.video_path,
                video_url=result.video_url,
            )
            if completed:
                await self._send_video(event, result.video_path, request.prompt)
        except asyncio.CancelledError:
            self.store.update_if_status(
                job_id, ["queued", "running"], status="cancelled", error="用户取消"
            )
            raise
        except VideoGenerationError as exc:
            failed = self.store.update_if_status(
                job_id, ["queued", "running"], status="failed", error=str(exc)
            )
            if failed and event is not None:
                await self._send(event, f"视频生成失败（{exc.stage}）：{exc}")
        except Exception as exc:
            safe_error = _redact(f"{type(exc).__name__}: {exc}", targets[0])[:800] if targets else type(exc).__name__
            logger.error("视频任务异常，任务号 %s，错误类型 %s", job_id, type(exc).__name__)
            failed = self.store.update_if_status(
                job_id, ["queued", "running"], status="failed", error=safe_error
            )
            if failed and event is not None:
                await self._send(event, "视频生成失败，请查看任务记录。")
        finally:
            self._running.pop(job_id, None)

    @filter.command("视频")
    async def cmd_video(
        self,
        event: AstrMessageEvent,
        p1: str = "",
        p2: str = "",
        p3: str = "",
    ) -> AsyncGenerator[Any, None]:
        if not self._authorized(event):
            yield event.plain_result("当前账号没有使用视频生成的权限。")
            return
        text = _message_text(event, p1, p2, p3)
        if not text or text in {"help", "帮助", "--help"}:
            yield event.plain_result("用法：/视频 提示词 [--duration 5] [--size 16:9] [--image 图片URL]")
            return
        settings = self.get_settings_snapshot()
        if not settings.get("video", {}).get("enable", True):
            yield event.plain_result("视频生成当前已关闭。")
            return
        prompt, request = _parse_request(
            text,
            _bounded_int(settings["video"].get("default_duration", 5), 5, 1, 60),
        )
        if not prompt:
            yield event.plain_result("请提供视频提示词。")
            return
        targets = build_video_targets(settings)
        if not targets:
            yield event.plain_result("没有可用视频渠道，请在插件配置中添加 video_channels。")
            return
        owner_id = _owner_id(event)
        self._loop = asyncio.get_running_loop()
        try:
            job_id = self.submit_job(owner_id, request, targets, event)
        except RuntimeError as exc:
            yield event.plain_result(str(exc))
            return
        yield event.plain_result(f"视频任务已受理：{job_id}。可用 /视频任务 查看状态。")

    @filter.command("视频任务")
    async def cmd_video_tasks(self, event: AstrMessageEvent) -> AsyncGenerator[Any, None]:
        if not self._authorized(event):
            yield event.plain_result("当前账号没有查看视频任务的权限。")
            return
        rows = self.store.list_active(owner_id=_owner_id(event))
        if not rows:
            yield event.plain_result("当前没有进行中的视频任务。")
            return
        lines = [f"{row['id']}  {row['status']}  {row['prompt'][:50]}" for row in rows]
        yield event.plain_result("\n".join(lines))

    @filter.command("视频取消")
    async def cmd_video_cancel(self, event: AstrMessageEvent, job_id: str = "") -> AsyncGenerator[Any, None]:
        if not self._authorized(event):
            yield event.plain_result("当前账号没有取消视频任务的权限。")
            return
        target_id = str(job_id or "").strip().upper()
        if not target_id:
            yield event.plain_result("用法：/视频取消 任务号")
            return
        if not self.cancel_job(target_id, owner_id=_owner_id(event)):
            yield event.plain_result("没有找到你正在运行的这个视频任务。")
            return
        yield event.plain_result(f"已请求取消任务 {target_id}。上游已受理的生成可能仍会继续计费。")

    @filter.command("视频重试")
    async def cmd_video_retry(self, event: AstrMessageEvent, job_id: str = "") -> AsyncGenerator[Any, None]:
        if not self._authorized(event):
            yield event.plain_result("当前账号没有重试视频任务的权限。")
            return
        target_id = str(job_id or "").strip().upper()
        if not target_id:
            yield event.plain_result("用法：/视频重试 任务号")
            return
        self._loop = asyncio.get_running_loop()
        try:
            new_id = self.retry_job(target_id, owner_id=_owner_id(event), event=event)
        except (RuntimeError, ValueError) as exc:
            yield event.plain_result(str(exc))
            return
        yield event.plain_result(f"已重新提交为新任务：{new_id}。")

    @filter.command("视频记录")
    async def cmd_video_records(self, event: AstrMessageEvent) -> AsyncGenerator[Any, None]:
        if not self._authorized(event):
            yield event.plain_result("当前账号没有查看视频记录的权限。")
            return
        rows = self.store.list_recent(owner_id=_owner_id(event), limit=10)
        if not rows:
            yield event.plain_result("还没有视频生成记录。")
            return
        lines = []
        for row in rows:
            line = f"{row['id']}  {row['status']}  {row['prompt'][:45]}"
            if row.get("error"):
                line += f"\n  错误：{row['error'][:120]}"
            lines.append(line)
        yield event.plain_result("\n".join(lines))

    async def terminate(self) -> None:
        if self._web_server:
            self._web_server.stop()
            self._web_server = None
        for task in list(self._running.values()):
            task.cancel()
        if self._running:
            await asyncio.gather(*self._running.values(), return_exceptions=True)

    @filter.command("视频帮助")
    async def cmd_video_help(self, event: AstrMessageEvent) -> AsyncGenerator[Any, None]:
        yield event.plain_result(
            "用法：/视频 提示词 [--duration 5] [--size 16:9] [--image 图片URL]\n"
            "/视频任务 查看进行中任务；/视频取消 任务号；/视频重试 任务号；/视频记录 查看历史。"
        )
