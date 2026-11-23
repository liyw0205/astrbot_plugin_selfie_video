"""Video generation transports for Agnes and OpenAI-compatible gateways."""

from __future__ import annotations

import asyncio
import base64
import binascii
import json
import os
import re
import time
import uuid
from dataclasses import dataclass, field
from typing import Any, Dict, Iterable, List, Optional, Tuple
from urllib.parse import quote, unquote_to_bytes, urlencode, urlparse
from urllib.request import ProxyHandler, Request as UrlRequest, build_opener
from urllib.error import HTTPError, URLError

import aiohttp


class VideoGenerationError(RuntimeError):
    def __init__(
        self,
        message: str,
        *,
        stage: str,
        status_code: Optional[int] = None,
        retryable: bool = False,
    ) -> None:
        super().__init__(message)
        self.stage = stage
        self.status_code = status_code
        self.retryable = retryable


@dataclass
class VideoTarget:
    channel_name: str
    provider_type: str
    base_url: str
    api_key: str
    model: str
    timeout: int = 300
    poll_interval: int = 5
    proxy: str = ""
    extra: Dict[str, Any] = field(default_factory=dict)
    api_keys: List[str] = field(default_factory=list)

    def resolved_api_keys(self) -> List[str]:
        values = self.api_keys or [self.api_key]
        result: List[str] = []
        for value in values:
            key = str(value or "").strip()
            if key and key not in result:
                result.append(key)
        return result


@dataclass
class VideoGenerateRequest:
    prompt: str
    duration: int = 5
    size: str = ""
    first_frame: str = ""
    extra: Dict[str, Any] = field(default_factory=dict)


@dataclass
class VideoGenerateResult:
    video_path: str
    video_url: str = ""
    used_model: str = ""
    channel_name: str = ""
    elapsed_seconds: float = 0.0


def normalize_video_provider_type(value: Any) -> str:
    value = str(value or "").strip().lower().replace("-", "_")
    if value in {"agnes", "agnes_video", "agnes_ai"}:
        return "agnes"
    if value in {
        "openai_video",
        "openai_compatible",
        "openai_videos",
        "video_async",
        "async",
        "async_task",
    }:
        return "openai_video"
    return ""


def build_video_generations_endpoint(base_url: str) -> str:
    base = str(base_url or "").strip().rstrip("/")
    if not base:
        return ""
    lowered = base.lower()
    if lowered.endswith("/videos/generations"):
        return base
    if lowered.endswith("/v1/videos") or lowered.endswith("/videos"):
        return f"{base}/generations"
    if lowered.endswith("/v1"):
        return f"{base}/videos/generations"
    if "/v1/" in lowered:
        return f"{base}/videos/generations"
    return f"{base}/v1/videos/generations"


def build_agnes_videos_endpoint(base_url: str) -> str:
    base = str(base_url or "").strip().rstrip("/")
    if not base:
        return ""
    lowered = base.lower()
    if lowered.endswith("/v1/videos"):
        return base
    if lowered.endswith("/videos"):
        return base
    if lowered.endswith("/v1"):
        return f"{base}/videos"
    return f"{base}/v1/videos"


def _agnes_model_family(model: str) -> str:
    normalized = str(model or "").lower().replace("_", "-")
    if "agnes-video-2.5-flash" in normalized or "agnes-video-25-flash" in normalized:
        return "v25_flash"
    if "agnes-video-2.5" in normalized or "agnes-video-25" in normalized:
        return "v25"
    return "v20"


def _aspect_ratio(size: str) -> str:
    raw = str(size or "").strip().lower().replace("×", "x")
    if re.fullmatch(r"\d+:\d+", raw):
        return raw
    match = re.fullmatch(r"(\d{2,4})\s*x\s*(\d{2,4})", raw)
    if not match:
        return ""
    width, height = int(match.group(1)), int(match.group(2))
    if not width or not height:
        return ""
    ratio = width / height
    common = ((16, 9), (9, 16), (1, 1), (4, 3), (3, 4), (3, 2), (2, 3))
    nearest = min(common, key=lambda pair: abs(pair[0] / pair[1] - ratio))
    return f"{nearest[0]}:{nearest[1]}" if abs(nearest[0] / nearest[1] - ratio) < 0.08 else ""


