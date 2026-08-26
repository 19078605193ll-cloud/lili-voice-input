# 故障排查

## 麦克风按钮不可用

确认页面运行在 HTTPS、localhost 或 127.0.0.1，并检查浏览器麦克风权限。AudioWorklet 不支持时 SDK 会返回不支持状态。

## WebSocket 可连接但没有 final

检查浏览器是否发送了 `commit`；服务端只在 commit 后返回最终稿。v1 不发送 partial。

## HTTP fallback 失败

确认 FFmpeg 可执行、上传格式受支持，且音频没有超过 `MAX_UPLOAD_BYTES` 和 `STT_MAX_DURATION_SECONDS`。

## `.env` 仍被识别错误

ASR 可能先产生“点 YNV”等结果，最终整理依赖完整转写上下文和润色 Provider。确认 `POLISH_ENABLED=true`、`POLISH_*` 配置完整，并检查响应中的 `polish_status`、`polish_reason` 和 `degraded_stage`。整理失败时服务端会安全返回合并后的 ASR 原文。

## 修改配置没有生效

`.env` 变化通常不会触发 Uvicorn 自动重载。完整重启服务后再测试。
