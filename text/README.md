# 文本翻译

文本翻译没有自己的服务，也不导入任何 Python 包。浏览器扩展、划词工具、脚本和局域网里的 Mac 都直接使用[翻译网关](../gateway/README.md)的 OpenAI 兼容接口，和音频翻译共用同一个 Qwen、同一份术语和缓存。字幕总是优先，文本请求在它后面排队。

先在这台电脑上启动网关：

```powershell
..\gateway\translation-gateway.cmd start
```

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

现成工具覆盖不到的部分会放进本目录，同样只通过 HTTP 调网关：`windows\` 放托盘里的快捷键改写和场景切换，`mac\` 放 Hammerspoon 脚本。目前还没有代码。
