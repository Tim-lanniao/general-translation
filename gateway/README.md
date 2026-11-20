# 翻译网关

网关是这台电脑上唯一直接连接翻译模型（Strata 上的 Qwen）的进程。音频翻译和所有文本前端都只连它：它按需加载和卸载模型，给实时字幕留出优先级，统一关闭思考模式、加入本句术语、缓存文本译文，并管理局域网设备的 token。接口约定见 [docs/gateway-api.md](../docs/gateway-api.md)。

网关只依赖 Python 标准库和 filelock，不需要 Whisper、Qt 或 CUDA。

## 启动和停止

```powershell
.\translation-gateway.cmd start    # 已在运行时直接返回地址
.\translation-gateway.cmd status
.\translation-gateway.cmd stop     # 等正在生成的请求结束，卸载托管的模型
```

音频翻译启动字幕时会自动拉起网关。只用文本翻译时运行 `start`。`translation-gateway.cmd` 依次使用 `TRANSLATION_GATEWAY_PYTHON`、本目录的 `.venv`、`..\audio\.venv` 里的 Python；音频翻译自动拉起网关时也先看 `TRANSLATION_GATEWAY_PYTHON`。单独准备网关环境：

```powershell
py -3.12 -m venv .venv
.\.venv\Scripts\python.exe -m pip install -r requirements.txt
```

## 配置

配置保存在数据目录的 `gateway\config.json`，只有网关读写。[配置示例](config.example.json)

| 配置 | 用途 |
|---|---|
| `llm_url`、`llm_model`、`llm_label` | 推理服务地址、模型标识和显示名称。18081 是作者机器上 Strata 的内部端口 |
| `llm_controller` | 接受 `start`、`stop`、`status` 的 PowerShell 控制脚本，网关用它按需启停模型。留空时需要自行保持推理服务在线。网关调用时设置 `TRANSLATION_GATEWAY_CONTROLLER=1` 和 `AUDIO_TRANSLATION_CONTROLLER=1`，脚本可据此区分网关和手动测试 |
| `llm_parallel` | 推理服务能同时生成的请求数，默认 1。Strata 开启并行请求后填同样的数 |
| `gateway_port`、`gateway_idle_seconds` | 默认监听 127.0.0.1:18083，闲置 300 秒后卸载托管的模型 |
| `gateway_lan`、`gateway_networks` | 设为 `true` 后同时向局域网开放，见下文。网段默认只放行局域网私有地址 |
| `gateway_cache`、`gateway_cache_entries` | 文本翻译缓存，默认开启，最多 20,000 条 |
| `topic` | 文本请求没有带 `topic` 时使用的默认场景 |
| `gpu_lock` | 可选共享锁。字幕会话或托管模型占用显卡期间，与其他 GPU 作业互斥 |
| `cache_dir` | 缓存目录，默认为数据目录的 `gateway\cache` |

## 调度

字幕走 `/v1/captions` 实时通道：先开会话，再逐句提交。字幕排在等待中的文本请求前面。`llm_parallel` 为 1 时同一时间只生成一个请求，已开始生成的段落会先完成。设为 2 或更大时，文本请求最多同时占用 `llm_parallel − 1` 个，总给字幕留一个空位。批量前端请按段提交，单段最多生成 512 个 token，普通队列最多等待 32 项。收到 HTTP 429 后按 `Retry-After` 重试。若响应的 `finish_reason` 为 `length`，请缩短原段后重译。

网关始终以流式向模型取词，即使前端要的是普通 JSON 响应。前端断开连接（例如关掉网页或滚走）时，网关立即断开模型请求并释放空位。

## 共享术语

术语保存在数据目录的 `gateway\terms.txt`，字幕和文本请求都在下一次翻译时读取变更。可以在音频翻译控制台的「共享翻译术语」里编辑，也可以由本机程序通过 `GET`/`POST /admin/terms` 读写。每行一个词对，例如

```text
[注塑展]
Spritzguss=注塑
Werkzeugabnahme=模具验收
GX-120=原型机
```

网关只发送本句命中的词条，最多 12 条、合计 512 个字符。它按原词和德语词尾匹配，带数字的型号须匹配。字幕会额外送来识别热词中的近音原名；文本客户端按书面原文查词。OpenAI 风格的消息接口使用最后一条用户消息匹配术语。

## 文本翻译缓存

`/v1/translate` 和 `/v1/chat/completions` 的完整译文会写入缓存目录的 `translation-cache.sqlite3`。缓存键是发给模型的完整请求：模型、消息、本句命中的术语、采样参数。改动本句用到的术语后它会重新翻译，改动其他词条不影响已有缓存。命中时不排队、不加载模型，响应头带 `X-Gateway-Cache: hit`。只有 `temperature` 为 0 或未填写的请求会被缓存。字幕从不进入缓存；带 `Cache-Control: no-store` 请求头的请求（例如桌面入口的翻译）也不写入缓存。缓存里保存原文和译文，不需要时把 `gateway_cache` 设为 `false`，或运行 `translation-gateway.cmd cache-clear` 清空。

## 局域网里的其他设备

例如让同一局域网里的 Mac 也用这台电脑上的 Qwen：

1. 在 `gateway\config.json` 里设置 `"gateway_lan": true`，然后重启网关（`stop` 后重新 `start`）。
2. 为每台设备生成 token：`.\translation-gateway.cmd token-add mac`。token 只显示这一次，数据目录的 `gateway\devices.json` 只保存它的 SHA-256。`token-list` 查看设备，`token-revoke mac` 吊销，运行中的网关立即生效。
3. 用管理员 PowerShell 放行端口，只允许专用网络和该设备的地址：

   ```powershell
   New-NetFirewallRule -DisplayName "Translation gateway (LAN)" -Direction Inbound -Action Allow -Protocol TCP -LocalPort 18083 -Profile Private -RemoteAddress 192.168.1.20
   ```

   首次以局域网模式启动时若弹出 Windows 防火墙提示，只勾选「专用网络」并允许。点取消可能生成阻止规则，使上面的放行失效。
4. 建议在路由器里为两台电脑保留固定 IP。客户端的设置见[文本翻译](../text/README.md)。

其他设备只能使用 `/v1/translate`、`/v1/chat/completions`、`/v1/models`、`/health` 和 `/status`。字幕接口、会话、术语编辑和关闭网关只接受本机请求。请求必须带 `Authorization: Bearer <token>`，来源地址默认须在 10.0.0.0/8、172.16.0.0/12、192.168.0.0/16 或 IPv6 本地地址内，可用 `gateway_networks` 改为其他 CIDR 网段。局域网设备的跨域请求接受任意来源，但仍须携带 token。通信为明文 HTTP，请只在信任的网络里开放。

## 独占运行本地大模型测试

先在桌面停止字幕，再运行 `translation-gateway.cmd stop`。该命令等待正在生成的请求结束并卸载托管模型。`status` 显示 `service` 为 `stopped` 后，通过测试入口启动模型。配置了控制脚本的安装可用以下顺序：

```powershell
.\translation-gateway.cmd stop
.\translation-gateway.cmd test-start
.\translation-gateway.cmd test-check
# 在模型自身的接口运行开发测试
.\translation-gateway.cmd test-stop
```

网关在运行时持有独占锁，即使 Qwen 已空闲卸载，也必须先关闭网关。测试占用会阻止字幕或文本客户端重新拉起网关。异常中断后可重新执行 `test-stop`，模型确认停止后才会解除占用。

## 测试

在本目录运行，不需要音频依赖：

```powershell
.\.venv\Scripts\python.exe -m unittest discover -s tests
```
