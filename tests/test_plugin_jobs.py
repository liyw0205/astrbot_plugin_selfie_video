import asyncio
import stat
import sys
import threading
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[2]))

from astrbot_plugin_selfie_video import main as plugin_module
from astrbot_plugin_selfie_video.generation.video import VideoGenerateRequest, VideoTarget


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
