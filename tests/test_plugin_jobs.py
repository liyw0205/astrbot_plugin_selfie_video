import asyncio
import json
import sqlite3
import stat
import sys
import threading
import types
from pathlib import Path

from aiohttp import web

sys.path.insert(0, str(Path(__file__).resolve().parents[2]))

from astrbot_plugin_selfie_video import main as plugin_module
from astrbot_plugin_selfie_video.generation.video import (
    VideoGenerateRequest,
    VideoGenerateResult,
    VideoTarget,
)


def _target():
    return VideoTarget("local", "openai_video", "http://127.0.0.1/v1", "k", "m")


def _plugin(tmp_path, monkeypatch, **config):
    monkeypatch.setattr(plugin_module, "get_astrbot_data_path", lambda: str(tmp_path))
    return plugin_module.SelfieVideoPlugin(context=None, config=config)


def _stub_video_component(monkeypatch):
    class VideoComponent:
        def __init__(self, *, file):
            self.file = file

    astrbot = types.ModuleType("astrbot")
    astrbot.__path__ = []
    api = types.ModuleType("astrbot.api")
    api.__path__ = []
    message_components = types.ModuleType("astrbot.api.message_components")
    message_components.Video = VideoComponent
    monkeypatch.setitem(sys.modules, "astrbot", astrbot)
    monkeypatch.setitem(sys.modules, "astrbot.api", api)
    monkeypatch.setitem(sys.modules, "astrbot.api.message_components", message_components)
    return VideoComponent


def test_submit_and_cancel_before_runner_starts_keeps_terminal_state(tmp_path, monkeypatch):
    plugin = _plugin(tmp_path, monkeypatch, web={"enable": False})
    assert stat.S_IMODE(Path(plugin.config_path).stat().st_mode) == 0o600

    async def never_runs(*args):
        await asyncio.Event().wait()

    async def scenario():
        plugin._loop = asyncio.get_running_loop()
        plugin._run_job = never_runs
        job_id = plugin.submit_job("user-1", VideoGenerateRequest("clip"), [_target()])
        assert plugin.store.get(job_id)["status"] == "queued"
        assert plugin.cancel_job(job_id, owner_id="user-1") is True
        await asyncio.sleep(0)
        await asyncio.sleep(0)
        assert plugin.store.get(job_id)["status"] == "cancelled"
        await plugin.terminate()

    asyncio.run(scenario())


def test_submit_from_web_thread_schedules_on_plugin_loop(tmp_path, monkeypatch):
    plugin = _plugin(tmp_path, monkeypatch, web={"enable": False})

    async def runner(job_id, owner_id, event, request, targets):
        plugin.store.update_if_status(job_id, ["queued"], status="running")
        plugin.store.update_if_status(job_id, ["running"], status="succeeded")

    async def scenario():
        plugin._loop = asyncio.get_running_loop()
        plugin._run_job = runner
        result = []
        worker = threading.Thread(
            target=lambda: result.append(
                plugin.submit_job("web-admin", VideoGenerateRequest("clip"), [_target()])
            )
        )
        worker.start()
        worker.join()
        await asyncio.sleep(0)
        await asyncio.sleep(0)
        assert plugin.store.get(result[0])["status"] == "succeeded"
        await plugin.terminate()

    asyncio.run(scenario())


def test_concurrency_setting_changes_wake_waiting_jobs(tmp_path, monkeypatch):
    plugin = _plugin(
        tmp_path,
        monkeypatch,
        web={"enable": False},
        video={"max_concurrent_tasks": 1},
    )

    async def scenario():
        plugin._loop = asyncio.get_running_loop()
        first_entered = asyncio.Event()
        second_entered = asyncio.Event()
        release = asyncio.Event()

        async def worker(entered):
            async with plugin._job_slot():
                entered.set()
                await release.wait()

        first = asyncio.create_task(worker(first_entered))
        second = asyncio.create_task(worker(second_entered))
        await first_entered.wait()
        await asyncio.sleep(0)
        assert not second_entered.is_set()
        plugin.replace_settings({"web": {"enable": False}, "video": {"max_concurrent_tasks": 2}})
        await asyncio.wait_for(second_entered.wait(), timeout=1)
        release.set()
        await asyncio.gather(first, second)

    asyncio.run(scenario())


