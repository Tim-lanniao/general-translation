# 翻译网关接口

音频翻译、文本前端和局域网设备只通过这些 HTTP 接口使用网关。默认地址是 `http://127.0.0.1:18083`，OpenAI 风格的客户端填 Base URL `http://127.0.0.1:18083/v1`。改动这里列出的字段时，要同时更新调用它的组件。

## 谁能调用

| 来源 | 认证 | 可用接口 |
|---|---|---|
| 本机程序 | 不需要 token，API key 可填 `local`。`Host` 必须是本机地址；带 `Origin` 时只接受浏览器扩展和本机页面 | 全部 |
| 局域网设备（`gateway_lan: true`） | `Authorization: Bearer <设备 token>`，来源须在 `gateway_networks` 内 | `/v1/translate`、`/v1/chat/completions`、`/v1/models`、`/health`、`/status` |

错误统一返回 `{"error": {"message": "…"}}`。常见状态码：400 请求有误，401 缺少或无效的 token，403 来源或接口不允许，409 会话已结束或状态冲突，413/415 请求体过大或不是 JSON，429 队列已满（按 `Retry-After` 重试），502 模型出错，503 网关正在关闭。

## 翻译

| 接口 | 请求 | 用途 |
|---|---|---|
| `POST /v1/translate` | `source`（必填）、`source_language`（默认 `auto`）、`context`（取最后 512 字）、`topic`、`stream` | 译成简体中文，只输出当前语句 |
| `POST /v1/chat/completions` | OpenAI 格式的 `messages`（只接受文本），`stream` | 保留前端自己的翻译指令；用最后一条用户消息匹配术语 |
| `POST /v1/captions` | 同 `/v1/translate`，另需 `session`，可带最多 12 个 `asr_names` | 实时通道：排在文本请求前面，从不缓存。仅限本机 |
| `GET /v1/models` | — | 返回当前模型标识 |

三个翻译接口都接受 `max_tokens` 或 `max_completion_tokens`（最多 512，字幕默认 256），以及 `temperature`、`top_p`、`seed`、`stop`、`stream_options`、`response_format`、`frequency_penalty`、`presence_penalty`。网关总是关闭模型的思考模式，其他字段会被忽略。

响应与 OpenAI 的 `chat.completion` 相同；`stream: true` 时为 SSE，以 `data: [DONE]` 结束。响应头 `X-Gateway-Cache` 为 `hit` 或 `miss`。`finish_reason` 为 `length` 时请缩短原段后重译。

## 实时会话（仅限本机）

| 接口 | 请求 → 响应 |
|---|---|
| `POST /sessions/open` | `{}` → `{"session": "…", "terms_revision": "…"}`。打开会话时网关会占用 `gpu_lock` |
| `POST /sessions/heartbeat` | `{"session": "…"}` → 同上。90 秒内没有心跳的会话会过期，音频翻译每 10 秒发一次 |
| `POST /sessions/close` | `{"session": "…"}` → 同上 |

`terms_revision` 是共享术语内容的哈希。它变化时，客户端缓存的同一原文译文应作废。

## 管理（仅限本机）

| 接口 | 请求 → 响应 |
|---|---|
| `GET /admin/terms` | → `{"terms": "原文=译法…", "revision": "…", "entries": N}` |
| `POST /admin/terms` | `{"terms": "…"}` → `{"revision": "…", "entries": N}`，保存后下一次翻译即生效 |
| `POST /admin/stop` | `{}` → `{"stopping": true}`；还有字幕会话时返回 409 |

## 状态

| 接口 | 内容 |
|---|---|
| `GET /health` | `service`（固定为 `translation-gateway`）、`model`、`loaded`、`active`、`caption_sessions`、`queued_captions`、`queued_text`、`stopping` |
| `GET /status` | 模型状态（`unloaded`、`loading`、`ready`、`unloading`）、并行数、各队列、术语条数与版本、缓存条数、网络设置，以及每台设备的请求数、缓存命中率、首字与总耗时（P50/P95）。不含任何原文或译文 |
