import asyncio
import os
import sys
from pathlib import Path

import pytest
from aiohttp import web

sys.path.insert(0, str(Path(__file__).resolve().parents[2]))

from astrbot_plugin_selfie_video.core.config import build_video_targets
from astrbot_plugin_selfie_video.generation.video import (
    VideoGenerateRequest,
    VideoGenerationError,
    VideoTarget,
    _extract_status,
    _extract_task_id,
    _extract_video_url,
    build_agnes_videos_endpoint,
    build_create_payload,
    build_video_generations_endpoint,
    generate_video,
    generate_video_with_fallback,
)


def _run(coroutine):
    return asyncio.run(coroutine)


def test_endpoint_builders_accept_root_and_full_endpoint():
    assert build_agnes_videos_endpoint("https://example.test/v1") == "https://example.test/v1/videos"
    assert build_agnes_videos_endpoint("https://example.test/v1/videos") == "https://example.test/v1/videos"
    assert build_video_generations_endpoint("https://example.test") == "https://example.test/v1/videos/generations"
    assert build_video_generations_endpoint("https://example.test/v1/videos/generations") == "https://example.test/v1/videos/generations"
    assert build_video_generations_endpoint("https://example.test/v1/videos") == "https://example.test/v1/videos/generations"


def test_agnes_payloads_follow_model_family_and_validate_local_reference():
    v20 = VideoTarget("a", "agnes", "https://example.test/v1", "k", "agnes-video-v2.0")
    payload = build_create_payload(v20, VideoGenerateRequest("walk forward", duration=5, size="9:16"))
    assert payload["num_frames"] == 121
    assert payload["frame_rate"] == 24
    assert payload["width"] == 768
    assert payload["height"] == 1152

    v25 = VideoTarget("a", "agnes", "https://example.test/v1", "k", "agnes-video-2.5")
    payload = build_create_payload(v25, VideoGenerateRequest("walk forward", duration=20))
    assert payload["seconds"] == "12"
    assert payload["mode"] == "text"
    assert payload["size"] == "720P"

    v25_keyframe = VideoTarget("a", "agnes", "https://example.test/v1", "k", "agnes-video-2.5")
    payload = build_create_payload(
        v25_keyframe,
        VideoGenerateRequest(
            "animate a scene",
            first_frame="https://cdn.example/start.png",
            extra={"last_frame": "https://cdn.example/end.png", "quality": "1080P"},
        ),
    )
    assert payload["mode"] == "keyframe"
    assert payload["first_frame"].endswith("start.png")
    assert payload["last_frame"].endswith("end.png")
    assert payload["size"] == "1080P"

    with pytest.raises(VideoGenerationError, match="公开访问"):
        build_create_payload(v20, VideoGenerateRequest("animate", first_frame="data:image/png;base64,AA=="))

    frames = ["https://cdn.example/frame-1.png", "https://cdn.example/frame-2.png"]
    payload = build_create_payload(v20, VideoGenerateRequest("animate", extra={"images": frames}))
    assert payload["image"] == frames[0]
    assert payload["extra_body"] == {"image": frames, "mode": "keyframes"}


def test_compatible_payload_and_nested_response_extractors():
    target = VideoTarget("gw", "openai_video", "https://example.test/v1", "k", "rh-minimax-h3")
    payload = build_create_payload(target, VideoGenerateRequest("camera pans", duration=6, size="16:9"))
    assert payload == {
        "model": "rh-minimax-h3",
        "prompt": "camera pans",
        "duration": 6,
        "size": "16:9",
        "aspect_ratio": "16:9",
    }
    response = {"data": {"task_id": "job-9", "task_status": "processing", "output": {"video_url": "https://cdn.test/out.mp4"}}}
    assert _extract_task_id(response) == "job-9"
    assert _extract_status(response) == "PROCESSING"
    assert _extract_video_url(response) == "https://cdn.test/out.mp4"
    assert _extract_video_url({"choices": [{"message": {"content": "[video](https://cdn.test/clip.mp4)"}}]}) == "https://cdn.test/clip.mp4"
    assert _extract_video_url({"data": {"b64_json": "YWJj"}}) == "data:video/mp4;base64,YWJj"


