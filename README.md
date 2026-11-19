# Teams Live Caption · 德语 → 中文实时字幕

在本机把 Microsoft Teams 会议里的德语实时翻译成简体中文，显示在屏幕底部的悬浮字幕条上。语音识别和翻译都在本机 GPU 上运行，音频和文字不上传云端。

*Local, real-time German → Simplified Chinese captions for Microsoft Teams on Windows: WASAPI process-loopback capture of the Teams app, faster-whisper large-v3-turbo on CUDA, translation by a local LLM, always-on-top caption bar. UI and docs are in Chinese.*

![字幕条（示意）](docs/overlay.png)

<img src="docs/console.png" alt="控制台（示意）" width="560">

截图中的台词为演示用虚构内容。

## 功能

- **只听 Teams 的声音**：用 Windows 的进程回环采集（process loopback）只抓 Teams 程序及其子进程播放的声音，不混入其他程序和通知音，也不用选播放设备。Teams 未运行时暂用默认播放设备，检测到 Teams 后自动切换。也可选网页版 Teams（Edge/Chrome）或任意播放设备。
- **实时字幕**。默认说话约 1.4 秒开始首轮识别，中文随翻译模型逐段出现在黄色临时区。后续约每 2 秒修订。检测到停顿时提前处理句尾，当前德语已成句时最快静音 384 毫秒确认，其他情况默认等 600 毫秒。长时间连续发言会分段定稿。
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

`first_update_seconds` 默认 1.4 秒，后续刷新间隔 `update_seconds` 默认 2 秒，滚动窗口 `window_seconds` 默认 12 秒。普通临时识别用 beam 1，长句切分和停顿定稿使用 `beam_size`，默认 5。`vad_punctuated_silence_ms` 和 `vad_final_silence_ms` 分别控制成句和常规停顿，默认 384 和 600 毫秒。两项设为相同值即可统一停顿时长。

### 在会前准备术语

在控制台的「会议主题」填入本次会议的场景，例如「注塑展技术交流」。打开「人名术语」，可一次粘贴几百行德中词对，保存后即生效。每行一个词对，方括号里的标题把后续词条分入相应场景，`[]` 切回通用组。

```text
Frau Schäfer=舍费尔女士
[注塑展]
Spritzguss=注塑
Werkzeugabnahme=模具验收
GX-120=原型机
```

识别时优先考虑会议主题点到的德文或中文名、最近一句定稿提到的词，以及主题包含的场景组，再用通用词补足。把组内最重要的产品排在前面。每次最多送入 32 个德文名称，用当前 Whisper 模型的分词器计算整条提示，含开头空格和标点，最多 223 个 token。[faster-whisper 1.2.1 源码](https://github.com/SYSTRAN/faster-whisper/blob/v1.2.1/faster_whisper/transcribe.py)

翻译时，Qwen 和 Hy‑MT 都只收到当前德语句子命中的词条。匹配支持人名的近音写法、常见德语词尾以及多词产品名，同音歧义会跳过，带数字的产品型号须匹配。每句最多带 12 条、合计 512 个字符。旧配置里用逗号分隔的德文人名可以继续使用。

## 工作原理

```
Teams 进程音频 ──WASAPI 进程回环──▶ 16 kHz 单声道 ──▶ Silero VAD（逐帧、因果）
   │                                                    │ 首帧静音 → 提前识别和翻译
   └─ 首轮约 1.4 秒，随后每 2 秒 ──▶ Whisper 临时识别 ──▶ 本地模型逐段生成中文 ──▶ 临时字幕
      同一次停顿达到阈值 ──▶ 复用已完成的结果，或等本句算完 ──▶ 定稿字幕 + 会议记录
```

- 采集（[capture.py](teams_caption/capture.py)）用 ctypes 直接调用 WASAPI，不依赖额外的音频库。WASAPI 在无声时不送数据，程序按时间补静音，保证 VAD 能看到停顿。
- 字幕始终是“替换”当前句，而不是把重叠窗口的结果拼接起来；不使用未来音频。
- Silero 每 32 毫秒处理一帧。语音概率首次低于 0.35 时唤醒识别线程。若上一轮临时翻译还在进行，此时也可以提前识别句尾。重新开口或出现不确定帧会使这次提前识别失效。停顿确认时，若缺少可复用结果，程序会识别整段尚未定稿的音频。
- 缩短停顿需要当前识别覆盖最后的语音，且以完整句末标点结束。缩写、序数和明显未完的连接词使用常规阈值。确认时间按到达的音频帧计算。翻译尚未完成时，黄色区域继续更新，完整译文才写入会议记录。
- 只有长句切分需要逐词对齐。出现积压时，从尚未定稿的开头处理。复用翻译前核对德语原文、前文、主题、本句命中的词条和上下文设置。

## 实测（作者本机）

2026-11-19 在 Windows 11、RTX PRO 4000 Blackwell 24 GB 上，通过同一组 3 段各 60 秒的德语问答录音，对照上一版 `1b49261`。两次都使用预热后的 Whisper Turbo FP16 和 Qwen3.8-Flash-Next IQ3_S，关闭思考，术语表留空。两个版本分别产生 7 和 8 次停顿定稿。时间从 VAD 检测到开口或最后一帧语音算起，终点是引擎向界面发出字幕事件。

| 耗时（秒） | 优化前中位 | 优化后中位 | 优化前 P95 | 优化后 P95 |
|---|---:|---:|---:|---:|
| 开口 → 首个汉字 | 2.39 | 2.20 | 2.47 | 2.79 |
| 开口 → 第一版完整译文 | 2.45 | 2.35 | 2.61 | 2.86 |
| 语音结束 → 停顿定稿 | 1.98 | 1.43 | 2.84 | 2.41 |

8 次停顿中有 7 次用上了提前识别的结果。短暂停顿后继续说话也会触发试算，这次回放的识别调用从 100 增至 172 次。整卡显存采样峰值约 20.6 GiB。缩短断句会改变分段，小样本中首字 P95 有所上升。实际 Teams 通话的端到端延迟仍待实测。

Hy‑MT2‑7B 单独回放其中 60 秒，停顿定稿中位数从 1.82 秒降至 0.84 秒，旧版 2 次停顿，新版 3 次。两种模型的定稿均保留了 19、80、800、190 公斤四个选项。

词表从 503 条扩到 5,003 条后，同一句展会德语仍只匹配到 3 条，传给翻译的术语文本都是 43 个字符。实际 Whisper 分词器计得识别提示为 216 个 token。用含 504 条术语的配置从正常入口回放 19 秒原声，Qwen 定稿写出了「海参」和「裸鼹鼠」。两种模型在独立的术语句测试中也都采用了这两个译名，产品型号有时仍会按原文输出。

单独测试采集端的重采样，对 10 毫秒音频包重复计时，每组 1,000 包，取 6 组的中位数。48 kHz 转 16 kHz 从每包 50 降到 14 微秒，44.1 kHz 转 16 kHz 从 387 降到 23 微秒。分包输出已逐样本与整段重采样核对。可用 `.venv\Scripts\python.exe -m unittest discover -s tests` 检查分包采样、流式预览和停顿定稿。

## 已知限制

- 只在作者的一台电脑上测试过；`setup.ps1` 尚未在全新电脑上完整验证。
- 新版 Teams 的通话声音应由 `ms-teams.exe` 的子进程播放；若进程回环采不到声音，请改选播放设备。
- 只支持 Windows 和 NVIDIA GPU；只做德语 → 简体中文。

## 许可证

[MIT](LICENSE)。依赖库和模型各有自己的许可证，使用前请自行查看。