def _agnes_dimensions(size: str) -> Tuple[int, int]:
    value = str(size or "").strip().lower().replace("×", "x")
    preset = {
        "16:9": (1152, 768),
        "9:16": (768, 1152),
        "1:1": (1024, 1024),
        "4:3": (1152, 864),
        "3:4": (864, 1152),
    }
    if value in preset:
        return preset[value]
    match = re.fullmatch(r"(\d{3,4})\s*x\s*(\d{3,4})", value)
    if match:
        return max(64, int(match.group(1))), max(64, int(match.group(2)))
    return (768, 1152) if "9:16" in value else (1152, 768)


def _agnes_media_list(value: Any) -> List[str]:
    if isinstance(value, str):
        values = [value]
    elif isinstance(value, (list, tuple)):
        values = list(value)
    else:
        values = []
    result: List[str] = []
    for item in values:
        if isinstance(item, dict):
            item = item.get("url") or item.get("source_url") or item.get("uri") or ""
        text = str(item or "").strip()
        if text:
            result.append(text)
    return result


def _validate_http_media_references(values: Iterable[Any], *, protocol: str) -> None:
    for value in values:
        for media_url in _agnes_media_list(value):
            parsed = urlparse(media_url)
            if parsed.scheme not in {"http", "https"} or not parsed.netloc:
                raise VideoGenerationError(
                    f"{protocol} 视频参考媒体必须是可公开访问的 HTTP(S) URL；未提交任务。",
                    stage="validate",
                )