def test_queue_cap_and_per_user_cooldown_reject_excess_submissions(tmp_path, monkeypatch):
    plugin = _plugin(
        tmp_path,
        monkeypatch,
        web={"enable": False},
    )
    plugin.replace_settings({"web": {"enable": False}, "video": {"max_pending_tasks": 1, "user_cooldown_seconds": 0}})

    async def scenario():
        plugin._loop = asyncio.get_running_loop()
        blocked = asyncio.Event()

        async def runner(*args):
            await blocked.wait()

        plugin._run_job = runner
        first = plugin.submit_job("user-1", VideoGenerateRequest("clip"), [_target()])
        try:
            plugin.submit_job("user-2", VideoGenerateRequest("another"), [_target()])
        except RuntimeError as exc:
            assert "队列已满" in str(exc)
        else:
            raise AssertionError("queue cap should reject a second pending job")
        assert plugin.cancel_job(first, owner_id="user-1") is True
        await asyncio.sleep(0)
        await asyncio.sleep(0)
        plugin.replace_settings({
            "web": {"enable": False},
            "video": {"max_pending_tasks": 5, "user_cooldown_seconds": 60},
        })
        plugin._run_job = runner
        second = plugin.submit_job("user-3", VideoGenerateRequest("clip"), [_target()])
        try:
            plugin.submit_job("user-3", VideoGenerateRequest("retry too soon"), [_target()])
        except RuntimeError as exc:
            assert "提交过于频繁" in str(exc)
        else:
            raise AssertionError("per-user cooldown should reject a rapid retry")
        plugin.cancel_job(second, owner_id="user-3")
        await plugin.terminate()

    asyncio.run(scenario())


def test_video_command_persists_request_and_delivers_generated_result(tmp_path, monkeypatch):
    plugin = _plugin(tmp_path, monkeypatch, web={"enable": False})
    plugin.replace_settings({
        "web": {"enable": False},
        "video": {"max_saved_videos": 1},
        "video_channels": [{
            "name": "primary",
            "enabled": True,
            "provider_type": "agnes",
            "base_url": "https://agnes.example/v1",
            "api_key": "key",
            "model": "agnes-video-2.0",
        }],
    })
    old_path = Path(plugin.video_dir) / "video_old.mp4"
    old_path.write_bytes(b"old video")
    plugin.store.create("OLD1", "previous-user", "old video")
    assert plugin.store.update_if_status(
        "OLD1", ["queued"], status="succeeded", video_path=str(old_path)
    ) is True

    video_path = Path(plugin.video_dir) / "generated.mp4"
    video_path.write_bytes(b"\x00\x00\x00\x18ftypmp42" + b"\x00" * 24)
    calls = []

    async def generated(targets, request, *, output_dir):
        calls.append((targets, request, output_dir))
        return VideoGenerateResult(
            video_path=str(video_path),
            used_model="agnes-video-2.0",
            channel_name="primary",
            elapsed_seconds=2.5,
        )

    monkeypatch.setattr(plugin_module, "generate_video_with_fallback", generated)

    class Event:
        def __init__(self):
            self.sent = []

        def get_sender_id(self):
            return "user-1"

        def get_message_str(self):
            return "/视频 a quiet lake --duration 7 --size 9:16"

        def plain_result(self, text):
            return text

        def chain_result(self, chain):
            return chain

        async def send(self, message):
            self.sent.append(message)

    async def scenario():
        event = Event()
        accepted = [item async for item in plugin.cmd_video(event)]
        assert "任务已受理" in accepted[0]
        job_id = accepted[0].split("：", 1)[1].split("。", 1)[0]
        await plugin._running[job_id]
        row = plugin.store.get(job_id, owner_id="user-1")
        assert row["status"] == "succeeded"
        assert row["channel_name"] == "primary"
        assert row["video_path"] == str(video_path)
        assert not old_path.exists()
        assert plugin.store.get("OLD1")["status"] == "succeeded"
        assert plugin.store.get("OLD1")["video_path"] == ""
        assert calls[0][1].prompt == "a quiet lake"
        assert calls[0][1].duration == 7
        assert calls[0][1].size == "9:16"
        assert any("视频生成完成" in item for item in event.sent)
        assert any(str(video_path) in item for item in event.sent)
        await plugin.terminate()

    asyncio.run(scenario())


