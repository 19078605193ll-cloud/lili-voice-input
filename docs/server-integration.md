# 服务端接入

不使用浏览器 SDK 的程序可以调用 `POST /v1/transcriptions`，以 multipart 表单上传 `file`，并可附带 `language`。

响应与 WebSocket `final` 使用相同结构，并包含 `"type": "final"`。错误同样统一为 `type`、`code`、`message` 和 `recoverable`。支持 WebM、MP4/M4A、MP3、WAV、Ogg、AAC 和 FLAC；服务端通过 FFmpeg 转成 16kHz 单声道 PCM WAV。

当配置 `SERVICE_TOKEN` 时：

```http
Authorization: Bearer your-service-token
```

生产服务应在反向代理处额外配置 TLS、请求体限制、访问日志脱敏和应用自己的身份认证。