def _agnes_payload(target: VideoTarget, request: VideoGenerateRequest) -> Dict[str, Any]:
    extra = dict(request.extra or {})
    family = _agnes_model_family(target.model)
    prompt = str(request.prompt or "").strip()
    first_frame = str(request.first_frame or extra.get("first_frame") or "").strip()
    if not first_frame:
        first_frame = str(extra.get("image") or extra.get("image_url") or "").strip()
    last_frame = str(extra.get("last_frame") or "").strip()
    keyframes = _agnes_media_list(extra.get("keyframes") or [])
    images = _agnes_media_list(extra.get("images"))
    audios = _agnes_media_list(extra.get("audios"))
    videos = _agnes_media_list(extra.get("videos"))
    if not first_frame and keyframes:
        first_frame = keyframes[0]
    if not last_frame and len(keyframes) > 1:
        last_frame = keyframes[1]
    if not first_frame and family == "v20" and images:
        first_frame = images[0]
        if not keyframes and len(images) > 1:
            keyframes = images

    _validate_http_media_references(
        (first_frame, last_frame, keyframes, images, audios, videos),
        protocol="Agnes",
    )
    if family in {"v25", "v25_flash"}:
        flash = family == "v25_flash"
        duration = max(4, min(12, int(request.duration or 5)))
        size = str(extra.get("quality") or extra.get("size") or request.size or "720P").upper()
        if size not in {"720P", "1080P", "1K", "2K"}:
            size = "720P"
        if flash:
            size = "720P"
        aspect = str(extra.get("aspect_ratio") or "").strip()
        aspect = aspect or _aspect_ratio(request.size) or "16:9"
        if not re.fullmatch(r"\d+:\d+", aspect):
            aspect = "16:9"
        explicit_mode = str(extra.get("mode") or "").strip().lower()
        if explicit_mode in {"keyframes", "key_frame", "i2v"}:
            explicit_mode = "keyframe"
        if explicit_mode not in {"text", "keyframe", "reference"}:
            explicit_mode = ""
        if explicit_mode:
            mode = explicit_mode
        elif first_frame or last_frame:
            mode = "keyframe"
        elif images or audios or videos:
            mode = "reference"
        else:
            mode = "text"
        if mode == "reference" and not images and first_frame:
            images = [first_frame]
        if mode == "reference" and flash and videos:
            raise VideoGenerationError("Agnes Video 2.5 Flash 不支持视频参考输入。", stage="validate")
        payload: Dict[str, Any] = {
            "model": target.model,
            "prompt": prompt,
            "seconds": str(duration),
            "mode": mode,
            "size": size,
            "aspect_ratio": aspect,
            "n": 1,
        }
        if mode == "keyframe":
            if first_frame:
                payload["first_frame"] = first_frame
            if last_frame:
                payload["last_frame"] = last_frame
        elif mode == "reference":
            if images:
                payload["images"] = images[:5] if flash else images
            if audios:
                payload["audios"] = audios[:3] if flash else audios
            if videos and not flash:
                payload["videos"] = videos
    else:
        fps = max(1, min(60, int(extra.get("frame_rate") or extra.get("fps") or 24)))
        try:
            frames = int(extra.get("num_frames") or 0)
        except (TypeError, ValueError):
            frames = 0
        if frames <= 0:
            target_frames = max(1, int(request.duration or 5)) * fps
            frames = int(round((target_frames - 1) / 8) * 8 + 1)
        elif (frames - 1) % 8:
            frames = int(round((frames - 1) / 8) * 8 + 1)
        frames = max(9, min(441, frames))
        width, height = _agnes_dimensions(str(request.size or extra.get("size") or extra.get("aspect_ratio") or ""))
        if extra.get("width") and extra.get("height"):
            try:
                width, height = int(extra["width"]), int(extra["height"])
            except (TypeError, ValueError):
                pass
        payload = {
            "model": target.model,
            "prompt": prompt,
            "width": width,
            "height": height,
            "num_frames": frames,
            "frame_rate": fps,
        }
        if first_frame:
            payload["image"] = first_frame
        if keyframes:
            payload["extra_body"] = {"image": keyframes, "mode": "keyframes"}
            payload.setdefault("mode", "keyframes")
        elif extra.get("mode"):
            payload["mode"] = str(extra["mode"])
    supported_options = ("seed", "negative_prompt")
    if family == "v20":
        supported_options += ("num_inference_steps",)
    for key in supported_options:
        if extra.get(key) is not None:
            payload[key] = extra[key]
    return payload


def _compatible_payload(target: VideoTarget, request: VideoGenerateRequest) -> Dict[str, Any]:
    payload: Dict[str, Any] = {
        "model": target.model,
        "prompt": str(request.prompt or "").strip(),
    }
    if request.duration:
        payload["duration"] = int(request.duration)
    if request.size:
        payload["size"] = request.size
        ratio = _aspect_ratio(request.size)
        if ratio:
            payload["aspect_ratio"] = ratio
    extra = dict(request.extra or {})
    image_candidates = [
        request.first_frame,
        extra.pop("image", ""),
        extra.pop("image_url", ""),
        extra.get("first_frame", ""),
    ]
    image = next((str(value).strip() for value in image_candidates if str(value or "").strip()), "")
    images = _agnes_media_list(extra.pop("images", []))
    _validate_http_media_references(
        (
            image_candidates,
            images,
            extra.get("last_frame"),
            extra.get("keyframes"),
            extra.get("videos"),
            extra.get("audios"),
        ),
        protocol="兼容渠道",
    )
    if image:
        payload["image"] = image
    elif images:
        payload["images"] = images
    target_extra = target.extra.get("extra_body") if isinstance(target.extra, dict) else None
    if isinstance(target_extra, dict) and target_extra:
        payload["extra_body"] = target_extra
    model_extra = target.extra.get("model_extra") if isinstance(target.extra, dict) else None
    if isinstance(model_extra, dict):
        extra = {**model_extra, **extra}
    for key, value in extra.items():
        if key not in {"model", "prompt", "duration", "size", "image", "first_frame"}:
            payload[key] = value
    return payload