def test_successful_job_prunes_video_files_off_the_event_loop(tmp_path, monkeypatch):
    plugin = _plugin(tmp_path, monkeypatch, web={"enable": False})
    loop_progressed = threading.Event()

    async def generated(*args, **kwargs):
        return VideoGenerateResult(video_path="unused.mp4", used_model="m", channel_name="local")

    def slow_prune():
        assert loop_progressed.wait(timeout=1), "media pruning blocked the event loop"

    monkeypatch.setattr(plugin_module, "generate_video_with_fallback", generated)
    monkeypatch.setattr(plugin, "_prune_video_assets", slow_prune)

    async def scenario():
        loop = asyncio.get_running_loop()
        loop.call_soon(loop_progressed.set)
        plugin.store.create("PRUNE1", "user", "clip")
        await plugin._run_job("PRUNE1", "user", None, VideoGenerateRequest("clip"), [_target()])
        assert plugin.store.get("PRUNE1")["status"] == "succeeded"

    asyncio.run(scenario())


def test_video_command_runs_agnes_http_flow_and_persists_sqlite_job(tmp_path, monkeypatch):
    plugin = _plugin(tmp_path, monkeypatch, web={"enable": False})
    calls = []
    poll_count = 0
    mp4 = b"\x00\x00\x00\x18ftypmp42" + b"\x00" * 24

    async def create(request):
        payload = await request.json()
        calls.append(("create", request.path, request.headers.get("Authorization"), payload))
        return web.json_response({"video_id": "agnes-e2e-1", "status": "processing"})

    async def poll(request):
        nonlocal poll_count
        poll_count += 1
        calls.append(("poll", request.query.get("video_id"), request.query.get("model_name")))
        if poll_count == 1:
            return web.json_response({"status": "processing"})
        return web.json_response({
            "status": "completed",
            "metadata": {"url": f"http://{request.host}/result.mp4"},
        })

    async def download(request):
        calls.append(("download", request.path))
        return web.Response(body=mp4, content_type="video/mp4")

    class Event:
        def __init__(self):
            self.sent = []

        def get_sender_id(self):
            return "user-e2e"

        def get_message_str(self):
            return "/视频 a test clip --duration 1 --size 9:16"

        def plain_result(self, text):
            return text

        def chain_result(self, chain):
            return chain

        async def send(self, message):
            self.sent.append(message)

    async def scenario():
        app = web.Application()
        app.router.add_post("/v1/videos", create)
        app.router.add_get("/agnesapi", poll)
        app.router.add_get("/result.mp4", download)
        runner = web.AppRunner(app)
        await runner.setup()
        site = web.TCPSite(runner, "127.0.0.1", 0)
        await site.start()
        port = site._server.sockets[0].getsockname()[1]
        plugin.replace_settings({
            "web": {"enable": False},
            "video": {"global_timeout": 30, "poll_interval": 1},
            "video_channels": [{
                "name": "agnes-local",
                "enabled": True,
                "provider_type": "agnes",
                "base_url": f"http://127.0.0.1:{port}/v1",
                "api_key": "local-test-key",
                "model": "agnes-video-2.0",
                "poll_interval": 1,
            }],
        })
        event = Event()
        try:
            accepted = [item async for item in plugin.cmd_video(event)]
            assert "任务已受理" in accepted[0]
            job_id = accepted[0].split("：", 1)[1].split("。", 1)[0]
            await asyncio.wait_for(plugin._running[job_id], timeout=10)

            row = plugin.store.get(job_id, owner_id="user-e2e")
            assert row["status"] == "succeeded"
            assert row["prompt"] == "a test clip"
            assert row["channel_name"] == "agnes-local"
            assert row["model"] == "agnes-video-2.0"
            assert Path(row["video_path"]).read_bytes() == mp4
            assert plugin_module.VideoJobStore(plugin.store.path).get(job_id)["status"] == "succeeded"
            assert [call[0] for call in calls] == ["create", "poll", "poll", "download"]
            assert calls[0][1:3] == ("/v1/videos", "Bearer local-test-key")
            assert calls[0][3]["prompt"] == "a test clip"
            assert calls[0][3]["num_frames"] == 25
            assert calls[1][1:] == ("agnes-e2e-1", "agnes-video-2.0")
            assert any("视频生成完成" in item for item in event.sent)
            assert any("视频文件已保存" in item and row["video_path"] in item for item in event.sent)
        finally:
            await plugin.terminate()
            await runner.cleanup()

    asyncio.run(scenario())


