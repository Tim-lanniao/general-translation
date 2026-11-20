# general-translation：音频翻译、文本翻译与共用网关

一台 Windows 电脑上的一个本地翻译模型（Strata 上的 Qwen），同时服务两类用途：把电脑播放的外语实时变成中文字幕，以及桌面入口、浏览器、划词工具和局域网里的其他设备发来的文本翻译。两类前端各自独立，只通过 HTTP 共用中间的翻译网关。

日常使用双击 `text\windows\启动翻译.cmd`：默认只开文本翻译（网关和 Qwen，不加载 Whisper），需要字幕时在窗口里点「打开音频翻译」。

```text
text/   桌面入口、浏览器扩展、划词工具、脚本 ──┐  /v1/translate、/v1/chat/completions
        局域网里的 Mac（设备 token） ──────────┤
audio/  音频翻译（由桌面入口按需启动） ────────┘  /v1/captions（实时通道，优先）
                                               ▼
gateway/  翻译网关：调度、术语、缓存、设备 token ──▶ Strata 上的 Qwen
```

| 目录 | 内容 | 说明 |
|---|---|---|
| [gateway/](gateway/README.md) | `translation_gateway`：唯一连接模型的进程 | 只依赖标准库和 filelock |
| [audio/](audio/README.md) | `audio_caption`：采集、Whisper 识别、悬浮字幕 | Windows、CUDA、PySide6 |
| [text/](text/README.md) | `translation_desk`：日常入口，以及各文本前端的接入说明 | 只走 HTTP 和其他组件的命令行，PySide6 |
| [docs/gateway-api.md](docs/gateway-api.md) | 网关接口约定 | 组件之间唯一的正式接口 |

## 依赖规则

- 网关不导入音频或文本的任何代码，测试会检查它只用标准库和 filelock。
- 音频只使用网关的 `client`、`config`、`terms` 三个模块，其余都属于网关内部。从仓库直接运行时，`audio_caption` 会自动找到旁边的 `gateway/`。
- 文本前端不导入网关或音频的 Python 包，测试会检查。桌面入口通过网关的启动器和 HTTP 接口使用网关，通过音频翻译的命令行打开和关闭字幕；Mac 和其他设备的用法与本机相同。

## 数据目录

数据根目录取 `TRANSLATION_DATA`（相对路径按启动时的工作目录换算成绝对路径），没有设置时为仓库下的 `data\`。各组件各用一个子目录：

```text
<数据根目录>\
  gateway\  config.json  terms.txt  devices.json  cache\  logs\
  audio\    config.json  会议记录\  logs\
  text\     desk.json  logs\
```

## 测试

各组件分别在自己的目录里运行：

```powershell
cd gateway; ..\audio\.venv\Scripts\python.exe -m unittest discover -s tests; cd ..
cd audio; .\.venv\Scripts\python.exe -m unittest discover -s tests; cd ..
cd text\windows; .\.venv\Scripts\python.exe -m unittest discover -s tests; cd ..\..
```

## 许可证

[MIT](LICENSE)。依赖库和模型各有自己的许可证，使用前请自行查看。