def build_create_payload(target: VideoTarget, request: VideoGenerateRequest) -> Dict[str, Any]:
    provider = normalize_video_provider_type(target.provider_type)
    if provider == "agnes":
        return _agnes_payload(target, request)
    if provider == "openai_video":
        return _compatible_payload(target, request)
    raise VideoGenerationError(f"不支持的视频协议：{target.provider_type}", stage="validate")


def _walk_dicts(value: Any) -> Iterable[Dict[str, Any]]:
    if isinstance(value, dict):
        yield value
        for child in value.values():
            yield from _walk_dicts(child)
    elif isinstance(value, list):
        for child in value:
            yield from _walk_dicts(child)


def _extract_video_url(value: Any) -> str:
    if isinstance(value, str):
        text = value.strip().strip("\"'")
        if text.lower().startswith("data:video/"):
            return text
        match = re.search(r"https?://[^\s<>\]\)\"']+", text)
        if match:
            return match.group(0).rstrip(".,;:")
        return ""
    for item in _walk_dicts(value):
        for key in ("b64_json", "base64", "b64"):
            encoded = item.get(key)
            if isinstance(encoded, str) and encoded.strip():
                candidate = encoded.strip()
                if candidate.startswith("data:video/"):
                    return candidate
                return f"data:video/mp4;base64,{candidate}"
        metadata = item.get("metadata")
        if isinstance(metadata, dict):
            result = _extract_video_url(metadata.get("url") or metadata.get("video_url") or "")
            if result:
                return result
        for key in ("video_url", "output_url", "download_url", "url"):
            found = item.get(key)
            if isinstance(found, str):
                result = _extract_video_url(found)
                if result:
                    return result
        for key in ("video", "output", "outputs", "result", "data", "content", "text"):
            if key in item:
                child = item[key]
                result = _extract_video_url(child)
                if result:
                    return result
    return ""


def _extract_task_id(value: Any, *, prefer_video_id: bool = False) -> str:
    items = list(_walk_dicts(value))
    keys = ("video_id", "task_id", "request_id", "id")
    if prefer_video_id:
        keys = ("video_id", "task_id", "id", "request_id")
    for key in keys:
        for item in items:
            found = item.get(key)
            if found is not None and str(found).strip():
                return str(found).strip()
    return ""


def _extract_agnes_legacy_task_id(value: Any) -> str:
    items = list(_walk_dicts(value))
    for key in ("task_id", "request_id", "id"):
        for item in items:
            found = item.get(key)
            if found is not None and str(found).strip():
                return str(found).strip()
    return ""


def _extract_status(value: Any) -> str:
    for item in _walk_dicts(value):
        for key in ("status", "task_status", "state"):
            found = item.get(key)
            if isinstance(found, str) and found.strip():
                return found.strip().upper().replace("-", "_")
    return ""


def _extract_error(value: Any) -> str:
    for item in _walk_dicts(value):
        found = item.get("error") or item.get("message")
        if isinstance(found, dict):
            found = found.get("message") or found.get("detail") or found
        if found:
            return str(found)
    return "视频任务失败"


def _redact(text: Any, target: VideoTarget) -> str:
    result = str(text or "")
    for key in target.resolved_api_keys():
        if key:
            result = result.replace(key, "[redacted]")
    result = re.sub(r"(?i)(bearer\s+)[^\s,;]+", r"\1[redacted]", result)
    return result


def _json_or_text(text: str) -> Any:
    try:
        return json.loads(text)
    except (TypeError, ValueError):
        stripped = str(text or "").strip()
        return {"url": stripped} if stripped.startswith(("http://", "https://")) else {"raw": stripped}