def test_video_command_runs_compatible_async_http_flow(tmp_path, monkeypatch):
    plugin = _plugin(tmp_path, monkeypatch, web={"enable": False})
    video_component = _stub_video_component(monkeypatch)
    calls = []
    poll_count = 0
    mp4 = b"\x00\x00\x00\x18ftypmp42" + b"\x00" * 24

    async def create(request):
        payload = await request.json()
        calls.append(("create", request.path, request.headers.get("Authorization"), payload))
        return web.json_response({"id": "compatible-e2e-1", "status": "queued"})

    async def poll(request):
        nonlocal poll_count
        poll_count += 1
        calls.append(("poll", request.path))
        if poll_count == 1:
            return web.json_response({"status": "processing"})
        return web.json_response({
            "status": "completed",
            "result": {"video_url": f"http://{request.host}/compatible.mp4"},
        })

    async def download(request):
        calls.append(("download", request.path))
        return web.Response(body=mp4, content_type="video/mp4")

    class Event:
        def __init__(self):
            self.sent = []

        def get_sender_id(self):
            return "user-compatible-e2e"

        def get_message_str(self):
            return "/视频 a compatible test --duration 4 --size 16:9"

        def plain_result(self, text):
            return text

        def chain_result(self, chain):
            return chain

        async def send(self, message):
            self.sent.append(message)

    async def scenario():
        app = web.Application()
        app.router.add_post("/v1/videos/generations", create)
        app.router.add_get("/v1/videos/generations/{task_id}", poll)
        app.router.add_get("/compatible.mp4", download)
        runner = web.AppRunner(app)
        await runner.setup()
        site = web.TCPSite(runner, "127.0.0.1", 0)
        await site.start()
        port = site._server.sockets[0].getsockname()[1]
        plugin.replace_settings({
            "web": {"enable": False},
            "video": {"global_timeout": 30, "poll_interval": 1},
            "video_channels": [{
                "name": "compatible-local",
                "enabled": True,
                "provider_type": "openai_video",
                "base_url": f"http://127.0.0.1:{port}/v1",
                "api_key": "local-compatible-key",
                "model": "rh-minimax-h3",
                "poll_interval": 1,
            }],
        })
        event = Event()
        try:
            accepted = [item async for item in plugin.cmd_video(event)]
            assert "任务已受理" in accepted[0]
            job_id = accepted[0].split("：", 1)[1].split("。", 1)[0]
            await asyncio.wait_for(plugin._running[job_id], timeout=10)

            row = plugin.store.get(job_id, owner_id="user-compatible-e2e")
            assert row["status"] == "succeeded"
            assert row["prompt"] == "a compatible test"
            assert row["channel_name"] == "compatible-local"
            assert row["model"] == "rh-minimax-h3"
            assert Path(row["video_path"]).read_bytes() == mp4
            assert row["video_url"].endswith("/compatible.mp4")
            assert [call[0] for call in calls] == ["create", "poll", "poll", "download"]
            assert calls[0][1:3] == (
                "/v1/videos/generations",
                "Bearer local-compatible-key",
            )
            assert calls[0][3]["prompt"] == "a compatible test"
            assert calls[0][3]["duration"] == 4
            assert calls[0][3]["aspect_ratio"] == "16:9"
            assert calls[1][1] == "/v1/videos/generations/compatible-e2e-1"
            assert any("视频生成完成" in item for item in event.sent)
            assert any(
                isinstance(item, list)
                and len(item) == 1
                and isinstance(item[0], video_component)
                and item[0].file == row["video_path"]
                for item in event.sent
            )
        finally:
            await plugin.terminate()
            await runner.cleanup()

    asyncio.run(scenario())


