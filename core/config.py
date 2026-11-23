"""Video channel configuration normalization."""

from __future__ import annotations

import json
import re
from collections.abc import Mapping
from typing import Any, Dict, List

from ..generation.video import VideoTarget, normalize_video_provider_type


DEFAULT_CONFIG: Dict[str, Any] = {
    "video": {
        "enable": True,
        "default_duration": 5,
        "max_concurrent_tasks": 1,
        "max_pending_tasks": 20,
        "user_cooldown_seconds": 5,
        "global_timeout": 600,
        "poll_interval": 5,
        "max_download_mb": 512,
        "max_saved_videos": 100,
    },
    "video_channels": [],
    "enabled_video_model_priority": [],
    "permission": {"usable_users": "", "blocked_users": ""},
    "web": {"enable": True, "host": "127.0.0.1", "port": 14515, "token": "changeme"},
}


def _dict(value: Any) -> Dict[str, Any]:
    return value if isinstance(value, dict) else {}


def _as_list(value: Any) -> List[Any]:
    if isinstance(value, list):
        return value
    if isinstance(value, tuple):
        return list(value)
    return []


def _strings(value: Any) -> List[str]:
    if isinstance(value, (list, tuple)):
        values = value
    else:
        values = re.split(r"[\n\r,;]+", str(value or ""))
    return list(dict.fromkeys(str(item or "").strip() for item in values if str(item or "").strip()))


def _enabled(value: Any) -> bool:
    if isinstance(value, str):
        return value.strip().lower() not in {"0", "false", "no", "off", "disabled"}
    return bool(value)


def config_to_dict(config: Any) -> Dict[str, Any]:
    if config is None:
        return {}
    if isinstance(config, Mapping):
        return {str(key): _plain_value(value) for key, value in config.items()}
    for method_name in ("to_dict", "dict", "model_dump", "get_config"):
        method = getattr(config, method_name, None)
        if callable(method):
            try:
                result = method()
                if isinstance(result, Mapping):
                    return {str(key): _plain_value(value) for key, value in result.items()}
            except Exception:
                pass
    if hasattr(config, "items"):
        try:
            return dict(config.items())
        except Exception:
            pass
    return {}


def _plain_value(value: Any) -> Any:
    if isinstance(value, Mapping):
        return {str(key): _plain_value(item) for key, item in value.items()}
    if isinstance(value, (list, tuple)):
        return [_plain_value(item) for item in value]
    for method_name in ("to_dict", "dict", "model_dump"):
        method = getattr(value, method_name, None)
        if callable(method):
            try:
                converted = method()
                if isinstance(converted, Mapping):
                    return _plain_value(converted)
            except Exception:
                continue
    return value


def normalize_config(config: Any) -> Dict[str, Any]:
    raw = config_to_dict(config)
    if isinstance(raw.get("video_channels"), str):
        try:
            parsed = json.loads(raw["video_channels"])
            raw["video_channels"] = parsed if isinstance(parsed, list) else []
        except (TypeError, ValueError):
            raw["video_channels"] = []
    channels = raw.get("video_channels")
    if not isinstance(channels, list) and isinstance(raw.get("video_channels_json"), str):
        try:
            parsed = json.loads(raw["video_channels_json"])
            channels = parsed if isinstance(parsed, list) else []
        except (TypeError, ValueError):
            channels = []
    return {
        **DEFAULT_CONFIG,
        **raw,
        "video": {**DEFAULT_CONFIG["video"], **_dict(raw.get("video"))},
        "permission": {**DEFAULT_CONFIG["permission"], **_dict(raw.get("permission"))},
        "web": {**DEFAULT_CONFIG["web"], **_dict(raw.get("web"))},
        "video_channels": _as_list(channels),
    }


def build_video_targets(config: Any) -> List[VideoTarget]:
    normalized = normalize_config(config)
    video = normalized["video"]
    try:
        timeout = max(30, min(1800, int(video.get("global_timeout", 600))))
    except (TypeError, ValueError):
        timeout = 600
    try:
        poll_interval = max(1, min(30, int(video.get("poll_interval", 5))))
    except (TypeError, ValueError):
        poll_interval = 5
    try:
        max_download_mb = max(1, min(2048, int(video.get("max_download_mb", 512))))
    except (TypeError, ValueError):
        max_download_mb = 512

    targets: List[VideoTarget] = []
    for channel in normalized["video_channels"]:
        if not isinstance(channel, dict) or not _enabled(channel.get("enabled", True)):
            continue
        channel_name = str(channel.get("name") or channel.get("id") or "default").strip()
        base_url = str(channel.get("base_url") or channel.get("baseUrl") or "").strip()
        raw_keys = channel.get("api_keys") or channel.get("api_key") or channel.get("apiKey")
        keys = _strings(raw_keys)
        raw_models = channel.get("enabled_models") or channel.get("models") or channel.get("model")
        models = _strings(raw_models)
        provider_default = normalize_video_provider_type(
            channel.get("provider_type") or channel.get("providerType")
        )
        model_types = _dict(channel.get("model_provider_types") or channel.get("modelProviderTypes"))
        extra = {
            **_dict(channel.get("extra")),
            "max_download_mb": max_download_mb,
        }
        try:
            channel_timeout = max(30, min(timeout, int(channel.get("timeout", timeout))))
        except (TypeError, ValueError):
            channel_timeout = timeout
        try:
            channel_poll = max(1, min(30, int(channel.get("poll_interval", poll_interval))))
        except (TypeError, ValueError):
            channel_poll = poll_interval
        channel_id = str(channel.get("channel_id") or "").strip()
        for model in models:
            provider = normalize_video_provider_type(model_types.get(model)) or provider_default
            if not provider:
                continue
            target_extra = {
                **extra,
                "channel_id": channel_id,
                "extra_body": _dict(channel.get("extra_body")),
                "model_extra": _dict(channel.get("model_extra")),
            }
            targets.append(
                VideoTarget(
                    channel_name=channel_name,
                    provider_type=provider,
                    base_url=base_url,
                    api_key=keys[0] if keys else "",
                    api_keys=keys,
                    model=model,
                    timeout=channel_timeout,
                    poll_interval=channel_poll,
                    proxy=str(channel.get("proxy") or "").strip(),
                    extra=target_extra,
                )
            )

    priority = _strings(normalized.get("enabled_video_model_priority"))
    if priority:
        priority_indexes = {label: index for index, label in enumerate(priority)}
        targets.sort(
            key=lambda target: priority_indexes.get(
                f"{target.channel_name}/{target.model}",
                priority_indexes.get(target.model, len(priority_indexes)),
            )
        )
    return targets
