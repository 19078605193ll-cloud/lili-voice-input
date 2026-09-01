# 浏览器接入

公开浏览器不能持有长期 `SERVICE_TOKEN`。SDK 默认调用 `POST /v1/anonymous-tokens`，使用保存在 `localStorage` 的随机 `client_id` 换取 10 分钟 JWT；Provider API Key 始终只保存在服务端。

```ts
const client = new VoiceInputClient({
  wsUrl: "wss://voice.example/v1/transcriptions/stream",
  fallbackUrl: "https://voice.example/v1/transcriptions",
  anonymousTokenUrl: "https://voice.example/v1/anonymous-tokens",
  workletUrl: "https://voice.example/sdk/pcm-worklet.js",
});

client.on("queued", ({ position, estimated_wait_ms }) => {
  status.textContent = `前方 ${position - 1} 个会话，预计等待 ${estimated_wait_ms}ms`;
});
client.on("final", ({ text }) => insertTranscript(text));
client.on("error", ({ code, message, retry_after_ms }) => showError(code, message, retry_after_ms));
```

SDK 状态包含 `idle`、`connecting`、`queued`、`recording`、`finalizing` 和 `error`。排队期间不申请麦克风；只有 `ready` 后才启动 AudioWorklet。连接初始化超时为 8 秒。

对 429、1013、1012 和其他可恢复错误，SDK 使用带抖动的 1/2/4 秒指数退避，最多重试 3 次，并优先采用服务端的 `retry_after_ms`。`CAPACITY_REACHED` 和 `QUEUE_TIMEOUT` 不自动触发 HTTP fallback。

SDK 监视 `WebSocket.bufferedAmount`：256KiB 为警戒值；超过 512KiB 且持续 3 秒会停止流式发送，标记 `BACKPRESSURE`，结束录音并仅尝试一次 HTTP fallback。页面隐藏、网络断开或 `destroy()` 时都会关闭麦克风和 AudioContext。

麦克风仅能在 HTTPS 或 localhost 使用。AudioWorklet 文件必须可直接访问并返回 JavaScript MIME 类型。Vue/React 等受控输入框要通过状态更新函数写入 final 文本，而不是只修改 DOM。
