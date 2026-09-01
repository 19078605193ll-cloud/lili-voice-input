# 多用户部署与容量验收

## 阶段一：单实例

默认配置是一个 Uvicorn worker、20 个活跃会话、20 个准入等待位置、3 个本地 ASR 请求、3 个本地整理请求和 2 个 FFmpeg 子进程。启动前复制 `.env.example`，填写 Provider 配置，并为公开浏览器调用启用匿名令牌：

```env
ANONYMOUS_TOKENS_ENABLED=true
ANONYMOUS_TOKEN_SECRET=<至少 32 字节的随机值>
ALLOWED_ORIGINS=https://your-site.example
```

`/health/live` 只表示进程存活；负载均衡器必须使用 `/health/ready`。后者还检查 Provider 必填配置、FFmpeg、Redis（启用时）和 draining 状态。

## 阶段二：100 路集群

仓库提供 `docker-compose.production.yml`，包含网关、5 个应用副本、Redis 7 和 Prometheus：

```powershell
Copy-Item .env.example .env
# 配置密钥、严格的 ALLOWED_ORIGINS、ANONYMOUS_TOKENS_ENABLED=true
docker compose -f docker-compose.production.yml up --build -d --scale voice-input=5
```

每个容器只能运行一个 Uvicorn worker。WebSocket 建立后由反向代理保留在原实例；Redis 只负责全局会话租约、匿名限流、准入队列以及 ASR/LLM 执行令牌，不迁移连接。

生产默认容量：每实例 25 个活跃会话，全局 100 个活跃会话、20 个等待位置；每实例 4 个 ASR 和 4 个整理请求，全局各 20 个。租约都带 TTL，实例异常退出后自动回收。Redis 失联时 readiness 返回 503，新会话拒绝；已经准入的 ASR/整理任务继续受本地限制器保护执行。

反向代理地址属于受信任网段时，必须配置 `TRUSTED_PROXY_CIDRS`，否则服务端会忽略 `X-Forwarded-For`，防止伪造来源 IP。示例容器网络的准确 CIDR 应根据实际基础设施填写，不要无条件信任 `0.0.0.0/0`。

## 滚动发布

使用 `SERVICE_TOKEN` 调用实例的排空端点：

```bash
curl -X POST -H "Authorization: Bearer $SERVICE_TOKEN" http://INSTANCE:9100/internal/drain
```

实例会立即变为不就绪并停止准入新会话；30 秒后仍未结束的 WebSocket 收到 `SERVER_RESTART`，随后以 1012 关闭。容器的 `stop_grace_period` 为 35 秒。编排平台应先调用排空端点，再摘除实例并发送终止信号。

## Provider 配额门槛

上线前必须由 OpenRouter/ASR 账户确认至少允许 20 个全局并发请求。DMX 整理配额也应覆盖全局 20 并发。配额不足时下调全局并发和会话容量；不要通过增加实例绕过 Provider 配额。

## 指标和告警

Prometheus 抓取 `/metrics`。指标包含会话/准入队列、ASR/整理/FFmpeg 队列与并发、请求耗时、重试、容量拒绝、降级、HTTP fallback 和 stop-to-final。指标标签不包含 session ID 或匿名 ID；这些信息只进入结构化日志。

`ops/alerts.yml` 提供以下默认告警：final 成功率低于 99%、P95 stop-to-final 超过 10 秒、队列使用率超过 70%、容量拒绝率超过 1%、Provider 429 超过 2%，以及进程内存连续增长。

## 压测与发布验收

完整的环境矩阵、匿名用户构造、执行步骤、用例、门槛和结果模板见 [多用户稳定性与并发容量测试计划](multi-user-test-plan.md)。

先安装 k6，再执行：

```powershell
k6 run -e VUS=10 -e DURATION=1m -e AUDIO_SECONDS=10 -e SERVICE_TOKEN=$env:SERVICE_TOKEN load/k6-websocket.js
k6 run -e VUS=100 -e DURATION=30m -e AUDIO_SECONDS=30 -e SERVICE_TOKEN=$env:SERVICE_TOKEN load/k6-websocket.js
```

测试环境应使用模拟 Provider；真实 Provider 只在 staging 运行并设置预算。发布前还要完成 100 路 24 小时稳定性、150 路过载、10% 超时/429/500、Redis 重启和单实例下线测试。

验收不能只看 HTTP 200，必须同时满足：非空 final ≥99.5%、`degraded=false` ≥98%、30 秒录音 stop-to-final P95≤10 秒/P99≤20 秒、准入 P95≤500ms、正常峰值拒绝率<0.1%、无租约/任务泄漏且内存无持续增长。