async def _request_json(
    session: aiohttp.ClientSession,
    method: str,
    url: str,
    *,
    headers: Dict[str, str],
    target: VideoTarget,
    payload: Optional[Dict[str, Any]] = None,
    timeout: int = 30,
    stage: str,
    use_urllib: bool = False,
) -> Any:
    if use_urllib and not str(target.proxy or "").lower().startswith(("socks5://", "socks5h://")):
        try:
            return await asyncio.to_thread(
                _request_json_urllib,
                method,
                url,
                headers,
                target,
                payload,
                timeout,
                stage,
            )
        except VideoGenerationError:
            raise
        except asyncio.CancelledError:
            raise
        except Exception as exc:
            raise VideoGenerationError(_redact(exc, target), stage=stage) from exc
    try:
        async with session.request(
            method,
            url,
            headers=headers,
            json=payload,
            timeout=aiohttp.ClientTimeout(total=max(1, timeout)),
            proxy=target.proxy or None,
        ) as response:
            text = await response.text()
            if response.status >= 400:
                raise VideoGenerationError(
                    _redact(f"HTTP {response.status}: {text[:800]}", target),
                    stage=stage,
                    status_code=response.status,
                )
            return _json_or_text(text)
    except VideoGenerationError:
        raise
    except asyncio.CancelledError:
        raise
    except Exception as exc:
        raise VideoGenerationError(_redact(exc, target), stage=stage) from exc


def _request_json_urllib(
    method: str,
    url: str,
    headers: Dict[str, str],
    target: VideoTarget,
    payload: Optional[Dict[str, Any]],
    timeout: int,
    stage: str,
) -> Any:
    body = json.dumps(payload, ensure_ascii=False).encode("utf-8") if payload is not None else None
    request = UrlRequest(url, data=body, headers=headers, method=method)
    proxy = str(target.proxy or "").strip()
    proxies = {"http": proxy, "https": proxy} if proxy else {}
    opener = build_opener(ProxyHandler(proxies)) if proxy else build_opener()
    try:
        with opener.open(request, timeout=max(1, timeout)) as response:
            status = response.status
            text = response.read().decode("utf-8", "replace")
    except HTTPError as exc:
        status = exc.code
        text = exc.read(1024 * 1024).decode("utf-8", "replace")
    except (URLError, TimeoutError, OSError) as exc:
        raise VideoGenerationError(_redact(exc, target), stage=stage) from exc
    except Exception as exc:
        raise VideoGenerationError(_redact(exc, target), stage=stage) from exc
    if status >= 400:
        raise VideoGenerationError(
            _redact(f"HTTP {status}: {text[:800]}", target),
            stage=stage,
            status_code=status,
        )
    return _json_or_text(text)


def _compatible_poll_url(endpoint: str, task_id: str, submission: Any) -> str:
    encoded = quote(task_id, safe="")
    if isinstance(submission, dict) and isinstance(submission.get("data"), list):
        parsed = urlparse(endpoint)
        return f"{parsed.scheme}://{parsed.netloc}/api/tasks/{encoded}"
    return f"{endpoint.rstrip('/')}/{encoded}"


def _agnes_poll_urls(
    target: VideoTarget,
    endpoint: str,
    video_id: str,
    legacy_task_id: str = "",
) -> List[str]:
    parsed = urlparse(target.base_url if "://" in target.base_url else f"https://{target.base_url}")
    origin = f"{parsed.scheme or 'https'}://{parsed.netloc}"
    query = {"video_id": video_id}
    if target.model:
        query["model_name"] = target.model
    urls = [f"{origin}/agnesapi?{urlencode(query)}"]
    legacy_id = legacy_task_id or video_id
    urls.append(f"{endpoint.rstrip('/')}/{quote(legacy_id, safe='')}")
    return list(dict.fromkeys(urls))


