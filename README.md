# Teams Live Caption · 德语 → 中文实时字幕

在本机把 Microsoft Teams 会议里的德语实时翻译成简体中文，显示在屏幕底部的悬浮字幕条上。语音识别和翻译都在本机 GPU 上运行，音频和文字不上传云端。

*Local, real-time German → Simplified Chinese captions for Microsoft Teams on Windows: WASAPI process-loopback capture of the Teams app, faster-whisper large-v3-turbo on CUDA, translation by a local LLM, always-on-top caption bar. UI and docs are in Chinese.*

![字幕条（示意）](docs/overlay.png)

<img src="docs/console.png" alt="控制台（示意）" width="560">

截图中的台词为演示用虚构内容。

## 功能

- **只听 Teams 的声音**：用 Windows 的进程回环采集（process loopback）只抓 Teams 程序及其子进程播放的声音，不混入其他程序和通知音，也不用选播放设备。Teams 未运行时暂用默认播放设备，检测到 Teams 后自动切换。也可选网页版 Teams（Edge/Chrome）或任意播放设备。
- **先快后准**：每 2 秒刷新一次临时字幕（黄色标记），说话停顿 600 ms 后重新完整识别并翻译成定稿（绿色标记）。一句话超过约 10 秒不停顿时，会在句号或逗号处先定稿前半句。
- **悬浮字幕条**：始终置顶、半透明；可拖动，拖边缘调宽度和行数，Ctrl+滚轮调字号；显示上一句和德语原文；默认在 Teams 共享屏幕或录屏时对其他人不可见；可设为鼠标穿透。
- **控制台**：开始、暂停、停止并释放显存；声音来源、翻译模型、会议主题、人名术语；定稿记录与“正在说”区域分开显示，可拖动调整大小。
- **会议记录**：每次会议的定稿字幕自动保存为 Markdown（时间、中文、德语原文）和 JSONL；不保存音频。
- 重复启动只会唤出已打开的窗口；连续 45 分钟无人说话时自动停止并释放显存。

## 运行要求