def test_config_keeps_protocol_explicit_and_orders_targets():
    targets = build_video_targets({
        "video_channels": [
            {"name": "generic", "provider_type": "openai_video", "base_url": "https://g.test/v1", "api_key": "k1", "models": ["rh-minimax-h3"]},
            {"name": "agnes", "provider_type": "agnes", "base_url": "https://a.test/v1", "api_key": "k2", "model": "rh-minimax-h3"},
        ],
        "enabled_video_model_priority": ["agnes/rh-minimax-h3", "rh-minimax-h3"],
    })
    assert [target.channel_name for target in targets] == ["agnes", "generic"]
    assert [target.provider_type for target in targets] == ["agnes", "openai_video"]

    assert build_video_targets({
        "video_channels": [{"name": "ambiguous", "base_url": "https://g.test/v1", "api_key": "k", "model": "rh-minimax-h3"}]
    }) == []


def test_async_compatible_create_poll_download_and_secret_redaction(tmp_path):
    calls = []
    poll_count = 0
    mp4 = b"\x00\x00\x00\x18ftypmp42" + b"\x00" * 24

    async def create(request):
        body = await request.json()
        calls.append(("create", body))
        return web.json_response({"id": "task-1", "status": "queued"})

    async def poll(request):
        nonlocal poll_count
        poll_count += 1
        calls.append(("poll", request.path))
        if poll_count == 1:
            return web.json_response({"status": "processing"})
        return web.json_response({"status": "completed", "result": {"video_url": f"http://{request.host}/out.mp4"}})

    async def download(request):
        calls.append(("download", request.path))
        return web.Response(body=mp4, content_type="video/mp4")

    async def missing_legacy(request):
        return web.Response(status=404)

    async def run_success():
        app = web.Application()
        app.router.add_post("/v1/videos/generations", create)
        app.router.add_get("/v1/videos/generations/{task_id}", poll)
        app.router.add_get("/out.mp4", download)
        runner = web.AppRunner(app)
        await runner.setup()
        site = web.TCPSite(runner, "127.0.0.1", 0)
        await site.start()
        port = site._server.sockets[0].getsockname()[1]
        target = VideoTarget("local", "openai_video", f"http://127.0.0.1:{port}/v1", "test-secret", "model", timeout=10, poll_interval=1)
        try:
            result = await generate_video(target, VideoGenerateRequest("a test clip"), output_dir=str(tmp_path))
            return result
        finally:
            await runner.cleanup()

    result = _run(run_success())
    assert result.channel_name == "local"
    assert result.used_model == "model"
    assert os.path.exists(result.video_path)
    assert [name for name, _ in calls] == ["create", "poll", "poll", "download"]
    assert calls[0][1]["prompt"] == "a test clip"


def test_agnes_official_create_poll_and_urllib_download(tmp_path):
    calls = []
    mp4 = b"\x00\x00\x00\x18ftypmp42" + b"\x00" * 24

    async def create(request):
        body = await request.json()
        calls.append(("create", body))
        return web.json_response({"video_id": "agnes-video-1", "status": "processing"})

    async def poll(request):
        calls.append(("poll", request.query.get("video_id")))
        return web.json_response({"status": "completed", "metadata": {"url": f"http://{request.host}/agnes.mp4"}})

    async def download(request):
        calls.append(("download", request.path))
        return web.Response(body=mp4, content_type="video/mp4")

    async def missing_legacy(request):
        return web.Response(status=404)

    async def run_success():
        app = web.Application()
        app.router.add_post("/v1/videos", create)
        app.router.add_get("/agnesapi", poll)
        app.router.add_get("/v1/videos/{video_id}", missing_legacy)
        app.router.add_get("/agnes.mp4", download)
        runner = web.AppRunner(app)
        await runner.setup()
        site = web.TCPSite(runner, "127.0.0.1", 0)
        await site.start()
        port = site._server.sockets[0].getsockname()[1]
        target = VideoTarget("agnes-local", "agnes", f"http://127.0.0.1:{port}/v1", "test-key", "agnes-video-2.0", timeout=10, poll_interval=1)
        try:
            result = await generate_video(target, VideoGenerateRequest("a small test clip", duration=1), output_dir=str(tmp_path))
            return result
        finally:
            await runner.cleanup()

    result = _run(run_success())
    assert os.path.exists(result.video_path)
    assert [name for name, _ in calls] == ["create", "poll", "download"]
    assert calls[0][1]["num_frames"] == 25


