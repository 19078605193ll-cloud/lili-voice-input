# lili-voice-input

一个可自托管、可嵌入任意网站的语音输入服务。浏览器在录音期间持续发送 16kHz PCM16 音频块；服务端滚动切片并提前识别，停止后合并结果，再通过一次普通文本整理调用返回最终文本。

> 当前仓库尚未选择开源许可证。在添加 `LICENSE` 前，请勿将它作为正式公开版本发布。

## 包含什么

- FastAPI HTTP + WebSocket 服务
- OpenRouter ASR Provider
- OpenAI-compatible 文本润色 Provider
- TypeScript Browser SDK（ESM + IIFE）
- 开发者集成工作台
- Docker 镜像、接入示例和离线测试

v1 不发送实时草稿。`ready.capabilities.partial` 固定为 `false`。

文本整理失败不会丢失已经成功的 ASR 结果：服务端会返回原始合并转写，并通过 `polish_status`、`polish_reason` 和 `degraded_stage` 标记降级状态。

## 本地启动

要求 Python 3.12、Node.js 20、`uv` 和 FFmpeg。

```powershell
Copy-Item .env.example .env
# 编辑 .env，至少配置 ASR_API_KEY 和 ASR_MODEL；启用润色时还要配置 POLISH_API_KEY、POLISH_MODEL。

Set-Location server
uv sync
uv run uvicorn lili_voice_input.main:app --host 127.0.0.1 --port 9100 --reload
```当前项目已经可以转写成功了，但是调试页面的前端有一点问题：录音控制和最终转写结果两个模块隔得太远，不方便用户操作。建议把最终转写结果部分移到录音控制区域块的下面，这样刚刚好。

另开一个终端启动调试页：

```powershell
npm install
npm run dev
```

打开 `http://127.0.0.1:5173/demo/`。API 文档位于 `http://127.0.0.1:9100/docs`。

## Docker

```powershell
Copy-Item .env.example .env
# 填写自己的 Provider 密钥
docker compose up --build
```

随后访问：

- 调试页：`http://127.0.0.1:9100/demo/`
- API 文档：`http://127.0.0.1:9100/docs`
- 健康检查：`http://127.0.0.1:9100/health/ready`

## Browser SDK

### ESM

```ts
import { VoiceInputClient } from "@lili-voice-input/browser";

const client = new VoiceInputClient({
  wsUrl: "ws://127.0.0.1:9100/v1/transcriptions/stream",
  fallbackUrl: "http://127.0.0.1:9100/v1/transcriptions",
  workletUrl: "http://127.0.0.1:9100/sdk/pcm-worklet.js",
});

client.on("final", ({ text }) => {
  document.querySelector("textarea").value = text;
});

await client.start();
// 用户点击停止时：await client.stop();
```

### Script 标签

```html
<script src="https://your-host.example/sdk/lili-voice-input.global.js"></script>
<script>
  const client = new LiliVoiceInput.VoiceInputClient({
    wsUrl: "wss://your-host.example/v1/transcriptions/stream",
    fallbackUrl: "https://your-host.example/v1/transcriptions",
    workletUrl: "https://your-host.example/sdk/pcm-worklet.js"
  });
</script>
```

SDK 不决定文本应该覆盖、插入还是追加到输入框；这个行为由宿主项目的小型适配层实现。

## HTTP 上传

```bash
curl -X POST http://127.0.0.1:9100/v1/transcriptions \
  -F "file=@sample.webm" \
  -F "language=zh"
```

如果配置了 `SERVICE_TOKEN`，HTTP 使用 `Authorization: Bearer ...`，WebSocket 在 `start.auth_token` 中传递。Provider API Key 永远不能放入浏览器。

## 项目边界

这是自托管组件，不提供用户账户、计费、公共托管 API 或云端密钥管理。调试页只是验证 SDK 和服务的工作台，接入方可以完全不使用它。

更多内容见 [docs/browser-integration.md](docs/browser-integration.md)、[docs/protocol.md](docs/protocol.md) 和 [docs/deployment.md](docs/deployment.md)。