async def _poll(
    session: aiohttp.ClientSession,
    urls: List[str],
    *,
    target: VideoTarget,
    headers: Dict[str, str],
    deadline: float,
    use_urllib: bool = False,
) -> str:
    last_error = ""
    while time.monotonic() < deadline:
        await asyncio.sleep(min(max(1, target.poll_interval), max(0.0, deadline - time.monotonic())))
        for url in urls:
            try:
                data = await _request_json(
                    session,
                    "GET",
                    url,
                    headers=headers,
                    target=target,
                    timeout=min(30, max(1, int(deadline - time.monotonic()))),
                    stage="poll",
                    use_urllib=use_urllib,
                )
            except VideoGenerationError as exc:
                last_error = str(exc)
                if exc.status_code in {400, 401, 403, 422}:
                    raise
                continue

            status = _extract_status(data)
            if status in {"FAILED", "FAIL", "FAILURE", "ERROR", "CANCELLED", "CANCELED"}:
                raise VideoGenerationError(_redact(_extract_error(data), target), stage="poll")
            video_url = _extract_video_url(data)
            if video_url and status not in {"FAILED", "FAIL", "FAILURE", "ERROR"}:
                return video_url
            if status in {"SUCCEEDED", "SUCCESS", "COMPLETED", "COMPLETE", "DONE"}:
                raise VideoGenerationError("任务已完成，但响应中没有视频地址。", stage="poll")
    detail = f"：{last_error}" if last_error else ""
    raise VideoGenerationError(f"视频生成轮询超时{detail}", stage="poll")


def _download_video_urllib(
    url: str,
    *,
    target: VideoTarget,
    temp_path: str,
    max_bytes: int,
    timeout: int,
) -> Tuple[int, bytes]:
    request = UrlRequest(url, headers={"User-Agent": "SelfieVideo/0.1"}, method="GET")
    proxy = str(target.proxy or "").strip()
    proxies = {"http": proxy, "https": proxy} if proxy else {}
    opener = build_opener(ProxyHandler(proxies)) if proxy else build_opener()
    try:
        with opener.open(request, timeout=max(1, timeout)) as response:
            length = response.headers.get("Content-Length")
            if length and int(length) > max_bytes:
                raise VideoGenerationError("上游视频超过下载大小限制。", stage="download")
            header = bytearray()
            total = 0
            with open(temp_path, "wb") as handle:
                while True:
                    chunk = response.read(256 * 1024)
                    if not chunk:
                        break
                    total += len(chunk)
                    if total > max_bytes:
                        raise VideoGenerationError("上游视频超过下载大小限制。", stage="download")
                    if len(header) < 32:
                        header.extend(chunk[: 32 - len(header)])
                    handle.write(chunk)
        return total, bytes(header)
    except HTTPError as exc:
        body = exc.read(400).decode("utf-8", "replace")
        raise VideoGenerationError(
            _redact(f"视频下载 HTTP {exc.code}: {body}", target),
            stage="download",
            status_code=exc.code,
        ) from exc
    except VideoGenerationError:
        raise
    except Exception as exc:
        raise VideoGenerationError(
            _redact(exc, target), stage="download", retryable=True
        ) from exc


