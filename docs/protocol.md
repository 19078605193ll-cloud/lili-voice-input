# WebSocket protocol v1

端点为 `/v1/transcriptions/stream`。连接建立后，客户端必须在 5 秒内发送 `start`。服务端先校验 Origin、令牌、协议和音频格式，再申请容量，因此未鉴权或不发送 `start` 的连接不会占用会话名额。

```json
{
  "type": "start",
  "protocol_version": "1",
  "format": "pcm16",
  "sample_rate": 16000,
  "language": "zh",
  "auth_token": "anonymous JWT or SERVICE_TOKEN"
}
```

有容量时立即返回 `ready`；暂时无容量但队列未满时，先返回：

```json
{
  "type": "queued",
  "position": 3,
  "estimated_wait_ms": 1200,
  "max_wait_ms": 5000
}
```

客户端只能在收到 `ready` 后启动 AudioWorklet 和采集 PCM。随后发送小端、单声道、16kHz PCM16 二进制帧，停止时发送 `{"type":"commit"}`。v1 不发送 partial；`ready.capabilities.partial` 固定为 `false`。

最终响应示例：

```json
{
  "type": "final",
  "text": "整理后的最终文本。",
  "polished": true,
  "polish_status": "applied",
  "polish_reason": null,
  "degraded": false,
  "degraded_stage": null,
  "segment_count": 2,
  "failed_segment_count": 0,
  "latency_ms": 940,
  "polish_latency_ms": 210,
  "total_latency_ms": 1150,
  "admission_wait_ms": 120,
  "asr_queue_wait_ms": 80
}
```

整理排队超过 3 秒或 Provider 失败时返回已合并的原始 ASR 文本，`polish_status="fallback"`。容量回退使用 `polish_reason="capacity_reached"`。部分 ASR 分片失败时返回成功分片并标记 `degraded_stage="asr"`；全部分片失败才返回 error。

错误响应包含稳定错误码和可选重试时间：

```json
{
  "type": "error",
  "code": "CAPACITY_REACHED",
  "message": "语音服务繁忙，请稍后重试",
  "recoverable": true,
  "retry_after_ms": 5000
}
```

容量满或队列超时后 WebSocket 使用 1013 关闭；滚动发布使用 1012。稳定错误码包括 `START_TIMEOUT`、`IDLE_TIMEOUT`、`QUEUE_TIMEOUT`、`CAPACITY_REACHED`、`RATE_LIMITED`、`BACKPRESSURE` 和 `SERVER_RESTART`。

HTTP fallback 使用 `/v1/transcriptions`。容量不足返回 429、`Retry-After: 5` 和 `retry_after_ms=5000`。客户端收到明确的 `CAPACITY_REACHED` 或 `QUEUE_TIMEOUT` 后不得立即转 HTTP，以免放大过载。
