# 部署

## 推荐拓扑

```text
Browser → HTTPS reverse proxy → lili-voice-input:9100 → model providers
```

反向代理必须支持 WebSocket Upgrade。推荐将服务暴露在宿主网站的同域路径下，减少 CORS、Cookie 和 AudioWorklet 跨域限制。

默认服务只监听 `127.0.0.1`。只有在容器或受保护的内网中才把 `SERVER_HOST` 改成 `0.0.0.0`。

公开部署前必须：

1. 选择许可证并添加 `LICENSE`。
2. 运行密钥扫描。
3. 轮换任何曾进入其他仓库历史的真实密钥。
4. 设置严格的 `ALLOWED_ORIGINS`。
5. 设置并发、时长和上传大小限制。
6. 配置 TLS 和宿主应用认证。

## 文本整理

启用 `POLISH_ENABLED` 后，每次最终转写只进行一次普通文本整理调用。需要同时配置 `POLISH_API_KEY` 和 `POLISH_MODEL`。整理超时、限流、网络或 Provider 异常不会令整个语音请求失败；服务端会返回已经合并的 ASR 原文并标记 `degraded_stage="polish"`。