async def _download_video(
    session: aiohttp.ClientSession,
    url: str,
    *,
    target: VideoTarget,
    output_dir: str,
    max_bytes: int = 512 * 1024 * 1024,
    use_urllib: bool = False,
    timeout: Optional[int] = None,
) -> str:
    request_timeout = max(1, int(timeout or target.timeout))
    os.makedirs(output_dir, exist_ok=True)
    path = os.path.join(output_dir, f"video_{uuid.uuid4().hex}.mp4")
    temp_path = f"{path}.part"
    if url.lower().startswith("data:video/"):
        try:
            header, encoded = url.split(",", 1)
            raw = (
                base64.b64decode(encoded, validate=True)
                if ";base64" in header.lower()
                else unquote_to_bytes(encoded)
            )
        except (ValueError, binascii.Error) as exc:
            raise VideoGenerationError("上游返回的视频 data URL 无法解码。", stage="download") from exc
        if not raw or len(raw) > max_bytes:
            raise VideoGenerationError("上游视频为空或超过下载大小限制。", stage="download")
        header_bytes = raw[:32]
        total = len(raw)
        with open(temp_path, "wb") as handle:
            handle.write(raw)
    else:
        parsed_url = urlparse(url)
        if parsed_url.scheme not in {"http", "https"} or not parsed_url.netloc:
            raise VideoGenerationError("上游返回的视频地址不是有效的 HTTP(S) URL。", stage="download")
        header_bytes = bytearray()
        total = 0
        urllib_error: Optional[VideoGenerationError] = None
        if use_urllib and not str(target.proxy or "").lower().startswith(("socks5://", "socks5h://")):
            download_task = asyncio.create_task(
                asyncio.to_thread(
                    _download_video_urllib,
                    url,
                    target=target,
                    temp_path=temp_path,
                    max_bytes=max_bytes,
                    timeout=request_timeout,
                )
            )
            try:
                total, header_bytes = await asyncio.shield(download_task)
            except asyncio.CancelledError:
                def cleanup_after_cancel(future: asyncio.Future) -> None:
                    try:
                        future.exception()
                    except asyncio.CancelledError:
                        pass
                    try:
                        os.remove(temp_path)
                    except OSError:
                        pass

                download_task.add_done_callback(cleanup_after_cancel)
                raise
            except VideoGenerationError as exc:
                try:
                    os.remove(temp_path)
                except OSError:
                    pass
                if not exc.retryable:
                    raise
                urllib_error = exc
        if total == 0:
            header_bytes = bytearray()
            total = 0
            try:
                async with session.get(
                    url,
                    timeout=aiohttp.ClientTimeout(total=request_timeout),
                    proxy=target.proxy or None,
                ) as response:
                    if response.status >= 400:
                        body = await response.text()
                        raise VideoGenerationError(
                            _redact(f"视频下载 HTTP {response.status}: {body[:400]}", target),
                            stage="download",
                            status_code=response.status,
                        )
                    length = response.content_length
                    if length is not None and length > max_bytes:
                        raise VideoGenerationError("上游视频超过下载大小限制。", stage="download")
                    with open(temp_path, "wb") as handle:
                        async for chunk in response.content.iter_chunked(1024 * 256):
                            total += len(chunk)
                            if total > max_bytes:
                                raise VideoGenerationError("上游视频超过下载大小限制。", stage="download")
                            if len(header_bytes) < 32:
                                header_bytes.extend(chunk[: 32 - len(header_bytes)])
                            handle.write(chunk)
            except VideoGenerationError:
                try:
                    os.remove(temp_path)
                except OSError:
                    pass
                raise
            except asyncio.CancelledError:
                try:
                    os.remove(temp_path)
                except OSError:
                    pass
                raise
            except Exception as exc:
                try:
                    os.remove(temp_path)
                except OSError:
                    pass
                detail = f"；urllib={urllib_error}" if urllib_error else ""
                raise VideoGenerationError(
                    _redact(f"{exc}{detail}", target), stage="download"
                ) from exc
    try:
        if total < 12 or b"ftyp" not in header_bytes:
            raise VideoGenerationError("下载结果不是有效的 MP4 视频文件。", stage="download")
        os.replace(temp_path, path)
    finally:
        try:
            if os.path.exists(temp_path):
                os.remove(temp_path)
        except OSError:
            pass
    return path


