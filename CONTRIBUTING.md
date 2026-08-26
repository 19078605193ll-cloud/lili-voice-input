# Contributing

## 开发检查

```powershell
Set-Location server
uv sync
uv run pytest

Set-Location ..
npm install
npm run typecheck
npm test
npm run build
```

默认测试不得调用真实付费 Provider。真实 ASR 和润色验证应通过本地 `.env` 手动执行，并且不得提交音频、转写内容或密钥。

提交前确认：

- 没有 `.env`、令牌、音频或用户文本。
- WebSocket v1 没有 `partial` 事件。
- 新配置已经同步到 `.env.example` 和文档。
- 对外协议变更已经更新 `docs/protocol.md`。