def test_video_command_cancels_compatible_poll_without_delivering_result(tmp_path, monkeypatch):
    plugin = _plugin(tmp_path, monkeypatch, web={"enable": False})
    calls = []
    poll_entered = asyncio.Event()
    release_poll = asyncio.Event()

    async def create(request):
        calls.append("create")
        return web.json_response({"id": "compatible-cancel-1", "status": "queued"})

    async def poll(request):
        calls.append("poll")
        poll_entered.set()
        await release_poll.wait()
        return web.json_response({"status": "processing"})

    class Event:
        def __init__(self):
            self.sent = []

        def get_sender_id(self):
            return "user-cancel-e2e"

        def get_message_str(self):
            return "/视频 a cancellable test"

        def plain_result(self, text):
            return text

        def chain_result(self, chain):
            return chain

        async def send(self, message):
            self.sent.append(message)

    async def scenario():
        app = web.Application()
        app.router.add_post("/v1/videos/generations", create)
        app.router.add_get("/v1/videos/generations/{task_id}", poll)
        runner = web.AppRunner(app)
        await runner.setup()
        site = web.TCPSite(runner, "127.0.0.1", 0)
        await site.start()
        port = site._server.sockets[0].getsockname()[1]
        plugin.replace_settings({
            "web": {"enable": False},
            "video": {"global_timeout": 30, "poll_interval": 1},
            "video_channels": [{
                "name": "compatible-local",
                "enabled": True,
                "provider_type": "openai_video",
                "base_url": f"http://127.0.0.1:{port}/v1",
                "api_key": "local-compatible-key",
                "model": "test-video-model",
                "poll_interval": 1,
            }],
        })
        event = Event()
        try:
            accepted = [item async for item in plugin.cmd_video(event)]
            job_id = accepted[0].split("：", 1)[1].split("。", 1)[0]
            task = plugin._running[job_id]
            await asyncio.wait_for(poll_entered.wait(), timeout=5)

            assert plugin.cancel_job(job_id, owner_id="user-cancel-e2e") is True
            release_poll.set()
            try:
                await asyncio.wait_for(task, timeout=5)
            except asyncio.CancelledError:
                pass
            else:
                raise AssertionError("cancelled provider polling task should stop")

            row = plugin.store.get(job_id, owner_id="user-cancel-e2e")
            assert row["status"] == "cancelled"
            assert calls == ["create", "poll"]
            assert event.sent == []
        finally:
            release_poll.set()
            await plugin.terminate()
            await runner.cleanup()

    asyncio.run(scenario())


def test_agnes_cancel_during_urllib_download_removes_partial_file(tmp_path, monkeypatch):
    plugin = _plugin(tmp_path, monkeypatch, web={"enable": False})
    calls = []
    download_started = asyncio.Event()
    release_download = asyncio.Event()
    download_finished = asyncio.Event()
    mp4 = b"\x00\x00\x00\x18ftypmp42" + b"\x00" * 24

    async def create(request):
        calls.append("create")
        return web.json_response({"video_id": "agnes-cancel-download", "status": "processing"})

    async def poll(request):
        calls.append("poll")
        return web.json_response({
            "status": "completed",
            "metadata": {"url": f"http://{request.host}/slow.mp4"},
        })

    async def download(request):
        calls.append("download")
        response = web.StreamResponse(headers={"Content-Type": "video/mp4"})
        await response.prepare(request)
        await response.write(mp4[:16])
        download_started.set()
        await release_download.wait()
        await response.write(mp4[16:])
        await response.write_eof()
        download_finished.set()
        return response

    class Event:
        def __init__(self):
            self.sent = []

        def get_sender_id(self):
            return "user-agnes-download-cancel"

        def get_message_str(self):
            return "/视频 a cancellable Agnes download"

        def plain_result(self, text):
            return text

        def chain_result(self, chain):
            return chain

        async def send(self, message):
            self.sent.append(message)

    async def scenario():
        app = web.Application()
        app.router.add_post("/v1/videos", create)
        app.router.add_get("/agnesapi", poll)
        app.router.add_get("/slow.mp4", download)
        runner = web.AppRunner(app)
        await runner.setup()
        site = web.TCPSite(runner, "127.0.0.1", 0)
        await site.start()
        port = site._server.sockets[0].getsockname()[1]
        plugin.replace_settings({
            "web": {"enable": False},
            "video": {"global_timeout": 30, "poll_interval": 1},
            "video_channels": [{
                "name": "agnes-local",
                "enabled": True,
                "provider_type": "agnes",
                "base_url": f"http://127.0.0.1:{port}/v1",
                "api_key": "local-agnes-key",
                "model": "agnes-video-2.0",
                "poll_interval": 1,
            }],
        })
        event = Event()
        try:
            accepted = [item async for item in plugin.cmd_video(event)]
            job_id = accepted[0].split("：", 1)[1].split("。", 1)[0]
            task = plugin._running[job_id]
            await asyncio.wait_for(download_started.wait(), timeout=5)

            assert plugin.cancel_job(job_id, owner_id="user-agnes-download-cancel") is True
            try:
                await asyncio.wait_for(task, timeout=5)
            except asyncio.CancelledError:
                pass
            else:
                raise AssertionError("cancelled Agnes download task should stop")
            assert plugin.store.get(job_id)["status"] == "cancelled"

            release_download.set()
            await asyncio.wait_for(download_finished.wait(), timeout=5)
            deadline = asyncio.get_running_loop().time() + 2
            partial_files = list(Path(plugin.video_dir).glob("*.part"))
            while partial_files and asyncio.get_running_loop().time() < deadline:
                await asyncio.sleep(0.01)
                partial_files = list(Path(plugin.video_dir).glob("*.part"))
            assert partial_files == []
            assert list(Path(plugin.video_dir).glob("*.mp4")) == []
            assert calls == ["create", "poll", "download"]
            assert event.sent == []
        finally:
            release_download.set()
            await plugin.terminate()
            await runner.cleanup()

    asyncio.run(scenario())