async def generate_video(
    target: VideoTarget,
    request: VideoGenerateRequest,
    *,
    output_dir: str,
    session: Optional[aiohttp.ClientSession] = None,
) -> VideoGenerateResult:
    started = time.monotonic()
    deadline = started + max(1, int(target.timeout))
    provider = normalize_video_provider_type(target.provider_type)
    if not provider:
        raise VideoGenerationError(f"不支持的视频协议：{target.provider_type}", stage="validate")
    if not target.base_url or not target.model:
        raise VideoGenerationError("视频渠道缺少 Base URL 或模型名。", stage="validate")
    keys = target.resolved_api_keys()
    if not keys:
        raise VideoGenerationError("视频渠道缺少 API Key。", stage="validate")
    if not str(request.prompt or "").strip():
        raise VideoGenerationError("视频提示词不能为空。", stage="validate")

    owned_session = session is None
    client = session or aiohttp.ClientSession()
    try:
        last_error: Optional[VideoGenerationError] = None
        for index, api_key in enumerate(keys):
            headers = {
                "Authorization": f"Bearer {api_key}",
                "Content-Type": "application/json",
                "Accept": "application/json",
                "User-Agent": "SelfieVideo/0.1",
            }
            endpoint = build_agnes_videos_endpoint(target.base_url) if provider == "agnes" else build_video_generations_endpoint(target.base_url)
            payload = build_create_payload(target, request)
            create_remaining = deadline - time.monotonic()
            if create_remaining <= 0:
                raise VideoGenerationError("视频任务全局超时已到。", stage="create")
            try:
                # A create timeout is ambiguous and may already have incurred a charge.
                submission = await _request_json(
                    client,
                    "POST",
                    endpoint,
                    headers=headers,
                    target=target,
                    payload=payload,
                    timeout=min(90, max(1, int(create_remaining))),
                    stage="create",
                    use_urllib=provider == "agnes",
                )
            except VideoGenerationError as exc:
                last_error = exc
                if exc.status_code in {401, 402, 403, 429} and index + 1 < len(keys):
                    continue
                raise

            video_url = _extract_video_url(submission)
            if not video_url:
                task_id = _extract_task_id(submission, prefer_video_id=provider == "agnes")
                if not task_id:
                    raise VideoGenerationError(
                        "上游未返回视频地址或任务号。",
                        stage="create",
                    )
                if provider == "agnes":
                    legacy_task_id = _extract_agnes_legacy_task_id(submission) or task_id
                    poll_urls = _agnes_poll_urls(
                        target, endpoint, task_id, legacy_task_id
                    )
                else:
                    poll_urls = [_compatible_poll_url(endpoint, task_id, submission)]
                video_url = await _poll(
                    client,
                    poll_urls,
                    target=target,
                    headers=headers,
                    deadline=deadline,
                    use_urllib=provider == "agnes",
                )
            try:
                max_download_mb = max(1, min(2048, int(target.extra.get("max_download_mb", 512))))
            except (TypeError, ValueError):
                max_download_mb = 512
            download_remaining = deadline - time.monotonic()
            if download_remaining <= 0:
                raise VideoGenerationError("视频任务全局超时已到。", stage="download")
            path = await _download_video(
                client,
                video_url,
                target=target,
                output_dir=output_dir,
                max_bytes=max_download_mb * 1024 * 1024,
                use_urllib=provider == "agnes",
                timeout=max(1, int(download_remaining)),
            )
            return VideoGenerateResult(
                video_path=path,
                video_url=video_url if video_url.startswith(("http://", "https://")) else "",
                used_model=target.model,
                channel_name=target.channel_name,
                elapsed_seconds=round(time.monotonic() - started, 2),
            )
        if last_error:
            raise last_error
        raise VideoGenerationError("视频生成失败。", stage="create")
    finally:
        if owned_session:
            await client.close()


async def generate_video_with_fallback(
    targets: List[VideoTarget],
    request: VideoGenerateRequest,
    *,
    output_dir: str,
    session: Optional[aiohttp.ClientSession] = None,
) -> VideoGenerateResult:
    if not targets:
        raise VideoGenerationError("没有启用的视频渠道。", stage="select_channel")
    last_error: Optional[VideoGenerationError] = None
    for index, target in enumerate(targets):
        try:
            return await generate_video(target, request, output_dir=output_dir, session=session)
        except VideoGenerationError as exc:
            last_error = exc
            # Only explicit pre-task authentication/quota responses are safe to retry.
            if not (exc.stage == "create" and exc.status_code in {401, 402, 403, 429} and index + 1 < len(targets)):
                raise
    if last_error:
        raise last_error
    raise VideoGenerationError("视频生成失败。", stage="create")
