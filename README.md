# 通用音频翻译 · 实时中文字幕

双击 `启动音频翻译.cmd`，播放器、浏览器、会议软件里的德语都会变成屏幕底部的中文字幕。默认监听全电脑播放的声音，语音识别与翻译都在本机运行。原文语言可在控制台改成自动检测、英语、法语、日语或西班牙语。

![可调整宽度、字号和行数的悬浮字幕条](docs/overlay.png)

<img src="docs/console.png" alt="通用音频翻译控制台" width="560">

## 日常使用

1. 双击启动器。字幕会自动连接网关，加载 Whisper 和 Qwen。旧的 `启动Teams实时字幕.cmd` 也可以继续使用。
2. 播放音频。黄色区域逐字显示当前译文，定稿留在上方记录里。拖动两区之间的分隔线可调整高度，拖动悬浮条边缘可调整宽度和行数，Ctrl + 滚轮可调字号。
3. 打开「设置」可切换到单个播放设备、Teams 桌面版、Edge 或 Chrome。系统回环覆盖多个播放设备，较旧的 Windows 会回退到默认播放设备并在控制台提示。「全电脑声音」采集应用的播放输出，麦克风输入需经应用播放才能进入字幕。[Windows 跨设备回环说明](https://learn.microsoft.com/en-us/samples/microsoft/windows-classic-samples/applicationloopbackaudio-sample/)
4. 「暂停」保留识别模型。「停止字幕并释放识别显存」释放 Whisper，其他文本客户端可以继续使用 Qwen。没有字幕会话和翻译请求后，网关默认等待 5 分钟再卸载 Qwen。持续 45 分钟无人说话时字幕会自动停止。

字幕条可置顶、鼠标穿透，默认在支持的屏幕共享中隐藏。定稿写入数据目录的 `会议记录\`，包含时间、原文和译文，格式为 Markdown、JSONL。录音不会写入记录。

## 安装与配置

需要 Windows、NVIDIA CUDA、Python 3.12，以及能提供 OpenAI 风格文本推理接口的本地模型。跨设备采集要求 Windows build 20348 或更高。作者的配置为 faster-whisper large-v3-turbo FP16 和 Strata 上的 Qwen3.8-Flash-Next IQ3_S，运行中整卡显存曾达到约 21 GiB。

```powershell
git clone https://github.com/Tim-lanniao/teams-live-caption
cd teams-live-caption
powershell -ExecutionPolicy Bypass -File setup.ps1
Copy-Item config.example.json data\config.json
```

安装脚本准备项目环境、Whisper 权重与 CUDA DLL。已有兼容 DLL 时可用 `-SkipCudaLibs` 并设置 `cuda_dll_dir`。配置保存在 `data\config.json`，可用 `TEAMS_CAPTION_DATA` 为所有客户端指定同一数据目录。[配置示例](config.example.json)

| 配置 | 用途 |
|---|---|
| `source`、`asr_language` | 默认 `system`、`de`，控制台可切换 |
| `asr_model_dir`、`cuda_dll_dir`、`cache_dir` | 识别模型、CUDA 库和缓存的位置 |
| `llm_url`、`llm_model`、`llm_label` | 网关访问的推理服务、模型标识和显示名称 |
| `llm_controller` | 接受 `start`、`stop`、`status` 的 PowerShell 控制脚本，网关用它按需启停模型。留空时需要自行保持上游服务在线 |
| `gateway_port`、`gateway_idle_seconds` | 网关默认监听 127.0.0.1:18083，闲置 300 秒后卸载托管的模型 |
| `gpu_lock` | 可选共享锁，字幕识别或托管模型占用显卡期间与其他 GPU 作业互斥 |
| `sample_wav` | 可选的 16 kHz 单声道 WAV，在声音来源中作为回放样例 |

## 字幕和文本共用翻译网关

字幕会自动启动网关。只用文本翻译时运行 `translation-gateway.cmd start`。第三方客户端填写 Base URL `http://127.0.0.1:18083/v1`。模型名可从 `GET /v1/models` 查询。要求填写 API key 的本地前端可填 `local`。18081 是这台机器上 Strata 的内部端口。

| 接口 | 用法 |
|---|---|
| `POST /v1/translate` | 提交 `source`，可选 `source_language`、`context`、`topic`、`stream`，返回简体中文 |
| `POST /v1/chat/completions` | OpenAI 风格的文本消息接口，支持 JSON 或 SSE 流式返回，保留前端的翻译指令 |
| `GET /health` | 查看模型加载状态、字幕会话数及两条等待队列 |

网关统一关闭 Qwen 的思考模式，并把当前句匹配的词对加入提示。单槽模型同一时间只生成一个请求。字幕会排在等待中的批量请求前面，已开始生成的段落会先完成。批量前端请按段提交，单段最多生成 512 个 token，普通队列最多等待 32 项。收到 HTTP 429 后按 `Retry-After` 重试。若响应的 `finish_reason` 为 `length`，请缩短原段后重译。

```powershell
$body = @{ source = 'Der Termin ist am Dienstag.'; source_language = 'de' } | ConvertTo-Json
$result = Invoke-RestMethod -Method Post -Uri 'http://127.0.0.1:18083/v1/translate' -ContentType 'application/json; charset=utf-8' -Body ([Text.Encoding]::UTF8.GetBytes($body))
$result.choices[0].message.content
```

## 识别热词与共享翻译术语

在控制台打开「识别热词」可维护本地德语人名、产品和场景。科隆音码帮助匹配近音，支持 `[注塑展]` 分组和 `[]` 通用组。「内容主题」与最近定稿影响选择。每次最多送入 32 个名称，实际 Whisper 分词器计算整条提示，包含空格和标点在内不超过 223 个 token。

「共享翻译术语」保存到数据目录的 `gateway-terms.txt`。字幕和文本请求都在下一次翻译时读取变更。已有安装的术语会在首次启动时复制一份，此后可分别编辑。每行填写一个词对，例如

```text
[注塑展]
Spritzguss=注塑
Werkzeugabnahme=模具验收
GX-120=原型机
```

网关只发送本句命中的词条，最多 12 条、合计 512 个字符。它按原词和德语词尾匹配，带数字的型号须匹配。字幕程序还会把识别热词中无歧义的近音还原为原名，由网关查找指定译法；文本客户端按书面原文查词。第三方消息接口使用最后一条用户消息匹配术语。

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

## 字幕延迟与回放

默认在开口约 1.4 秒后首次识别，此后约每 2 秒修订一次。普通临时识别用 beam 1，长句切分和定稿用 beam 5。Silero 首次判定静音时提前处理句尾，重新开口会使旧结果失效。已覆盖最后一帧语音且带有句末标点时，最快静音 384 毫秒确认，其余默认等 600 毫秒。只有完整译文会写入记录。

```powershell
.\.venv\Scripts\python.exe -m teams_caption.engine --replay sample.wav --record-dir replay-output
.\.venv\Scripts\python.exe -m unittest discover -s tests
```

## 字幕流水线的历史实测

以下数据来自引入共享网关之前的版本。2026-11-19 在 Windows 11、RTX PRO 4000 Blackwell 24 GB 上，通过同一组 3 段各 60 秒的德语问答录音，对照上一版 `1b49261`。两次都使用预热后的 Whisper Turbo FP16 和 Qwen3.8-Flash-Next IQ3_S，关闭思考，术语表留空。两个版本分别产生 7 和 8 次停顿定稿。时间从 VAD 检测到开口或最后一帧语音算起，终点是引擎向界面发出字幕事件。

| 耗时（秒） | 优化前中位 | 优化后中位 | 优化前 P95 | 优化后 P95 |
|---|---:|---:|---:|---:|
| 开口 → 首个汉字 | 2.39 | 2.20 | 2.47 | 2.79 |
| 开口 → 第一版完整译文 | 2.45 | 2.35 | 2.61 | 2.86 |
| 语音结束 → 停顿定稿 | 1.98 | 1.43 | 2.84 | 2.41 |

8 次停顿中有 7 次用上了提前识别的结果。短暂停顿后继续说话也会触发试算，这次回放的识别调用从 100 增至 172 次。整卡显存采样峰值约 20.6 GiB。缩短断句会改变分段，小样本中首字 P95 有所上升。实际 Teams 通话的端到端延迟仍待实测。

当时参与对照、现已退役的 Hy‑MT2‑7B 单独回放其中 60 秒，停顿定稿中位数从 1.82 秒降至 0.84 秒，旧版 2 次停顿，新版 3 次。两种模型的定稿均保留了 19、80、800、190 公斤四个选项。

词表从 503 条扩到 5,003 条后，同一句展会德语仍只匹配到 3 条，传给翻译的术语文本都是 43 个字符。实际 Whisper 分词器计得识别提示为 216 个 token。用含 504 条术语的配置从正常入口回放 19 秒原声，Qwen 定稿写出了「海参」和「裸鼹鼠」。两种模型在独立的术语句测试中也都采用了这两个译名，产品型号有时仍会按原文输出。

单独测试采集端的重采样，对 10 毫秒音频包重复计时，每组 1,000 包，取 6 组的中位数。48 kHz 转 16 kHz 从每包 50 降到 14 微秒，44.1 kHz 转 16 kHz 从 387 降到 23 微秒。分包输出已逐样本与整段重采样核对。可用 `.venv\Scripts\python.exe -m unittest discover -s tests` 检查分包采样、流式预览和停顿定稿。

## 已知限制

- 只在作者的一台电脑上测试过；`setup.ps1` 尚未在全新电脑上完整验证。
- 新版 Teams 的通话声音应由 `ms-teams.exe` 的子进程播放；若进程回环采不到声音，请改选播放设备。
- 当前桌面版使用 Windows 与 NVIDIA CUDA。实时延迟的历史评测使用德语音频。

## 许可证

[MIT](LICENSE)。依赖库和模型各有自己的许可证，使用前请自行查看。
