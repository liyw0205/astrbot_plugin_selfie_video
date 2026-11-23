import asyncio
import stat
import sys
import threading
from pathlib import Path

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
        "video_channels": [{
            "name": "primary",
            "enabled": True,
            "provider_type": "agnes",
            "base_url": "https://agnes.example/v1",
            "api_key": "key",
            "model": "agnes-video-2.0",
        }],
    })
    video_path = tmp_path / "generated.mp4"
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
        assert calls[0][1].prompt == "a quiet lake"
        assert calls[0][1].duration == 7
        assert calls[0][1].size == "9:16"
        assert any("视频生成完成" in item for item in event.sent)
        assert any(str(video_path) in item for item in event.sent)
        await plugin.terminate()

    asyncio.run(scenario())