- Windows 11，或 Windows 10 2004 及以上（进程回环采集需要）。
- NVIDIA GPU 和驱动。识别模型约占 2 GB 显存；翻译模型另算：作者用的 Qwen3.8-Flash-Next IQ3_S 加识别时整块 24 GB 卡用到约 21 GB，Hy-MT2-7B Q6_K 模式约 11 GB。
- Python 3.12（`setup.ps1` 可借助 [uv](https://github.com/astral-sh/uv) 自动准备）。
- 一个本地翻译服务，任选其一：
  - **本地大模型（推荐）**：任何兼容 OpenAI `/v1/chat/completions` 流式接口的本地服务，例如 llama.cpp `llama-server`、LM Studio、Ollama。请求会附带 `chat_template_kwargs: {"enable_thinking": false}`，以关闭 Qwen3 类模型的思考输出。
  - **Hy-MT2-7B 省显存模式**：下载 llama.cpp 的 `llama-server.exe` 和 [tencent/Hy-MT2-7B-GGUF](https://huggingface.co/tencent/Hy-MT2-7B-GGUF) 的 `HY-MT2-7B-Q6_K.gguf`，本程序会自动启动和关闭它。

## 安装

```powershell
git clone https://github.com/Tim-lanniao/teams-live-caption
cd teams-live-caption
powershell -ExecutionPolicy Bypass -File setup.ps1
```

`setup.ps1` 会在 `.venv` 建立 Python 环境，安装 [requirements.txt](requirements.txt) 中固定版本的依赖和 CUDA 12 的 cuBLAS/cuDNN（[requirements-cuda.txt](requirements-cuda.txt)），从 Hugging Face 下载 [dropbox-dash/faster-whisper-large-v3-turbo](https://huggingface.co/dropbox-dash/faster-whisper-large-v3-turbo) 到 `models\`，最后运行环境自检。已有 CUDA 12 与 cuDNN 9 的 DLL（例如来自 PyTorch）时可加 `-SkipCudaLibs`，再在配置中填写 `cuda_dll_dir`。

然后把 [config.example.json](config.example.json) 复制为 `data\config.json`，按需修改翻译服务等设置（见下文）。

## 使用

1. 启动本地翻译服务（若已在配置中设置 `llm_controller` 或使用 Hy-MT2-7B 模式，可跳过）。
2. 双击 `启动Teams实时字幕.cmd`。默认会自动开始：加载识别模型和翻译模型，状态显示“字幕运行中”后，会议中其他人说的德语就会显示为中文。
3. 会议前建议在 Teams “设置 → 设备 → 进行测试通话”：回放你的声音时，控制台的输入音量条应该会动；不动就改选 Teams 实际使用的播放设备。

只翻译电脑播放出来的声音，即其他参会者的发言，不包括你自己的麦克风。

- `启动Teams实时字幕.cmd --quit`：让已运行的程序停止、释放显存并退出。
- `启动Teams实时字幕.cmd --no-auto-start`：打开后不自动开始。
- `.venv\Scripts\python.exe -m teams_caption.engine --replay 某个16kHz单声道.wav --record-dir 临时目录`：无界面按实时速度回放音频文件，以 JSON 行输出字幕和耗时，用于测试。

## 配置

设置保存在数据目录的 `config.json`。数据目录默认为仓库下的 `data\`，可用环境变量 `TEAMS_CAPTION_DATA` 改到别处；会议记录（`会议记录\`）和日志（`logs\`）也在其中。界面里改的设置会自动写回这个文件。和本机相关的设置如下：

| 键 | 说明 |
|---|---|
| `asr_model_dir` | faster-whisper 模型目录，默认 `models\faster-whisper-large-v3-turbo` |
| `cuda_dll_dir` | 含 `cublas64_12.dll`、`cudnn64_9.dll` 的目录；留空时使用 pip 安装的 nvidia 包或 PATH |
| `cache_dir` | 临时文件与缓存目录，默认在数据目录下 |
| `llm_url`、`llm_model`、`llm_label` | 本地大模型服务地址、模型名、界面显示名 |
| `llm_controller` | 可选：接受 `start`/`stop`/`status` 参数的 PowerShell 脚本；设置后，服务未运行时自动启动，退出时关闭由本程序启动的服务 |
| `llama_server`、`hymt_model`、`hymt_port` | Hy-MT2-7B 模式所需的 `llama-server.exe`、GGUF 路径和端口 |
| `gpu_lock` | 可选：与其他 GPU 任务共用的锁文件；被占用时拒绝开始，避免显存冲突 |
| `sample_wav` | 可选：16 kHz 单声道德语 WAV，会作为“测试：回放样例音频”出现在声音来源中 |

识别与刷新参数（`window_seconds` 12、`update_seconds` 2、`vad_final_silence_ms` 600、`beam_size` 5）来自作者本机的实时回放评测，一般不需要改。

## 工作原理

```
Teams 进程音频 ──WASAPI 进程回环──▶ 16 kHz 单声道 ──▶ Silero VAD（逐帧、因果）
   │                                                    │ 停顿 600 ms = 一句结束
   └─ 每 2 秒：对当前句（≤12 秒）用 faster-whisper large-v3-turbo 识别 ──▶ 本地 LLM 翻译 ──▶ 替换临时字幕
      停顿后：整句重新识别并翻译 ──▶ 定稿字幕 + 会议记录
```

- 采集（[capture.py](teams_caption/capture.py)）用 ctypes 直接调用 WASAPI，不依赖额外的音频库。WASAPI 在无声时不送数据，程序按时间补静音，保证 VAD 能看到停顿。
- 字幕始终是“替换”当前句，而不是把重叠窗口的结果拼接起来；不使用未来音频。
- 翻译提示词保留数字、否定、疑问和残句，只纠正上下文明显支持的识别错词；会议主题和人名术语会加入提示，人名术语也作为 Whisper 的提示词。

## 实测（作者本机）

RTX PRO 4000 Blackwell 24 GB、Windows 11，用一段 60 秒德语问答节目音频按实时速度回放（不含 Teams 和扬声器链路）：

| 翻译模型 | 临时字幕延迟（中位） | 停顿后定稿（中位） | 整卡显存 |
|---|---|---|---|
| Qwen3.8-Flash-Next IQ3_S | 约 1.6–1.8 秒 | 约 2.4–2.9 秒 | 约 21 GB |
| Hy-MT2-7B Q6_K | 约 0.5 秒 | 约 1 秒 | 约 11 GB |

临时字幕延迟是从最新音频到达算起；定稿延迟是从这句话说完算起。Hy-MT2-7B 更快，但词义错误明显更多（例如把“海参”译成“海黄瓜”）。尚未在真实 Teams 通话中测量端到端延迟。口音、噪声、多人同时说话都会降低效果；机器翻译可能出错，重要内容请以德语原文为准。

## 已知限制

- 只在作者的一台电脑上测试过；`setup.ps1` 尚未在全新电脑上完整验证。
- 新版 Teams 的通话声音应由 `ms-teams.exe` 的子进程播放；若进程回环采不到声音，请改选播放设备。
- 只支持 Windows 和 NVIDIA GPU；只做德语 → 简体中文。

## 许可证

[MIT](LICENSE)。依赖库和模型各有自己的许可证，使用前请自行查看。