def test_video_retention_removes_old_mp4s_but_preserves_task_history(tmp_path, monkeypatch):
    plugin = _plugin(tmp_path, monkeypatch, web={"enable": False})
    plugin.replace_settings({
        "web": {"enable": False},
        "video": {"max_saved_videos": 2},
    })
    paths = []
    for index in range(3):
        path = Path(plugin.video_dir) / f"video_{index}.mp4"
        path.write_bytes(b"video")
        paths.append(str(path))
        job_id = f"RET{index}"
        plugin.store.create(job_id, "user", f"clip {index}")
        assert plugin.store.update_if_status(
            job_id, ["queued"], status="succeeded", video_path=str(path)
        ) is True

    with sqlite3.connect(plugin.store.path) as connection:
        for index in range(3):
            connection.execute(
                "UPDATE video_jobs SET updated_at = ? WHERE id = ?",
                (index + 1, f"RET{index}"),
            )

    async def initialize_without_web():
        await plugin.initialize()
        await plugin.terminate()

    asyncio.run(initialize_without_web())

    assert not Path(paths[0]).exists()
    assert Path(paths[1]).exists()
    assert Path(paths[2]).exists()
    assert plugin.store.get("RET0")["status"] == "succeeded"
    assert plugin.store.get("RET0")["video_path"] == ""
    assert plugin.store.get("RET1")["video_path"] == paths[1]
    assert plugin.store.get("RET2")["video_path"] == paths[2]


def test_plugin_applies_pending_video_backup_before_opening_job_store(tmp_path, monkeypatch):
    plugin = _plugin(tmp_path, monkeypatch, web={"enable": False})
    video_dir = Path(plugin.video_dir)
    (video_dir / "video_REST1.mp4").write_bytes(b"\x00\x00\x00\x18ftypmp42" + b"\x00" * 24)
    plugin.store.create("REST1", "user", "restored clip")
    assert plugin.store.update_if_status(
        "REST1", ["queued"], status="succeeded", video_path=str(video_dir / "video_REST1.mp4")
    )
    backup = plugin.video_backup.create_backup()
    plugin.store.create("LATER1", "user", "later clip")
    assert plugin.store.update_if_status("LATER1", ["queued"], status="failed", error="test")
    (video_dir / "later.mp4").write_bytes(b"later")
    plugin.prepare_video_backup_restore(backup_id=backup["id"])

    reloaded = _plugin(tmp_path, monkeypatch, web={"enable": False})

    assert reloaded.video_backup.restore_pending is False
    assert reloaded.store.get("REST1")["video_path"] == str(video_dir / "REST1.mp4")
    assert reloaded.store.get("LATER1") is None
    assert (video_dir / "REST1.mp4").is_file()
    assert not (video_dir / "later.mp4").exists()


