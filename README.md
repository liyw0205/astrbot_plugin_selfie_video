# Selfie Video 视频生成

AstrBot 视频生成插件，首期提供 Agnes 视频协议与 OpenAI 兼容异步视频协议。

## 安装与配置

将仓库目录安装为 `astrbot_plugin_selfie_video`，在 AstrBot 插件设置中配置 Web 管理面板后重载插件。首次启动会在 AstrBot 数据目录的 `plugin_data/astrbot_plugin_selfie_video/selfie_video_config.json` 创建配置文件。可以在 Web 面板的“渠道与模型”页配置渠道，也可以把 `config.example.json` 中的渠道配置复制到该文件并填写自己的 Base URL、模型和 API Key。面板默认监听 `127.0.0.1:14515`，仅回环可访问；首次启动生成随机 Token 并保存在该配置文件中，不写入日志。

配置示例不包含可用凭据。不要把真实 API Key 写进仓库、日志或截图。

渠道的 `provider_type` 必须明确设置为 `agnes` 或 `openai_video`；不会只根据模型名猜协议。每个渠道支持 `api_key` 或多 Key 字段 `api_keys`、`models` / `enabled_models`、代理 `proxy` 和按模型覆盖 `model_provider_types`。

## 指令

| 指令 | 用途 |
| --- | --- |
| `/视频 提示词 [--duration 5] [--size 16:9] [--image 图片URL]` | 创建文生视频或 URL 首帧图生视频任务 |
| `/视频任务` | 查看当前账号进行中的任务 |
| `/视频取消 任务号` | 取消当前账号的排队或运行中任务 |
| `/视频重试 任务号` | 使用当前配置重新提交已有任务 |
| `/视频记录` | 查看当前账号最近的视频生成记录 |
| `/视频帮助` | 查看指令用法 |

取消只停止本地轮询与下载；上游已受理的任务可能仍会继续运行并计费。

## 协议

- `agnes`：官方 `/v1/videos` 创建，支持 Agnes v2.0 与 v2.5 请求体，经 `agnesapi?video_id=...` 轮询，并保留 `/v1/videos/{id}` 兼容回退。Agnes 参考帧需为可公开访问的 HTTP(S) 图片 URL。
- `openai_video`：`/v1/videos/generations` 异步创建与任务轮询，兼容常见任务 ID、状态字段、任务状态和视频 URL 响应形状。

网络超时不会自动重复提交创建请求。仅明确的创建阶段认证或限流响应可以尝试后备 Key/渠道。

Web 面板使用 Token 认证，提供渠道编辑、任务筛选、取消、重试、MP4 播放和下载。默认全局最多保留 20 个待处理任务、同一用户每 5 秒提交一次；可在“生成设置”中调整。测试按钮会建立真实上游生成任务；轮询与下载在后台任务中完成，任务状态会写入 SQLite。插件重启时遗留的排队/运行中记录会标记为状态未知的失败，重试必须手动触发。检测到损坏的 JSON 配置时，插件会先以仅所有者可读的权限备份，再创建默认配置。