def test_create_error_does_not_retry_another_channel():
    requests_seen = []

    async def create(request):
        requests_seen.append(request.path)
        return web.json_response({"error": "upstream failure"}, status=500)

    async def run_failure():
        app = web.Application()
        app.router.add_post("/v1/videos/generations", create)
        runner = web.AppRunner(app)
        await runner.setup()
        site = web.TCPSite(runner, "127.0.0.1", 0)
        await site.start()
        port = site._server.sockets[0].getsockname()[1]
        base = f"http://127.0.0.1:{port}/v1"
        targets = [
            VideoTarget("first", "openai_video", base, "k1", "model-a"),
            VideoTarget("second", "openai_video", base, "k2", "model-b"),
        ]
        try:
            with pytest.raises(VideoGenerationError) as error:
                await generate_video_with_fallback(targets, VideoGenerateRequest("clip"), output_dir="/tmp/video-test")
            return error.value
        finally:
            await runner.cleanup()

    error = _run(run_failure())
    assert error.stage == "create"
    assert error.status_code == 500
    assert requests_seen == ["/v1/videos/generations"]


def test_explicit_auth_error_retries_next_key_without_logging_keys(tmp_path):
    seen = []
    mp4 = b"\x00\x00\x00\x18ftypmp42" + b"\x00" * 24

    async def create(request):
        auth = request.headers.get("Authorization")
        seen.append(auth)
        if auth == "Bearer bad-key":
            return web.json_response({"error": "invalid key"}, status=401)
        return web.json_response({"video_url": f"http://{request.host}/clip.mp4"})

    async def download(request):
        return web.Response(body=mp4, content_type="video/mp4")

    async def run_success():
        app = web.Application()
        app.router.add_post("/v1/videos/generations", create)
        app.router.add_get("/clip.mp4", download)
        runner = web.AppRunner(app)
        await runner.setup()
        site = web.TCPSite(runner, "127.0.0.1", 0)
        await site.start()
        port = site._server.sockets[0].getsockname()[1]
        target = VideoTarget("local", "openai_video", f"http://127.0.0.1:{port}/v1", "bad-key", "model", api_keys=["bad-key", "good-key"])
        try:
            return await generate_video(target, VideoGenerateRequest("clip"), output_dir=str(tmp_path))
        finally:
            await runner.cleanup()

    result = _run(run_success())
    assert os.path.exists(result.video_path)
    assert seen == ["Bearer bad-key", "Bearer good-key"]


def test_upstream_error_redacts_channel_key(tmp_path):
    secret = "private-channel-key"

    async def create(request):
        return web.json_response({"error": f"rejected {secret}"}, status=401)

    async def run_failure():
        app = web.Application()
        app.router.add_post("/v1/videos/generations", create)
        runner = web.AppRunner(app)
        await runner.setup()
        site = web.TCPSite(runner, "127.0.0.1", 0)
        await site.start()
        port = site._server.sockets[0].getsockname()[1]
        target = VideoTarget("local", "openai_video", f"http://127.0.0.1:{port}/v1", secret, "model")
        try:
            with pytest.raises(VideoGenerationError) as error:
                await generate_video(target, VideoGenerateRequest("clip"), output_dir=str(tmp_path))
            return str(error.value)
        finally:
            await runner.cleanup()

    message = _run(run_failure())
    assert secret not in message
    assert "[redacted]" in message