def test_prepared_restore_blocks_new_video_submissions(tmp_path, monkeypatch):
    plugin = _plugin(tmp_path, monkeypatch, web={"enable": False})
    backup = plugin.video_backup.create_backup()
    plugin.prepare_video_backup_restore(backup_id=backup["id"])

    async def scenario():
        plugin._loop = asyncio.get_running_loop()
        try:
            plugin.submit_job("user", VideoGenerateRequest("clip"), [_target()])
        except RuntimeError as exc:
            assert "恢复已准备" in str(exc)
        else:
            raise AssertionError("new jobs must stop while a restore awaits restart")

    asyncio.run(scenario())
    assert plugin.store.active_count() == 0


def test_video_backup_restore_is_rejected_while_jobs_are_active(tmp_path, monkeypatch):
    plugin = _plugin(tmp_path, monkeypatch, web={"enable": False})
    plugin.store.create("ACTIVE1", "user", "still running")
    try:
        plugin.prepare_video_backup_restore(backup_id="0" * 32)
    except ValueError as exc:
        assert "等待视频生成任务完成" in str(exc)
    else:
        raise AssertionError("restore preparation must reject active jobs")
    assert plugin.video_backup.restore_pending is False


def test_invalid_config_is_backed_up_before_defaults_are_written(tmp_path, monkeypatch):
    data_dir = tmp_path / "plugin_data" / plugin_module.PLUGIN_NAME
    data_dir.mkdir(parents=True)
    config_path = data_dir / plugin_module.PLUGIN_CONFIG_FILENAME
    config_path.write_text('{"video_channels": [broken', encoding="utf-8")
    monkeypatch.setattr(plugin_module, "get_astrbot_data_path", lambda: str(tmp_path))

    plugin = plugin_module.SelfieVideoPlugin(context=None, config={"web": {"enable": False}})

    backups = list(data_dir.glob("*.corrupt.*.bak"))
    assert len(backups) == 1
    assert backups[0].read_text(encoding="utf-8") == '{"video_channels": [broken'
    assert stat.S_IMODE(backups[0].stat().st_mode) == 0o600
    assert json.loads(config_path.read_text(encoding="utf-8"))["video_channels"] == []
    assert stat.S_IMODE(config_path.stat().st_mode) == 0o600


def test_astrbot_default_token_does_not_rotate_saved_dashboard_token(tmp_path, monkeypatch):
    data_dir = tmp_path / "plugin_data" / plugin_module.PLUGIN_NAME
    data_dir.mkdir(parents=True)
    config_path = data_dir / plugin_module.PLUGIN_CONFIG_FILENAME
    config_path.write_text(
        json.dumps({"web": {"enable": True, "token": "persisted-random-token"}}),
        encoding="utf-8",
    )
    monkeypatch.setattr(plugin_module, "get_astrbot_data_path", lambda: str(tmp_path))

    plugin = plugin_module.SelfieVideoPlugin(
        context=None,
        config={"web": {"enable": True, "token": "changeme"}},
    )

    assert plugin.settings["web"]["token"] == "persisted-random-token"


def test_unexpected_provider_error_redacts_keys_from_every_fallback_target(tmp_path, monkeypatch, caplog):
    plugin = _plugin(tmp_path, monkeypatch, web={"enable": False})
    targets = [
        VideoTarget("first", "openai_video", "https://first.example/v1", "first-secret", "model-a"),
        VideoTarget("second", "openai_video", "https://second.example/v1", "second-secret", "model-b"),
    ]

    async def unexpected_failure(*args, **kwargs):
        raise RuntimeError("upstream leaked second-secret")

    monkeypatch.setattr(plugin_module, "generate_video_with_fallback", unexpected_failure)

    async def scenario():
        plugin._loop = asyncio.get_running_loop()
        plugin.store.create("SECRET1", "user-1", "clip")
        await plugin._run_job("SECRET1", "user-1", None, VideoGenerateRequest("clip"), targets)
        row = plugin.store.get("SECRET1")
        assert row["status"] == "failed"
        assert "second-secret" not in row["error"]
        assert "[redacted]" in row["error"]

    asyncio.run(scenario())
    assert "second-secret" not in caplog.text
