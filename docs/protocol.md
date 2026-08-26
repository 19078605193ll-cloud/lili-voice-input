# WebSocket protocol v1

端点：`/v1/transcriptions/stream`。

1. 客户端连接 WebSocket。
2. 客户端发送 `start` JSON。
3. 服务端返回 `ready`。
4. 客户端发送小端、单声道、16kHz PCM16 二进制帧。
5. 客户端发送 `commit`。
6. 服务端返回一次 `final` 或 `error`，随后关闭会话。

```json
{
  "type": "start",
  "protocol_version": "1",
  "format": "pcm16",
  "sample_rate": 16000,
  "language": "zh",
  "auth_token": "optional"
}
```

`ready.capabilities.partial` 在 v1 中固定为 `false`。客户端不得等待草稿事件。

`final.polish_status` 为 `applied`、`disabled` 或 `fallback`。`final.polish_reason` 在成功或主动关闭整理时为 `null`，回退时给出稳定原因。主动关闭整理不会设置 `degraded=true`；整理失败会返回原始合并 ASR 文本并设置 `degraded_stage="polish"`。

`final.degraded_stage` 只可能是 `"asr"`、`"polish"` 或 `null`。如果 ASR 分片和整理都发生降级，优先返回 `"asr"`，同时仍可通过 `polish_reason` 查看整理失败原因。

```json
{
  "type": "final",
  "text": "整理后的最终文本。",
  "polished": true,
  "polish_status": "applied",
  "polish_reason": null,
  "degraded": false,
  "degraded_stage": null
}
```

错误结构：

```json
{"type":"error","code":"ASR_TIMEOUT","message":"语音最终识别超时","recoverable":true}
```
