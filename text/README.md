# 文本翻译

文本翻译没有自己的翻译服务。桌面入口、浏览器扩展、划词工具、脚本和局域网里的 Mac 都直接使用[翻译网关](../gateway/README.md)的 OpenAI 兼容接口，和音频翻译共用同一个 Qwen、同一份术语和缓存。字幕总是优先，文本请求排在后面。本目录的程序不导入网关或音频翻译的 Python 包，只调用它们的启动器、命令行和 HTTP 接口。

## 日常入口：通用翻译（`windows\`）

双击 `windows\启动翻译.cmd` 后，默认只进入文本翻译模式：启动网关，预热 Qwen，不加载 Whisper。窗口和托盘图标随即出现。

- **文本翻译**：粘贴或输入原文，按 Ctrl + Enter 或「翻译」，译文逐字出现。原文语言可选自动检测或指定，译成简体中文、德语或英语。空行分隔的段落逐段翻译；译成中文时，后一段带上前一段作为上下文。
- **模型状态**：窗口顶部显示模型就绪、加载中，或已在空闲时卸载。网关闲置 5 分钟后会卸载 Qwen，下次翻译时自动重新加载，约需 25 秒。
- **音频翻译**：点「打开音频翻译」才启动[音频翻译](../audio/README.md)：加载 Whisper，开始实时字幕。点「关闭音频翻译」会停止字幕、释放 Whisper，文本翻译不受影响。字幕程序的运行状态以网关上的字幕会话为准。
- 关闭窗口后程序留在托盘里，右键托盘图标可以退出。退出后网关继续为浏览器扩展等工具服务。再次双击启动器会把已打开的窗口带到前面。
- 命令行参数：`--open-audio`、`--close-audio`、`--quit` 发给已运行的入口；`--no-warm` 启动时不预热模型。

准备环境（与音频翻译用同一版 Qt）：

```powershell
cd windows
py -3.12 -m venv .venv
.\.venv\Scripts\python.exe -m pip install -r requirements.txt
```

入口启动网关时，用 `gateway\translation-gateway.cmd` 选择的 Python；打开音频翻译时，用 `AUDIO_CAPTION_PYTHON` 或 `audio\.venv`。设置和日志保存在数据目录的 `text\`，日志只记录耗时和字数，不记录原文或译文。测试在 `windows\` 目录运行：`.\.venv\Scripts\python.exe -m unittest discover -s tests`。

## 本机的浏览器扩展和划词工具

在沉浸式翻译、Pot 这类支持「OpenAI 兼容」或「自定义 OpenAI」服务的工具里添加一个服务：

| 设置 | 填写 |
|---|---|
| Base URL | `http://127.0.0.1:18083/v1` |
| API Key | `local` |
| 模型 | `GET http://127.0.0.1:18083/v1/models` 返回的标识 |

术语由网关加入，前端的提示词可保持默认。批量翻译网页时把前端的并发数调低，网关单段最多生成 512 个 token，等待队列满时会返回 429。

## 局域网里的 Mac

按[网关说明](../gateway/README.md#局域网里的其他设备)开放局域网并为 Mac 生成 token，然后在 Mac 上的工具里填写：

| 设置 | 填写 |
|---|---|
| Base URL | `http://<Windows 电脑的 IP>:18083/v1` |
| API Key | 这台设备的 token |

## 脚本

PowerShell：

```powershell
$body = @{ source = 'Der Termin ist am Dienstag.'; source_language = 'de' } | ConvertTo-Json
$result = Invoke-RestMethod -Method Post -Uri 'http://127.0.0.1:18083/v1/translate' -ContentType 'application/json; charset=utf-8' -Body ([Text.Encoding]::UTF8.GetBytes($body))
$result.choices[0].message.content
```

Mac 的终端：

```sh
curl -s http://<Windows 电脑的 IP>:18083/v1/translate \
  -H "Authorization: Bearer $TOKEN" -H "Content-Type: application/json" \
  -d '{"source": "Der Termin ist am Dienstag.", "source_language": "de"}'
```

## 以后放在这里

快捷键改写输入框和场景切换会加到 `windows\` 的入口里；`mac\` 放 Hammerspoon 脚本。
