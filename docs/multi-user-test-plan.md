# 多用户稳定性与并发容量测试计划

## 1. 文档信息

| 项目 | 内容 |
| --- | --- |
| 测试对象 | `lili-voice-input` WebSocket 语音输入服务 |
| 当前上线目标 | 2 核 4G 服务器，Docker 单容器、单 Uvicorn worker |
| 当前 Redis 策略 | 单实例关闭 Redis；只有进入多实例发布路线时才启用 |
| 单实例候选额定容量 | 7 个持续活跃会话，仅为首轮候选值，最终由实测决定 |
| 单实例候选硬上限 | 10 个活跃会话、5～10 个准入等待位置 |
| 容量探测范围 | 1、5、10、15、20 VU 逐级测试 |
| 未来集群候选容量 | 5 个实例，全局 100 个活跃会话；不阻塞当前单实例上线 |
| 主要工具 | pytest、k6、Prometheus、Docker Compose |
| 计划状态 | 待执行 |
| 执行人 | 待填写 |
| 执行日期 | 待填写 |
| 被测提交/镜像摘要 | 待填写，测试开始后不得无记录地更换 |

本计划优先证明目标 2 核 4G 服务器上的 Docker 单实例能够在声明容量内持续返回有效结果，并在排队、过载和上游异常时以可预测方式退化和恢复。多实例 Redis、Nginx 和滚动发布属于未来集群发布路线，单独验收，不阻塞当前单实例上线。

## 2. 最终要回答的问题

测试完成后必须能够明确回答以下问题：

1. 一个 Uvicorn 进程能否稳定服务多个并发 WebSocket 用户？
2. 匿名 `client_id` 是否会映射为彼此隔离的匿名主体，并正确执行每用户、每 IP 限制？
3. 当前单实例的额定容量、硬上限和等待队列上限分别是多少？
4. 达到硬上限后，系统是否受控返回 `CAPACITY_REACHED` 或 `QUEUE_TIMEOUT`，而不是崩溃或无界排队？
5. ASR、整理和 FFmpeg 的本地队列是否会公平调度并在测试结束后清空？
6. 真实 ASR/整理 Provider 的配额和延迟能否支撑配置的并发度？
7. 24 小时运行后是否存在连接、任务、文件描述符或内存泄漏？
8. 如果未来进入多实例发布路线，Redis、Nginx 和排空发布是否满足预期？

只有当前发布路线的全部适用项目都有证据，才能给出对应范围的上线结论。当前单实例可以在未执行未来集群用例时上线，但结论只能覆盖单实例；Mock Provider 测试通过也不能替代真实 Provider 测试。

## 3. 测试范围与不在范围内的内容

### 3.1 本计划覆盖

- WebSocket 建连、`start`、`queued`、`ready`、PCM 分块、`commit`、`final` 完整链路。
- 匿名令牌签发、Origin 绑定、匿名主体隔离、签发限流和会话启动限流。
- 单实例准入容量、排队、FIFO 释放、队列超时和容量拒绝。
- ASR、整理、FFmpeg 的并发、队列、超时、重试和降级。
- HTTP fallback 相关指标，但不在明确容量错误后放大过载。
- 单实例基线、正常峰值、硬上限、过载、恢复和 24 小时长稳。
- 真实 Provider 小规模和峰值验证。
- 未来多实例形态下的 Redis 重启、实例排空、实例异常退出和恢复；这些用例单独标记，不阻塞当前单实例发布。

### 3.2 不在本计划的主要范围

- 识别模型的完整语言学质量评估，例如大规模 WER/CER 基准。
- 浏览器麦克风权限的跨浏览器兼容性全矩阵。
- 公网 DDoS、防火墙、WAF 和大规模多地域压测。

真实语音场景仍需至少确认非空结果、关键业务词识别和整理不篡改原意；它不是本计划中的完整模型质量评测。

## 4. 上线容量声明

测试前必须先选择要对外声明的容量，不能在测试完成后根据最好结果倒推口径。

### 4.1 当前 2 核 4G 单实例候选声明

- 目标部署：Docker 单容器、单 Uvicorn worker、`REDIS_ENABLED=false`。
- 首轮候选额定容量：7 个同时录音或等待 final 的会话。
- 首轮候选短时峰值：10 个活跃会话。
- 首轮候选等待队列：5～10 个位置，最长等待 5 秒；正式测试时必须选定一个明确值并冻结。
- 超过候选硬上限时允许进入有界队列。
- 队列满或等待超过 5 秒时允许返回 1013，并携带 `CAPACITY_REACHED` 或 `QUEUE_TIMEOUT`。
- 单实例不提供进程崩溃、主机重启和发布期间的无中断保证。

7/10 是测试起点，不是预先承诺的最终容量。必须完成 1、5、10、15、20 VU 阶梯测试，找到满足全部 SLO 的最大稳定档位，再取该值的约 70%～80%作为额定容量。不能把勉强通过的极限档位直接当作日常容量。

等待队列只是短时突发缓冲，不提供额外吞吐量。额定负载下队列应接近 0；持续排队说明服务能力不足。若 10 个活跃会话平均占用 35 秒，粗略完成速率只有 `10 / 35 ≈ 0.29` 会话/秒，5 秒内通常只释放约 1～2 个位置，因此没有证据时不应配置 20 个等待位置。

### 4.2 未来 100 路集群声明

- 5 个应用实例，每实例 25 个活跃会话。
- Redis 控制全局 100 个活跃会话和全局 ASR/整理并发。
- 全局 ASR、整理并发分别为 20。
- 100 路是额定目标时，应另行提高硬容量或证明在 100 路下仍有足够资源余量。
- 必须完成多实例、Redis、网关、排空发布和单实例异常退出测试后才能使用该声明。
- 本节不属于当前 2 核 4G 单实例上线的阻塞门槛。

## 5. 测试环境矩阵

| 环境 | 用途 | Provider | Redis | 负载规模 | 是否可作为当前单实例上线证据 |
| --- | --- | --- | --- | --- | --- |
| E0 开发机预检 | 功能、匿名身份、调度和脚本调试 | Mock | 关闭 | 1～21+ VU | 否，仅作为预检 |
| E1 目标服务器 Docker | 2C4G 应用容量、队列、过载和长稳 | Mock | 关闭 | 1/5/10/15/20/过载 | 是，证明应用与主机容量 |
| E2 目标服务器 Docker | 真实延迟、配额和结果完整性 | 真实 | 关闭 | 1/3/5/7/10，按预算增加 | 是，证明真实单实例链路 |
| E3 未来生产同构集群 | 100 路、Redis、网关和故障恢复 | Mock 后再真实小规模 | 开启 | 100/150 VU | 否；只用于未来集群发布 |

要求：

- E0 不得使用生产 Provider Key；设置 `MOCK_PROVIDERS_ENABLED=true`。
- E1 必须运行正式构建的 Docker 镜像，不能用宿主机直接启动的 Uvicorn 结果替代。
- E2 必须使用与 E1 相同的 Docker 镜像，并设置费用预算、调用上限和测试时间窗口。
- E3 的实例数、容器资源限制、Nginx、Redis 和网络路径必须与生产一致。
- E1/E2 的负载发生器不应运行在目标 2C4G 服务器上，避免与被测容器争抢 CPU、内存和网络；E0 才允许本机自测。
- 所有环境使用同一被测提交，记录 Git SHA 和镜像摘要。

## 6. 匿名用户与负载模型

### 6.1 身份映射

浏览器 SDK 在 `localStorage` 中保存随机 UUID，并用它作为 `client_id` 申请匿名 JWT。服务端把 `client_id` 做 HMAC 后写入 JWT `sub`。因此：

- 一个稳定 `client_id` 表示一个匿名用户。
- 不同 `client_id` 表示不同匿名用户。
- 同一用户的多个并发会话必须复用同一个 JWT 或同一个 `client_id` 获得的 JWT。
- 不得通过信任任意 `X-Forwarded-For` 伪造用户；只有受控测试代理的准确 CIDR 才能放入 `TRUSTED_PROXY_CIDRS`。

现有 `load/k6-websocket.js` 每个 VU 首次使用不同的 `client_id` 申请令牌，并在该 VU 内缓存令牌。因此匿名用户数等于 VU 数，不等于迭代数。

### 6.2 匿名限流对本机测试的影响

默认配置：

- 同一 IP 每分钟最多签发 10 个匿名令牌。
- 同一匿名主体每分钟最多启动 10 个会话。
- 同一来源 IP 每分钟最多启动 `10 × 5 = 50` 个匿名会话。
- 同一匿名主体最多同时占用 2 个活跃会话。
- JWT 默认 600 秒过期。

因此：

- 10 VU、短于 10 分钟的测试可以直接验证匿名链路。
- 超过 10 个新匿名用户的同机测试会命中 IP 签发限流，这是预期结果。
- 30 分钟和 24 小时容量测试使用测试专用 `SERVICE_TOKEN`，避免把令牌过期或匿名限流混入容量结果。
- 如果要验证大量真实匿名用户，只能在隔离环境临时提高限额，或使用多个真实来源 IP；测试后必须恢复生产值。

### 6.3 音频模型

现有 k6 脚本发送 16kHz PCM16、100ms 一帧的 440Hz 正弦波：

- 适合 Mock Provider 下验证传输、分段、排队和资源占用。
- 不适合真实 Provider 的非空 final 或识别质量验收。

E2 真实 Provider 测试必须使用预先准备并冻结的中文语音 PCM 样本，至少包括：

- 10 秒短句。
- 30 秒普通连续口述。
- 60～90 秒长口述，能够触发多分段。
- 包含静音停顿、数字、英文缩写和业务专有词的样本。
- 至少一条接近最大允许时长的边界样本。

当前仓库没有“从真实 PCM 文件流式发送”的 k6 脚本。完成 E2 前需要补充该能力；在此之前不能用正弦波结果替代真实 Provider 验收。

## 7. 测试数据与证据保存

每轮测试建立独立目录：

```text
test-results/load/<YYYYMMDD-HHmm>-<environment>-<case>/
  metadata.txt
  k6-summary.json
  server-before.prom
  server-after.prom
  server.log
  container-stats.csv
  prometheus-screenshot-or-export/
  result.md
```

`metadata.txt` 至少记录：

- Git SHA、镜像 ID、执行时间和执行人。
- 操作系统、CPU、内存、Docker 版本。
- 实例数、每实例 CPU/内存限制。
- 所有容量和超时配置，但不得记录 Secret 或 Provider Key。
- Provider 名称、模型、账户配额和是否为 Mock。
- k6 VU、持续时间、音频长度、认证方式和来源机器。

任何缺少原始 k6 输出、服务端日志或指标快照的测试都只能视为探索性测试，不能作为正式发布证据。

## 8. 执行前准备

### 8.1 冻结代码并运行静态测试

```powershell
git rev-parse HEAD

Set-Location server
uv sync --frozen
uvx ruff check src tests
uvx ruff format --check src tests
uv run pytest
Set-Location ..

npm ci
npm run typecheck
npm test
npm run build
```

通过条件：全部命令退出码为 0。任何与认证、准入、调度、Redis 租约或 WebSocket 协议相关的失败都阻止继续压测。

### 8.2 确认运行的是当前镜像

当前开发机曾运行不含最新匿名令牌、Redis 和 metrics 配置的旧镜像，因此每次正式测试前必须重建：

```powershell
docker compose up --build -d --force-recreate
docker compose ps
Invoke-RestMethod http://127.0.0.1:9100/health/ready
(Invoke-WebRequest http://127.0.0.1:9100/metrics).StatusCode
```

通过条件：

- 容器健康。
- `/health/ready` 返回 200 和 `status=ok`。
- 单实例直连 `/metrics` 返回 200。
- 运行镜像创建时间和 Git SHA 与本轮记录一致。

生产网关故意对外隐藏 `/metrics` 和 `/internal/*`。E3 应由 Prometheus 在内部网络抓取应用实例，不能把网关 `/metrics` 返回 404 判为失败。

### 8.3 E0 开发机预检

开发机可以使用当前 `docker-compose.yml` 快速验证匿名链路和测试脚本：

```powershell
docker compose up --build -d --force-recreate
docker compose ps
Invoke-RestMethod http://127.0.0.1:9100/health/ready
(Invoke-WebRequest http://127.0.0.1:9100/metrics).StatusCode
```

也可以直接从源码启动额外 Uvicorn 端口调试，但这类结果只能标记为 E0，不能作为 2C4G 上线容量证据。

### 8.4 E1 目标 2C4G Docker 测试实例

正式容量测试必须在目标 2 核 4G 服务器上构建并运行 Docker 镜像。测试前固定以下候选配置：

```env
REDIS_ENABLED=false
METRICS_ENABLED=true
MOCK_PROVIDERS_ENABLED=true
# 先用单用户真实 Provider 测得的 P95 延迟设置，而不是长期使用默认 10ms。
MOCK_PROVIDER_DELAY_MS=<真实Provider单段请求P95毫秒>

ANONYMOUS_TOKENS_ENABLED=true
ANONYMOUS_TOKEN_SECRET=<至少32字节测试密钥>
ANONYMOUS_TOKEN_ISSUE_LIMIT_PER_MINUTE=10
ANONYMOUS_MAX_ACTIVE_SESSIONS=2
ANONYMOUS_SESSION_START_LIMIT_PER_MINUTE=10
SERVICE_TOKEN=<测试专用随机令牌>

STT_MAX_ACTIVE_SESSIONS=10
STT_ADMISSION_QUEUE_SIZE=10
STT_ADMISSION_WAIT_SECONDS=5
STT_MAX_CONCURRENCY=3
STT_ASR_QUEUE_SIZE=12
STT_ASR_QUEUE_TIMEOUT_SECONDS=10
POLISH_LOCAL_CONCURRENCY=3
POLISH_QUEUE_SIZE=12
POLISH_QUEUE_TIMEOUT_SECONDS=3
FFMPEG_MAX_CONCURRENCY=1
FFMPEG_QUEUE_SIZE=4
FFMPEG_QUEUE_TIMEOUT_SECONDS=5
```

其中 `STT_ADMISSION_QUEUE_SIZE=10` 是本计划的首轮固定候选值。如果最终决定使用 5 或其他值，必须在测试元数据中记录，并按实际值重算排队和过载人数。

在目标服务器仓库根目录执行：

```powershell
docker compose up --build -d --force-recreate
docker compose ps
Invoke-RestMethod http://127.0.0.1:9100/health/ready
(Invoke-WebRequest http://127.0.0.1:9100/metrics).StatusCode
docker stats --no-stream
```

如果目标服务器使用 Linux，使用等价命令检查：

```bash
docker compose up --build -d --force-recreate
docker compose ps
curl --fail --silent http://127.0.0.1:9100/health/ready
curl --fail --silent --output /dev/null http://127.0.0.1:9100/metrics
docker stats --no-stream
```

通过条件：

- 只有一个应用容器、一个 Uvicorn worker。
- 容器健康，readiness 和实例直连 metrics 均返回 200。
- `REDIS_ENABLED=false`，测试日志中没有 Redis 连接尝试。
- 容器实际配置与冻结的测试元数据一致。
- 镜像 ID 与待发布镜像一致。

目标服务器的测试入口必须通过受控反向代理、私网地址或只允许负载机访问的防火墙规则暴露。不得为了压测临时向公网开放无认证端口。

当前 Compose 没有给应用容器设置独立 CPU/内存上限，所以资源门槛统一按整台 2C4G 主机归一化：主机 CPU P95 <70%，主机内存使用率 <70%；同时记录应用容器 RSS，要求 P95 <2GiB 且预热后没有持续增长。Docker `CPU %` 常以一个逻辑核心为 100%，在 2 核主机上必须换算为整机比例后再判定，不能直接把 `docker stats` 的 100%当作整机 100%。

### 8.5 独立负载机 k6 命令模板

以下命令在独立负载机的仓库根目录执行。先填写实际受控测试地址：

```powershell
$env:VOICE_LOAD_BASE_HTTP = "https://voice-staging.example"
$env:VOICE_LOAD_BASE_WS = "wss://voice-staging.example"
$env:VOICE_LOAD_ORIGIN = "https://allowed-test-origin.example"
$env:VOICE_LOAD_SERVICE_TOKEN = "<与目标容器一致的测试令牌>"
```

匿名身份短测：

```powershell
docker run --rm `
  -v "${PWD}:/work" -w /work `
  -e BASE_HTTP="$env:VOICE_LOAD_BASE_HTTP" `
  -e BASE_WS="$env:VOICE_LOAD_BASE_WS" `
  -e ORIGIN="$env:VOICE_LOAD_ORIGIN" `
  -e VUS=10 `
  -e DURATION=2m `
  -e AUDIO_SECONDS=10 `
  grafana/k6:0.57.0 run load/k6-websocket.js
```

`SERVICE_TOKEN` 容量测试模板：

```powershell
$env:VOICE_LOAD_VUS = "7"
$env:VOICE_LOAD_DURATION = "30m"
$env:VOICE_LOAD_AUDIO_SECONDS = "30"

docker run --rm `
  -v "${PWD}:/work" -w /work `
  -e BASE_HTTP="$env:VOICE_LOAD_BASE_HTTP" `
  -e BASE_WS="$env:VOICE_LOAD_BASE_WS" `
  -e ORIGIN="$env:VOICE_LOAD_ORIGIN" `
  -e SERVICE_TOKEN="$env:VOICE_LOAD_SERVICE_TOKEN" `
  -e VUS="$env:VOICE_LOAD_VUS" `
  -e DURATION="$env:VOICE_LOAD_DURATION" `
  -e AUDIO_SECONDS="$env:VOICE_LOAD_AUDIO_SECONDS" `
  grafana/k6:0.57.0 run load/k6-websocket.js
```

容量阶梯分别把 `VOICE_LOAD_VUS` 设置为 1、5、10、15、20。正式负载机不得与目标 2C4G 应用容器共享 CPU 和内存。

## 9. 监控指标与计算口径

### 9.1 k6 客户端指标

- WebSocket 101 升级成功率。
- 非空 `final` 比例。
- `voice_stop_to_final_ms` P50、P95、P99、max。
- `voice_admission_wait_ms` P50、P95、P99、max。
- k6 VU 数、迭代数和失败检查数。

当前 k6 的 `checks` 同时混合了令牌签发、WebSocket 升级和非空 final。它不能精确代替“非空 final ≥99.5%”这一独立 SLO。正式发布前应把这些结果拆成独立 Rate/Counter；在完成前必须用服务端计数器和日志交叉计算。

### 9.2 服务端关键指标

| 目的 | 指标 |
| --- | --- |
| 活跃与排队 | `voice_active_sessions`、`voice_admission_queue_depth`、`voice_redis_admission_queue_depth` |
| 会话结果 | `voice_sessions_total`、`voice_ws_disconnects_total` |
| 容量拒绝 | `voice_capacity_rejections_total` |
| ASR | `voice_asr_inflight`、`voice_asr_queue_depth`、`voice_asr_queue_wait_seconds`、`voice_asr_requests_total`、`voice_asr_latency_seconds`、`voice_asr_retries_total` |
| 整理 | `voice_polish_inflight`、`voice_polish_queue_depth`、`voice_polish_queue_wait_seconds`、`voice_polish_requests_total`、`voice_polish_latency_seconds` |
| FFmpeg | `voice_ffmpeg_inflight`、`voice_ffmpeg_queue_depth`、`voice_ffmpeg_queue_wait_seconds`、`voice_ffmpeg_requests_total` |
| 结果质量 | `voice_final_latency_seconds`、`voice_degraded_results_total`、`voice_http_fallback_total` |
| Redis/租约 | `voice_redis_ready`、`voice_lease_release_failures_total` |
| 进程资源 | `process_resident_memory_bytes`、`process_cpu_seconds_total`、`process_open_fds`（平台支持时） |

### 9.3 统一计算口径

- 非空 final 率 = 非空 final 数 / 已准入且非用户主动取消的会话数。
- final 成功率 = `outcome="success"` / 所有已准入且非取消会话。
- 非降级率 = `(成功 final - degraded final)` / 成功 final。
- 正常峰值容量拒绝率 = 容量拒绝数 / `(非取消会话数 + 容量拒绝数)`。
- Provider 429 率 = ASR 和整理 `status="rate_limited"` / ASR 和整理请求总数。
- 恢复时间 = 停止过载或故障到 readiness 正常、队列回到基线且新请求重新满足 SLO 的时间。

告警阈值是生产告警线，不等于发布门槛。例如容量拒绝率超过 1%才告警，但正常峰值发布门槛是小于 0.1%。

## 10. 详细测试用例

### T01 单元、协议和构建回归

目的：证明认证、准入队列、调度、Redis 租约和客户端协议的确定性逻辑没有回归。

步骤：执行第 8.1 节全部命令。

通过条件：

- 全部测试通过。
- FIFO、取消清理、队列超时、主体上限、Redis 原子租约测试均执行。
- 前端类型检查、单元测试和构建通过。

### T02 匿名令牌与身份隔离

环境：先在 E0 调通，再在 E1 正式执行；Mock，匿名认证，10 VU，2 分钟，10 秒音频。

步骤：

1. 确认匿名令牌签发计数基线。
2. 不传 `SERVICE_TOKEN` 运行匿名 k6 命令。
3. 检查 10 个首次令牌请求均返回 200。
4. 从结构化日志确认存在 10 个不同的匿名 subject，日志中不得出现原始 `client_id` 或 JWT。
5. 检查每个会话都经过 `queued` 或 `ready`，最终返回非空 final。

通过条件：

- 匿名令牌签发成功率 100%。
- 不出现意外 401、403、429。
- 10 个 VU 对应 10 个不同匿名主体。
- 非空 final ≥99.5%，本短测建议要求 100%。

### T03 Origin 绑定和非法令牌

环境：先在 E0 调通，再在 E1 正式执行。

步骤：

1. 用允许的 Origin 申请匿名 JWT。
2. 使用相同 Origin 建立连接，应成功。
3. 用另一个 Origin 使用该 JWT，应收到 `UNAUTHORIZED` 或 Origin 拒绝。
4. 分别测试空令牌、被修改的 JWT、过期 JWT。

通过条件：所有非法情况均被拒绝，且不占用活跃会话和队列位置。

### T04 匿名签发与会话启动限流

环境：先在 E0 调通，再在 E1 正式执行，保持候选生产限额。

步骤：

1. 同一 IP 在一分钟内申请 11 个不同 `client_id` 的令牌。
2. 验证前 10 个允许，第 11 个返回 429、`RATE_LIMITED`、`Retry-After`。
3. 同一匿名主体在一分钟内快速启动超过 10 个会话。
4. 验证超额请求返回 429，其他匿名主体不受该主体限额影响，IP 总限额仍生效。

通过条件：限流准确、不会误伤已经准入的会话、计数窗口结束后自动恢复。

现有 k6 脚本不适合精确断言“第 11 个请求”的响应，本用例需要一个单次令牌/会话请求脚本或自动化 API 测试。

### T05 同一匿名用户活跃会话上限

环境：先在 E0 调通，再在 E1 正式执行，`ANONYMOUS_MAX_ACTIVE_SESSIONS=2`。

步骤：

1. 申请一个匿名 JWT。
2. 使用同一个 JWT 同时保持两个正在录音的 WebSocket。
3. 再用同一个 JWT 打开第三条连接。
4. 验证第三条进入等待队列，并在前两条没有释放时最终 `QUEUE_TIMEOUT`；不能越过主体上限。
5. 释放第一条，重新测试第三条能够获得名额。

通过条件：同一匿名主体同时最多 2 条活跃会话；不同主体仍可使用剩余全局容量。

### T06 单用户基线

环境：E1，1 VU，10 分钟，30 秒音频，`SERVICE_TOKEN`。

目的：建立无并发时的 ASR、整理、final 延迟和资源基线。

通过条件：

- 非空 final 100%。
- 无降级、容量拒绝和意外断开。
- 记录延迟 P50/P95/P99、CPU、RSS 作为后续对比基线。

### T07 单实例正常额定负载

环境：E1，7 VU，30 分钟，30 秒音频，`SERVICE_TOKEN`。

说明：7 VU 是首轮候选额定值。只有完成 T08 容量探测后，才能把它确认为最终额定容量或调整为其他值。

通过条件：

- 非空 final ≥99.5%。
- final 成功率 ≥99.5%。
- 非降级率 ≥98%。
- stop-to-final P95 ≤10 秒、P99 ≤20 秒。
- 准入等待 P95 ≤500ms。
- 容量拒绝率 <0.1%。
- Provider 429 为 0（Mock 环境）。
- 2C4G 主机 CPU P95 <70%、主机内存使用率 <70%；应用容器 RSS P95 <2GiB 且无持续增长。
- 队列不能连续 5 分钟增长。

### T08 单实例容量阶梯与稳定上限探测

环境：E1，依次测试 1、5、10、15、20 VU；1 VU 运行 10 分钟，其余每档运行 30 分钟，30 秒音频，`SERVICE_TOKEN`。

步骤：

1. 先完成 1 VU 基线和 5 VU 低负载。
2. 以 `STT_MAX_ACTIVE_SESSIONS=10` 执行 10 VU。
3. 10 VU 通过后，把 `STT_MAX_ACTIVE_SESSIONS` 改为 15，重启同一镜像并确认配置，再执行 15 VU。
4. 15 VU 通过后，以相同方式测试 20 VU。
5. 每档之间停止负载，等待活跃会话和全部队列归零，并保存独立证据目录。
6. 任一档触发停止条件后，不再继续升档。

每个档位都必须满足 T07 的成功率、延迟和正常负载拒绝率门槛，同时满足 2C4G 主机资源门槛。主机 CPU 或内存达到 80%，应用容器 RSS 达到 2.5GiB，或者队列连续增长，即使请求最终成功也不得把该档位认定为稳定上限。

结果计算：

- `N_stable` = 满足全部门槛的最大档位。
- 建议额定容量 `R` = `N_stable` 的约 70%～80%，并取便于配置的保守整数。
- 候选硬上限 `C` 不得高于 `N_stable`。
- 完成后冻结最终 `R`、`C` 和等待队列 `Q`，后续 T09～T16 全部使用这组值。

如果 20 VU 仍有充足余量，只能记录为“稳定上限尚未找到”，需要继续按小步递增补测，不能直接声称 20 是极限。

### T09 准入排队和释放

环境：E1，使用 T08 冻结的 `C` 和 `Q`，以 `C + Q` VU 运行 5 分钟，2 秒音频，`SERVICE_TOKEN`。

预期：最先到达的 `C` 条会话立即准入，其余 `Q` 条进入队列；随着短会话结束，等待会话按资格和顺序获得名额。若采用首轮候选 `C=10、Q=10`，本用例使用 20 VU。

通过条件：

- 出现 `queued` 事件，位置和最大等待时间合理。
- 所有等待者在 5 秒内被释放并获得 final。
- 没有超越主体上限或全局容量。
- 测试结束后活跃数、准入队列、ASR 队列和整理队列全部回到 0。

该用例会有意产生超过 500ms 的准入等待，因此通用 k6 延迟 threshold 可能失败。结果按本用例预期判断。

如果不能在 5 秒内释放全部 `Q` 个等待者，应优先减小 `Q` 并重测；只有产品明确接受更长等待体验时才评估增加 `STT_ADMISSION_WAIT_SECONDS`。不得为了让大队列通过而无限延长等待时间。

### T10 队列满和过载保护

环境：E1，使用 T08 冻结的 `C` 和 `Q`。第一档为 `C + Q + 1` VU，随后增加到约 `2 × C`、`3 × C`，每档 10 分钟，30 秒音频，`SERVICE_TOKEN`。

预期：最多 `C` 个活跃会话和 `Q` 个等待者；第 `C + Q + 1` 条及后续请求受控拒绝，部分等待者在 5 秒后收到 `QUEUE_TIMEOUT`。若采用首轮候选 `C=10、Q=10`，第一个明确超额档位是 21 VU。

通过条件：

- 活跃会话不超过 `C`，准入队列不超过 `Q`。
- 超额请求返回 1013 和稳定的 `CAPACITY_REACHED`/`QUEUE_TIMEOUT`，包含合理 `retry_after_ms`。
- 不出现进程退出、OOM、无界内存增长或大量未知 5xx。
- 已经准入的会话仍满足非空 final ≥99%。
- `/health/live` 始终可用；除明确故障注入外 `/health/ready` 保持正常。

通用 k6 `checks>99.5%` 在本用例中预期失败，因为容量拒绝是测试目标，不能按普通成功率门槛判定。

### T11 过载撤除后的恢复

环境：紧接 T10。

步骤：

1. 停止过载流量。
2. 每 5 秒发起一个 1 VU 探针会话。
3. 观察 readiness、活跃会话、全部队列和延迟。

通过条件：

- 60 秒内活跃数和队列回到合理基线。
- 新会话不再得到容量错误。
- 2 分钟内恢复到 T06 延迟基线的 20%范围内。
- 没有残留任务或持续增长的 RSS。

### T12 WebSocket 背压与客户端断开

环境：E1。

步骤：

1. 模拟慢网络或暂停服务端读取，使客户端 `bufferedAmount` 达到告警和停止阈值。
2. 验证客户端在持续背压时停止流式发送，只尝试一次 HTTP fallback。
3. 分别在录音中、排队中和 finalizing 中断开客户端。

通过条件：

- 产生稳定 `BACKPRESSURE` 或客户端断开结果。
- 明确 `CAPACITY_REACHED`、`QUEUE_TIMEOUT` 后不得触发 HTTP fallback。
- 断开后会话、排队位置、ASR 任务及时释放。

该用例需要浏览器端网络节流或专用代理，现有 k6 脚本不直接模拟 WebSocket 下行停滞。

### T13 24 小时单实例长稳

环境：E1，使用 T08 确定的最终额定容量 `R` VU，24 小时，30 秒音频，`SERVICE_TOKEN`。

执行前先完成至少一次 30 分钟预热，避免把初始化缓存误判为泄漏。

通过条件：

- 满足 T07 的成功率和延迟门槛。
- 预热后 RSS 不得连续单向增长；用线性趋势和首尾稳定窗口比较，不能只比较单个瞬时值。
- 进程、线程、文件描述符、连接数和 asyncio 任务数无持续增长。
- 无租约释放失败、未捕获异常和进程重启。
- 测试结束 60 秒后所有活跃和队列 Gauge 为 0。

### T14 真实 Provider 基线和峰值

环境：E2，真实语音 PCM 样本，Docker 镜像和容量配置必须与 E1 一致。

切换 E2 时必须设置 `MOCK_PROVIDERS_ENABLED=false`、`REDIS_ENABLED=false`，加载真实 Provider 配置并重新创建容器；不得只修改宿主机 `.env` 而不重启容器。

负载阶梯：1 VU 10 分钟 → 3 VU 15 分钟 → 5 VU 15 分钟 → 最终额定容量 `R` 运行 30 分钟 → 候选硬上限 `C` 运行 15 分钟。每档之间观察 5 分钟并确认队列清空。

通过条件：

- `R` 档满足 T07 全部门槛。
- `C` 档不出现配额耗尽，且满足 T08 对硬上限的约束。
- Provider 429 在额定负载下应为 0；任何 429 都要确认账户配额和重试行为。
- 非空 final ≥99.5%，非降级率 ≥98%。
- 多分段语音的文本顺序正确，无明显重复或丢段。
- 整理结果不为空、不截断、不改变原意。

如果 Provider 账户未确认至少覆盖配置的并发请求数，则本用例不能判为通过。

### T15 Provider 慢响应、429、超时和 500

环境：E1 优先使用可控测试 Provider；不得对真实生产 Provider 主动制造大量失败。

场景：

- 固定增加 Provider 延迟，覆盖接近请求超时的情况。
- 10% ASR 请求返回 429。
- 10% ASR 请求超时。
- 10% ASR 请求返回 500。
- 10% 整理请求分别返回 429、超时和 500。

通过条件：

- ASR 可重试错误遵循最大重试数，不产生重试风暴。
- 部分 ASR 分片失败时尽可能返回成功分片并标记 `degraded_stage="asr"`。
- 全部分片失败时返回稳定的 ASR error。
- 整理失败回退到原始 ASR 文本，`polish_status="fallback"` 且原因正确。
- 故障撤除后 2 分钟内恢复基线，不遗留占用令牌或任务。

当前 `MockAsrProvider` 和 `MockTextPolisher` 只支持固定延迟，没有按比例注入 429/超时/500 的配置。本用例在增加可控故障测试 Provider或代理前属于发布阻塞项，不能声称已完成系统级 10% 故障验收。

### T16 HTTP fallback 放大保护

环境：E1。

步骤：

1. 制造普通 WebSocket 背压，确认最多一次 HTTP fallback。
2. 制造 `CAPACITY_REACHED` 和 `QUEUE_TIMEOUT`。
3. 观察 `voice_http_fallback_total` 和总请求量。

通过条件：容量错误后 HTTP fallback 增量为 0；普通背压每个客户端最多增加 1 次，不产生请求风暴。

### T17 多实例 100 路正常负载（未来集群发布）

环境：E3，使用 `docker-compose.production.yml`，5 个实例、Redis、Nginx、Prometheus。

本用例及 T18～T21 不阻塞当前 2C4G 单实例上线；只有准备发布多实例集群时才转为阻塞项。

负载：100 VU，30 分钟预检；通过后执行 100 VU、24 小时长稳。先使用 Mock，再用真实 Provider 做受预算限制的较短验证。

通过条件：

- 100 路下满足正式发布门槛。
- 全局活跃会话不超过 100；每实例不超过本地上限。
- 全局 ASR/整理并发不超过各自 20。
- 负载合理分布到多个实例，无单实例持续热点。
- Redis 租约释放失败为 0。
- 任一连接建立后保持在原实例直到结束。

### T18 集群 150 路过载（未来集群发布）

环境：E3，150 VU，30 分钟音频，持续 30 分钟。

通过条件：

- 全局活跃数不超过 100，队列不超过配置值。
- 超额会话受控返回容量错误。
- 已准入会话的成功率 ≥99%，集群不崩溃。
- Redis、Nginx、任一实例 CPU/内存不饱和失控。
- 撤除过载后 2 分钟内恢复正常峰值 SLO。

### T19 Redis 重启与失联（未来集群发布）

环境：E3，在稳定负载期间执行一次受控 Redis 重启，并另做一次临时网络隔离。

通过条件：

- Redis 不可用时 `/health/ready` 返回 503。
- 新会话失败关闭，不被错误准入。
- 已准入的 ASR/整理任务继续受本地并发限制保护。
- Redis 恢复后 readiness 自动恢复。
- 过期租约最终回收，无超卖、永久占用或重复释放异常。

### T20 实例排空和滚动发布（未来集群发布）

环境：E3，持续 70% 额定负载。

步骤：

1. 使用 `SERVICE_TOKEN` 调用目标实例 `/internal/drain`。
2. 确认实例立即变为 not-ready，不再获得新会话。
3. 观察存量 WebSocket 正常结束。
4. 30 秒后仍未结束的连接应收到 `SERVER_RESTART` 并以 1012 关闭。
5. 完成实例替换，确认容量恢复。

通过条件：

- 排空实例没有新准入。
- 其他实例继续服务。
- 客户端按策略重连，无大规模同时重试。
- 发布期间总体 final 成功率和容量拒绝率不越过约定发布窗口门槛。

### T21 实例异常退出（未来集群发布）

环境：E3，持续 70% 额定负载，直接终止一个应用容器，不执行 drain。

通过条件：

- 只有该实例上的连接受影响。
- 客户端收到断开并按带抖动退避重试。
- Redis 租约在 TTL 后回收。
- 剩余实例不会突破本地或全局 Provider 并发。
- 集群恢复到完整副本数后，指标和容量恢复基线。

## 11. 推荐执行顺序与停止条件

当前 2C4G 单实例发布执行顺序：

1. T01～T05：功能、认证和限制正确性。
2. 先执行 T14 的 1 VU 真实 Provider 基线，取得 Provider 单段请求 P95，作为 E1 Mock 延迟依据。
3. T06～T09：单实例基线、候选额定值、容量阶梯和排队。
4. T10～T12：过载、恢复、背压和断开。
5. T13：单实例 24 小时长稳。
6. 完成 T14 其余真实 Provider 阶梯，再执行 T15～T16：故障和 fallback。

未来多实例发布时再执行 T17～T21：生产同构集群容量、Redis、排空和故障恢复。这组用例的未执行状态不阻塞当前单实例签字。

出现以下任一情况立即停止当前高负载测试：

- 进程或容器非预期重启。
- CPU 连续 5 分钟 >90%。
- 2C4G 主机内存使用率 >85%、应用容器 RSS >2.8GiB，或任一内存曲线持续快速增长。
- 队列达到上限且已准入会话失败率持续上升。
- Provider 产生非预期费用或连续 429。
- 日志出现 Secret、完整 JWT、Provider Key 或原始敏感音频内容。
- 影响到非测试用户或生产流量。

停止后必须保留现场指标和日志，不能直接重启后重新测试并覆盖失败证据。

## 12. 发布验收门槛

### 12.1 正常峰值门槛

| 指标 | 门槛 |
| --- | --- |
| 非空 final | ≥99.5% |
| final 成功率 | ≥99.5% |
| 非降级 final | ≥98% |
| 30 秒录音 stop-to-final P95 | ≤10 秒 |
| 30 秒录音 stop-to-final P99 | ≤20 秒 |
| 准入等待 P95 | ≤500ms |
| 容量拒绝率 | <0.1% |
| 正常峰值 Provider 429 | 目标为 0，必须低于 2% 告警线 |
| 2C4G 主机 CPU P95 | <70% |
| 2C4G 主机内存使用率 | <70% |
| 应用容器 RSS | P95 <2GiB，且无持续增长 |
| Redis 租约释放失败 | 单实例不适用；未来集群必须为 0 |
| 测试后残留活跃/队列 | 60 秒内全部为 0 |

### 12.2 过载门槛

- 容量上限不被突破。
- 只出现预期的受控拒绝和队列超时。
- 已准入会话成功率 ≥99%。
- 不崩溃、不 OOM、不产生无界队列和重试风暴。
- 撤除负载后 2 分钟内恢复正常 SLO。

### 12.3 长稳门槛

- 24 小时内满足正常峰值门槛。
- 无非预期进程重启。
- 预热后资源无持续上升趋势。
- 当前单实例无任务、连接和文件描述符泄漏；未来集群还必须无 Redis 租约泄漏。

## 13. 当前自动化缺口

在给出完整上线结论前，建议补齐以下自动化能力：

1. k6 使用真实 PCM 文件并按实时节奏流式发送。
2. k6 将令牌签发、WebSocket 升级、非空 final、degraded 和错误码拆成独立指标。
3. k6 支持一次性到达、阶梯负载、固定到达率，而不只是 constant VU 循环。
4. k6 支持同一匿名 JWT 并发打开 3 条连接，端到端验证主体上限。
5. Mock Provider 支持确定性的延迟分布和按比例注入 429、超时、500。
6. 自动保存 k6 JSON、Prometheus 快照、容器资源和服务端日志。
7. nightly workflow 增加 24 小时任务或定期较短 soak，并对资源趋势做断言。

其中第 1、2、5 项是完整真实 Provider 和故障验收的关键缺口。缺口未补齐时，应在发布报告中明确写为“未验证”，不能写成“通过”。

## 14. 单轮测试结果模板

```markdown
# 测试结果：<用例编号和名称>

- 结果：PASS / FAIL / BLOCKED
- 环境：E0 / E1 / E2 / E3
- Git SHA：
- 镜像 ID：
- 开始/结束时间：
- VU / 持续时间 / 音频长度：
- 认证方式：anonymous / service token
- Provider：mock / 名称与模型

## 核心结果

| 指标 | 实测 | 门槛 | 结论 |
| --- | ---: | ---: | --- |
| 非空 final | | ≥99.5% | |
| final 成功率 | | ≥99.5% | |
| 非降级率 | | ≥98% | |
| stop-to-final P95 | | ≤10s | |
| stop-to-final P99 | | ≤20s | |
| 准入等待 P95 | | ≤500ms | |
| 容量拒绝率 | | <0.1% | |
| 2C4G 主机 CPU P95 | | <70% | |
| 2C4G 主机内存使用率 | | <70% | |
| 应用容器 RSS 峰值/趋势 | | <2GiB且无增长 | |

## 错误分布

| 错误码/结果 | 数量 | 是否预期 |
| --- | ---: | --- |
| CAPACITY_REACHED | | |
| QUEUE_TIMEOUT | | |
| RATE_LIMITED | | |
| ASR_TIMEOUT | | |
| SERVER_RESTART | | |
| degraded=asr | | |
| degraded=polish | | |

## 证据路径

- k6：
- Prometheus：
- 服务端日志：
- 容器资源：

## 异常、解释和后续动作

- （填写）
```

## 15. 最终上线签字表

### 15.1 当前 2 核 4G Docker 单实例发布

| 验收项 | 结果 | 证据 | 负责人 |
| --- | --- | --- | --- |
| 单元与协议回归 | 待执行 | | |
| 匿名身份与限流 | 待执行 | | |
| 目标服务器 Docker 镜像与配置确认 | 待执行 | | |
| 1/5/10/15/20 VU 容量阶梯 | 待执行 | | |
| 最终额定容量 `R` | 待确定 | | |
| 最终硬上限 `C` | 待确定 | | |
| 最终等待队列 `Q` | 待确定 | | |
| 单实例排队与过载 | 待执行 | | |
| 单实例 24 小时长稳 | 待执行 | | |
| 真实 Provider 配额与延迟 | 待执行 | | |
| 10% Provider 故障 | 阻塞：缺少故障注入能力 | | |
| 告警验证 | 待执行 | | |
| 回滚方案验证 | 待执行 | | |

当前发布明确不承诺：容器或主机故障期间无中断、多实例全局容量、Redis 协调和滚动发布无中断。

### 15.2 未来多实例 Redis 集群发布

| 验收项 | 当前状态 | 证据 | 负责人 |
| --- | --- | --- | --- |
| 100 路集群 24 小时 | 非当前发布范围 | | |
| 150 路集群过载 | 非当前发布范围 | | |
| Redis 重启/失联 | 非当前发布范围 | | |
| Redis 全局会话和 Provider 租约 | 非当前发布范围 | | |
| 排空和滚动发布 | 非当前发布范围 | | |
| 单实例异常退出 | 非当前发布范围 | | |
| 多实例告警与回滚 | 非当前发布范围 | | |

这些项目在未来切换到多实例部署前必须全部改为“待执行”，并完成独立签字；当前保持“非当前发布范围”不影响单实例结论。

最终结论只能使用以下格式之一：

- `允许上线：2 核 4G Docker 单实例，额定 R 路、硬上限 C 路、等待队列 Q；Redis 关闭，不提供容器或主机故障期间无中断保证。`
- `允许上线：5 实例集群额定 <实测值> 路，已完成 Redis、网关和故障恢复验收。`
- `有条件上线：<明确限制、风险、到期时间和负责人>。`
- `不允许上线：<未通过门槛或未完成的阻塞项>。`

## 16. 2026-08-27 本地 Mock 拐点探测记录

本节是本地预备结论，不是生产上线签字。目标容器限制为 2 CPU、4GiB，单 Uvicorn worker、Redis 关闭、Mock Provider 延迟 1000ms；负载发生器与目标容器共享 Windows 主机。

- 已有 7 VU 和 10 VU、每人连续 180 秒的结果均通过。
- 15 VU 连续 180 秒测试执行两次，均为 15/15 获得 `ready` 和非空 final，服务端错误、容量拒绝和排队超时均为 0。
- 有效复测的 stop-to-final P95 为 5904.1ms、最大值为 5909ms，2 核配额归一 CPU P95 为 7.535%，容器内存峰值为 139.5MiB，测试结束 6.8 秒后连接和队列清空。
- 15 VU 档的 ASR 队列在约 92、137、182 秒三次达到配置上限 12，因此按照“内部队列不得触顶”的条件判定失败；未继续测试 20～60 VU。
- 当前 Mock 配置得到 `N_stable=10`、`N_fail=15`，拐点区间为 10～15 VU；候选日常容量保持 `R=7～8`，候选硬上限保持 `C≤10`。

证据保存在 `test-results/load/20260827-145509-breakpoint-confirm/`。自动化入口为 `load/run-breakpoint.ps1`，测试覆盖配置为 `load/docker-compose.breakpoint.yml`。真实 Provider 和实际 2 核 4GB 云主机验证完成前，不修改生产容量配置。

后续复跑命令：

```powershell
.\load\run-breakpoint.ps1
```

脚本默认执行 15、20、30、40、50、60 VU；首次失败后停止升档，区间大于 5 VU 时自动补测一个 5 的倍数档位，并在 `finally` 中恢复正常 Compose 配置。

## 17. 真实 Qwen ASR 并发上限省钱测试计划

### 17.1 目标与边界

本轮只验证 `qwen/qwen3-asr-0.6b` 经 OpenRouter/DeepInfra 的真实并发、429 和延迟，不同时测试整理 Provider，也不重新寻找 CPU/内存上限。

需要回答：

1. 当前 API Key 在 3、5、8、12、16 个同时 ASR 请求下是否出现 Provider 429、超时或明显延迟恶化？
2. 本地 `STT_MAX_CONCURRENCY` 可以安全提高到多少？
3. 调高并发后，一轮并发用户连续输入 3 分钟是否仍能全部获得 final？

OpenRouter 当前未公开该模型的固定 RPM 或最大并发；模型和端点 API 的 `per_request_limits` 均未给出数字。因此本轮得到的是当前账号、当前 DeepInfra 端点和当前时段的经验上限，不是永久官方额度。执行前参考 [OpenRouter Limits](https://openrouter.ai/docs/api_reference/limits) 核对最新限流规则。

### 17.2 测试输入准备

真实 Provider 测试不得继续使用 `load/k6-websocket.js` 当前生成的 440Hz 正弦波。

- 准备一个已获授权、语音清楚的 30 秒普通话样本。
- 转换为 16kHz、单声道、PCM16 little-endian，保存到被 Git 忽略的 `test-data/voice/zh-30s.pcm`。
- k6 增加 `AUDIO_FILE` 参数，在初始化阶段读取 PCM，并按 100ms 一帧发送；未提供文件时仍保留原 Mock 正弦波行为。
- 3 分钟档循环该样本 6 次。1 VU 预检必须返回非空且基本可辨认的文本，否则不得开始并发测试。

### 17.3 固定环境

- 单容器、单 Uvicorn worker、Redis 关闭，容器限制 2 CPU、4GiB。
- `MOCK_PROVIDERS_ENABLED=false`，使用 `.env` 中的 `ASR_BASE_URL`、`ASR_MODEL` 和 `ASR_API_KEY`，证据中不得记录密钥。
- `POLISH_ENABLED=false`，隔离 ASR 延迟和费用。
- 使用临时 `SERVICE_TOKEN`，匿名限流关闭。
- `STT_MAX_ACTIVE_SESSIONS=20`、`STT_ADMISSION_QUEUE_SIZE=0`，避免准入队列干扰。
- 短阶梯设置 `STT_SEGMENT_MAX_RETRIES=0`，直接暴露 429；最终持续档恢复生产候选值 2。
- 每档按实际并发设置 `STT_MAX_CONCURRENCY=K`、`STT_ASR_QUEUE_SIZE=4×K`，避免本地队列 12 先成为瓶颈。
- 每 1 秒采集 ASR inflight、队列、等待时间、请求延迟、结果分类、CPU 和内存；每档结束最多等待 60 秒归零。

### 17.4 执行阶梯

| 阶段 | VU | 音频 | ASR 并发 `K` | 本地 ASR 队列 | 目的 |
| --- | ---: | ---: | ---: | ---: | --- |
| P0 | 1 | 10s | 1 | 4 | 验证密钥、模型、音频和非空转写 |
| B1 | 3 | 30s | 3 | 12 | 当前并发基线 |
| B2 | 5 | 30s | 5 | 20 | 小步提高 Provider 并发 |
| B3 | 8 | 30s | 8 | 32 | 中等并发 |
| B4 | 12 | 30s | 12 | 48 | 高并发候选 |
| B5 | 16 | 30s | 16 | 64 | 本轮最高探测档 |

执行规则：

1. P0 通过后依次执行 B1～B5，任一档失败立即停止升档。
2. 如果唯一失败原因是 Provider 429、5xx 或网络超时，等待 2 分钟后只复测该失败档一次；第二次仍失败才冻结 `N_fail`。
3. 如果容器重启、OOM、健康检查失败，或 2 核 CPU 连续 15 秒超过 90%，立即停止且不复测。
4. 如果 B5 仍通过，只记录“当前 Provider 并发上限高于 16”，本轮不继续增加人数。
5. 短阶梯完成后，从最高通过档按约 75% 取保守并发：16→12、12→8、8→5、5→3、3→3，得到 `K_safe`。
6. 先执行一次 `K_safe VU × 180s`，设置 `STT_MAX_CONCURRENCY=K_safe`、`STT_ASR_QUEUE_SIZE=2×K_safe`、重试次数 2，验证持续负载；如果全部 final 成功但出现被重试掩盖的 Provider 错误，则在预算内按 `12→8→5→3` 单变量降档，直到首次严格通过。

### 17.5 每档通过与停止条件

短阶梯必须同时满足：

- 所有用户获得 `ready` 和非空 final，k6 检查失败为 0。
- Provider 429、Provider 5xx、ASR 超时、本地 ASR `queue_timeout` 均为 0。
- stop-to-final P95 `<10s`、最大值 `<20s`。
- ASR inflight 峰值达到目标 `K`；未达到则该档只能标记为“并发未被实际打满”，不得据此声称 Provider 支持该并发。
- ASR 队列不触及该档配置上限，测试结束 60 秒内全部连接、inflight 和队列归零。
- 容器健康、重启 0、OOM 0，容器内存 `<2GiB`。

最终 3 分钟档除上述条件外，还要求 ASR 队列等待 P95 `<2s`、队列无持续增长。任何一次 429 都必须记录响应中的 `provider_code`、`Retry-After` 和 `X-RateLimit-*`（如果存在），但不得记录 Authorization 请求头。

### 17.6 费用上限

截至 2026-08-27，[OpenRouter 模型页](https://openrouter.ai/qwen/qwen3-asr-0.6b)价格为 `$0.000003/音频秒`。按全部短阶梯计算约为：

```text
(3 + 5 + 8 + 12 + 16) × 30 秒 × $0.000003 ≈ $0.00396
```

如果 B5 通过，最终 12 VU × 180 秒约 `$0.00648`。考虑分段重叠和一次失败复测，本轮 ASR 预算上限固定为 `$0.02`；执行前必须重新读取官方价格，预计费用超过上限则停止。整理关闭，因此不产生整理费用。

### 17.7 结果计算与产物

- `K_burst_pass`：没有 429、超时或延迟越线的最高短阶梯并发。
- `K_burst_fail`：连续两次失败的第一个短阶梯并发；没有失败则记为 `>16`。
- `K_safe`：按 17.4 的 75% 映射得到的本地 Provider 并发候选值。
- `N_sustained`：通过 3 分钟持续确认的并发用户数。
- 本地 ASR 队列候选值为 `2×K_safe`；如果持续档队列等待 P95 超过 2 秒，应降低用户硬上限或提高 Provider 并发，不得只继续扩大等待队列。

每档保存独立的 k6 JSON、控制台日志、1 秒监控 CSV、脱敏 Provider 错误摘要和容器日志。最终报告必须明确区分“OpenRouter 平台限流”“DeepInfra Provider 限流”“本地队列超时”和“应用资源故障”，并在 `finally` 中恢复原始 `.env`/Compose 配置。真实测试通过前，不修改生产 `STT_MAX_CONCURRENCY`、`STT_ASR_QUEUE_SIZE`、`R` 或 `C`。

### 17.8 2026-08-27 P0 真实 ASR 预检记录

- 输入文件：39.488 秒单声道 AAC/M4A，取前 30 秒转换为 16kHz、单声道 PCM16；产物 `test-data/voice/zh-30s.pcm` 已被 Git 忽略，原始录音未修改。
- 执行参数：1 VU、发送前 10 秒音频、真实 `qwen/qwen3-asr-0.6b`、ASR 并发 1、队列 4、重试 0、整理关闭。
- 结果：1/1 获得 `ready` 和非空 final，final 长度 65 个字符，停止到 final 为 1990ms，准入等待 0ms，服务端错误 0，k6 检查 2/2 通过。
- 证据：`test-results/load/20260827-153225-real-asr-preflight/`。
- 恢复：正常 Compose 配置已恢复，服务健康、重启 0、OOM 0，临时令牌和整理关闭覆盖均已移除。

P0 结论为“压测音频、k6 发送链路、真实 ASR Key/模型和自动判定均可用”，可以进入 B1（3 VU × 30 秒）。B1 及后续结果见 17.9。

### 17.9 2026-08-27 真实 ASR 阶梯与持续测试记录

短阶梯关闭整理和重试，只改变真实 ASR 并发 `K`。五档均实际观察到目标 inflight 峰值，且本地 ASR 队列峰值均为 0：

| 阶段 | K/VU | 结果 | final | 降级 | stop-to-final P95 | ASR 排队 P95 | Provider 异常 |
| --- | ---: | --- | ---: | ---: | ---: | ---: | --- |
| B1 | 3 | PASS | 3/3 | 0 | 3662.4ms | 163.9ms | 0 |
| B2 | 5 | PASS | 5/5 | 0 | 3192.6ms | 137.6ms | 0 |
| B3 | 8 | PASS | 8/8 | 0 | 4909ms | 230ms | 0 |
| B4 | 12 | PASS | 12/12 | 0 | 4593.3ms | 21.4ms | 0 |
| B5 | 16 | PASS | 16/16 | 0 | 5403.8ms | 13.2ms | 0 |

由此得到 `K_burst_pass=16`、`K_burst_fail=>16`。这只证明当前账号和时段能够完成一次 16 并发突发，不能证明长期并发 16 稳定，也没有找到官方硬上限。

随后执行三轮 180 秒持续测试；每名用户产生 6 个真实 ASR 分片，重试上限为 2：

| K/VU | 严格结果 | final | 成功 ASR 分片 | 被重试掩盖的异常 | final P95 / max | ASR 排队 P95 | CPU P95 | 内存峰值 | 归零 |
| ---: | --- | ---: | ---: | --- | --- | ---: | ---: | ---: | ---: |
| 12 | FAIL | 12/12 | 72 | `provider_error=12` | 8433 / 8647ms | 131.4ms | 7.065% | 277.2MiB | 3.8s |
| 8 | FAIL | 8/8 | 48 | `timeout=1` | 3864 / 3949ms | 1361.1ms | 5.92% | 180MiB | 3.7s |
| 5 | PASS | 5/5 | 30 | 0，重试 0 | 5264.6 / 5763ms | 119.8ms | 6.305% | 154.3MiB | 3.7s |

严格结论：

- 所有持续档最终都获得完整 final 且无降级，但严格门槛不允许用重试掩盖 Provider 异常，因此 12 和 8 均判失败。
- `K=12` 的 12 次异常集中在同一轮分片的第一次请求，重试后全部成功；日志没有对应的 HTTP 429 或 5xx 响应，因此只能推断为传输层/连接类 `provider_error`，不能声称是 OpenRouter 或 DeepInfra 的明确限流。
- `K=8` 出现一次 30 秒 ASR 超时，重试后成功；`K=5` 的 30 个分片全部一次成功。
- 当前零异常持续并发候选值为 `K_sustained_safe=5`，首个严格失败持续档为 `K_sustained_fail=8`，经验区间为 `5～8`。生产候选可考虑 `STT_MAX_CONCURRENCY=5`、`STT_ASR_QUEUE_SIZE=10`，但在真实整理和组合端到端测试完成前不修改 `.env`。
- 三档资源占用都很低、队列未触顶并在 4 秒内归零，失败来自 Provider/网络侧表现，不是 2 核 4GiB 容器的 CPU、内存或本地 ASR 队列。
- 名义发送音频费用估算约 `$0.01746`；把 1 秒重叠分片和失败尝试也按可能计费保守计算约 `$0.0191`，仍低于 `$0.02` 上限。准确费用以 OpenRouter 账单为准。

证据目录：

- 短阶梯和 `K=12` 持续档：`test-results/load/20260827-161430-real-asr-ladder/`
- `K=8` 持续档：`test-results/load/20260827-162425-real-asr-ladder/`
- `K=5` 持续档：`test-results/load/20260827-162819-real-asr-ladder/`

自动化入口为 `load/run-real-asr-ladder.ps1`，测试覆盖配置为 `load/docker-compose.real-asr.yml`。证据目录的 Secret 扫描无命中；每轮结束后正常 Compose 均恢复健康，重启 0、OOM 0，临时令牌和 `POLISH_ENABLED=false` 覆盖均已移除。

## 18. 2026-08-28 无 Clash 真实整理与端到端探索记录

### 18.1 真实整理 Provider 隔离结果

本轮使用 Mock ASR 固定文本和真实 DMX/DeepSeek 整理，只改变 `POLISH_LOCAL_CONCURRENCY`。短档关闭重试；180 秒档仍严格检查所有 Provider 异常，不能用重试后的 final 掩盖失败。

| 档位 | 结果 | final / applied | 降级 | stop-to-final P95 | 整理 inflight 峰值 | Provider 异常 |
| ---: | --- | ---: | ---: | ---: | ---: | ---: |
| K=3，30 秒 | PASS | 3/3 | 0 | 3114.7ms | 3 | 0 |
| K=5，30 秒 | PASS | 5/5 | 0 | 4848ms | 5 | 0 |
| K=5，180 秒 | PASS | 5/5 | 0 | 6937.6ms | 5 | 0 |

隔离结论：`LLM_K=5` 可以作为当前端到端候选值；该结果验证的是同步突发和长会话结束时的一批整理请求，不等价于连续数小时的 LLM 吞吐测试。证据目录：`test-results/load/20260828-133821-real-polish-ladder/`。

### 18.2 固定 ASR_K=5、LLM_K=5 的端到端结果

端到端阶梯使用真实 ASR、真实整理、单容器、单 Uvicorn worker、Redis 关闭，ASR 队列 10、整理队列 5。全部档位均为同步开始和停止，属于对 final 阶段突发较严格的场景。

| C/VU | 音频 | 严格结果 | final | 降级 | stop-to-final P95 | ASR 排队 P95 | 主要原因 |
| ---: | ---: | --- | ---: | ---: | ---: | ---: | --- |
| 5 | 30 秒 | PASS | 5/5 | 0 | 8559ms | 142.4ms | 无异常，但延迟余量较小 |
| 8 | 30 秒 | FAIL | 8/8 | 0 | 12047.9ms | 6792.2ms | ASR 本地排队和总延迟超线 |
| 6 | 30 秒 | FAIL | 6/6 | 0 | 12554.2ms | 4825.8ms | ASR 本地排队和总延迟超线 |
| 5 | 180 秒 | FAIL | 5/5 | 0 | 10481.2ms | 186.4ms | 总延迟略超 10 秒 |
| 4 | 180 秒 | FAIL | 4/4 | 0 | 11149ms | 133.4ms | 无本地排队瓶颈，但 Provider 尾延迟超线 |
| 3 | 180 秒，首次 | FAIL | 3/3 | 1 | 9587.2ms | 1251.7ms | `provider_error=1`、`timeout=8`、`queue_timeout=1` |
| 3 | 180 秒，冷却后唯一复测 | PASS | 3/3 | 0 | 9594ms | 142.3ms | 18 个 ASR 分片和 3 次整理均一次成功 |

关键证据目录：

- `C=5/8` 短档：`test-results/load/20260828-134502-real-e2e-ladder/`。该轮在 `C=8` 失败后曾因脚本控制流继续启动 `C=10`，随后人工立即中止；`C=10` 不产生结论。
- `C=6` 短档：`test-results/load/20260828-135819-real-e2e-ladder/`。
- `C=5` 持续档：`test-results/load/20260828-135957-real-e2e-ladder/`。
- `C=4` 持续档：`test-results/load/20260828-140511-real-e2e-ladder/`。
- `C=3` 首次和唯一复测：`test-results/load/20260828-141023-real-e2e-ladder/`、`test-results/load/20260828-141605-real-e2e-ladder/`。

### 18.3 是否继续提高 ASR K

在端到端 `C=8、ASR_K=5` 出现 ASR 排队后，单变量隔离测试了无 Clash 下的 `ASR_K=6`。6/6 final 成功、无降级、ASR 排队 P95 仅 129.8ms，旁路 DNS/TCP/TLS/HTTP 探针均无失败，但 stop-to-final P95 达到 17162.2ms，因此严格失败并停止 K=7。证据目录：`test-results/load/20260828-135525-real-asr-ladder/`。

结论：当前不能通过继续提高 ASR K 来实现 `C=10`。`ASR_K=6` 虽消除了本地排队，却显著放大上游处理尾延迟；继续扩大 K 只会增加同时请求数、TLS 连接、出站带宽和 Provider 压力，不能保证提高有效吞吐。

### 18.4 当前阶段结论

- 保持 `ASR_K=5`、`LLM_K=5` 为下一轮候选，不修改生产 `.env`。
- 当前证据不支持 `C=10`；本轮唯一通过的 180 秒真实端到端档为冷却复测后的 `C=3`，只能记为探索性候选，不能替代目标 2C4G 服务器上的 15/30 分钟和长稳验证。
- `C=4/5` 的主要问题是 Provider 尾延迟超过 10 秒，而不是本机 CPU、容器内存或本地队列容量；`C=6/8` 还叠加了 `ASR_K=5` 下的本地排队。
- 无 Clash 后成功率明显改善，且失败时未观察到明确 429/5xx，支持“先前失败更可能与网络/TLS/上游瞬时波动有关”；但旁路 OpenRouter 探针在部分持续档仍有 TCP timeout，因此不能把原因唯一归结为 Clash。
- 测试脚本已修正为：任一短阶梯严格失败后跳过指定持续档，仍执行正常配置恢复并生成报告，避免在已知失败后继续放大负载。

### 18.5 典型 120 秒输入复测

考虑到实际用户的一次语音输入通常约 2 分钟，本轮只把音频时长从 180 秒改为 120 秒，保持 `C=5`、`ASR_K=5`、`LLM_K=5`、队列、重试和 stop-to-final P95 `<10s` 门槛不变。当前输入仍是循环四次冻结的 30 秒真实 PCM，适合容量测试，但后续质量验收应补一条自然连续的 120 秒录音。

| 尝试 | 严格结果 | 客户端 final | ASR 成功/超时 | stop-to-final P95 | ASR 排队 P95 | 工作归零 |
| --- | --- | ---: | ---: | ---: | ---: | ---: |
| 首次 | FAIL | 2/5 | 17/11 | 7045ms（仅统计收到 final 的 2 路） | 4113.3ms | 60 秒内未归零 |
| 冷却后唯一复测 | FAIL | 3/5 | 20/4 | 25321.6ms | 2481.9ms | 45.5s |

两轮失败均出现 ASR 请求达到 30 秒请求超时；复测中部分分片重试后耗时 51～70 秒。OpenRouter 旁路可达探针两轮均无失败，容器 CPU P95 约 6%～7%、内存峰值约 164～172MiB，因此证据更接近“OpenRouter 后面的推理端或请求路径出现长尾”，而不是本机资源不足。旁路探针只能验证 DNS/TCP/TLS/HTTP 可达，不能证明一次真实推理会在 30 秒内完成。

客户端测试连接在录音时长加 30 秒后关闭；因此虽然服务端最终记录到 5 次整理 applied，迟到的 final 已经不能算用户成功收到。120 秒应作为后续主要业务容量场景，180 秒保留为长输入压力场景；但缩短到 120 秒没有让本轮 `C=5` 通过，当前阻塞项仍是真实 ASR 长尾和超时，而不是 10 秒 SLO 本身。

证据目录：

- 首次：`test-results/load/20260828-144645-real-e2e-ladder/`
- 冷却后唯一复测：`test-results/load/20260828-145400-real-e2e-ladder/`

### 18.6 20 秒分片、零重试的 120 秒连续三轮复测

为隔离 30 秒级 ASR 长尾，本轮保持 `C=5`、`ASR_K=5`、`LLM_K=5`、ASR 队列 10、整理队列 5 和 stop-to-final P95 `<10s` 不变，只将 ASR 目标/最大分片改为 `20s/30s`，并将 ASR 重试设为 0。三轮均使用 120 秒音频，每轮包含 5 个用户、每用户 6 个 ASR 分片，共 30 次 ASR 请求；失败不由重试掩盖。

| 轮次 | 严格结果 | final / 降级 | ASR 结果 | ASR Provider P95 / 最大 | ASR 排队 P95 | 整理结果与 Provider P95 | stop-to-final P95 / 最大 |
| ---: | --- | ---: | --- | ---: | ---: | --- | ---: |
| 1 | FAIL | 5/5 / 5 | 30 success | 24141ms / 24356ms | 4453.6ms | 5 network_error，6510ms | 7876ms / 7876ms |
| 2 | FAIL | 5/5 / 5 | 25 success、5 provider_error | 5295ms / 5446ms | 17.8ms | 5 applied，13523ms | 16056.6ms / 16658ms |
| 3 | PASS | 5/5 / 0 | 30 success | 5500ms / 6177ms | 213.4ms | 5 applied，8515ms | 9900ms / 9963ms |

三轮合计只严格通过 1/3，因此当前不能把 `C=5、ASR_K=5、LLM_K=5` 记为稳定通过，第三轮距离 10 秒 SLO 也只有约 100ms 余量。

- 本轮 90 次真实 ASR 调用中，没有出现 `http11.receive_response_headers.failed`，也没有请求达到 29 秒；因此 20 秒目标分片在这组三轮样本中避免了此前的 30 秒响应头超时，但样本不足以证明该问题已消除。
- 第一轮的首批 5 个 ASR 请求均成功，但耗时 23.5～24.4 秒，占满 5 个 ASR 槽位并导致后续请求排队；说明缩短分片没有消除 ASR 上游推理长尾。
- 第二轮的 5 个 ASR 失败均发生在 TLS 握手阶段，错误为 `ConnectError/SSLEOFError`、`connection.start_tls.failed`，耗时 5.28～5.45 秒；由于重试为 0，每个用户均有一个分片直接失败并产生降级 final。该轮 OpenRouter 旁路探针没有失败，说明轻量可达探针不能排除真实请求的瞬时 TLS 故障。
- 第一轮 5 次整理均因 `APIConnectionError` 降级，耗时 4.94～6.51 秒；第二轮整理虽全部成功，但 Provider P95 达到 13.523 秒，直接推动 stop-to-final P95 超过 10 秒。第三轮整理 P95 仍为 8.515 秒，是总延迟余量很小的主要原因。
- 三轮容器 CPU P95 为 7.03%～7.87%，内存峰值为 150.5～169.6MiB，工作在 3.5～4.4 秒内归零；没有证据指向本机 CPU、容器内存或任务泄漏是失败主因。

结论：20 秒分片值得保留为候选，但它只降低了单次 ASR 请求撞上 30 秒超时的概率，不能解决 TLS 瞬时失败、20 多秒 ASR 推理长尾或 LLM 整理长尾。下一步不应提高 `C` 或 `K`；应先分别给 ASR TLS/推理阶段和 LLM 阶段建立可接受的重试、超时与降级策略，再用相同参数做更长时间的重复验证。

证据目录：

- 第 1 轮：`test-results/load/20260828-152540-real-e2e-ladder/`
- 第 2 轮：`test-results/load/20260828-152819-real-e2e-ladder/`
- 第 3 轮：`test-results/load/20260828-153108-real-e2e-ladder/`

### 18.7 阿里百炼 Qwen Audio 3.0 ASR Flash 复测

本轮把 ASR 从 OpenRouter/DeepInfra 切换到阿里百炼
`qwen-audio-3.0-asr-flash-filetrans`，保持 `C=5`、`ASR_K=5`、`LLM_K=5`、
ASR 队列 10、整理队列 5、20/30 秒分片、ASR 重试 0 和 120 秒输入不变。
每轮 5 个用户、每用户 6 个 ASR 分片，共连续执行三轮。

接入预检最初通过 `/compatible-mode/v1/chat/completions` 请求该模型时返回
`404 model_not_supported`。阿里官方 `dashscope` SDK 表明该 `filetrans` 模型使用
原生 `audio/asr/transcription` 异步任务接口，而不是聊天补全接口。改为“内联
`data:audio/wav;base64` 提交、task-id 轮询、签名结果 URL 拉取”后，10 秒真实音频
预检一次成功，耗时 3023ms，得到 54 字文本；无需额外 OSS 上传。

| 轮次 | 自动化严格结果 | final / 降级 | ASR | ASR Provider P95 / 最大 | ASR 排队 P95 | 整理 | 整理 Provider P95 / 最大 | stop-to-final P95 / 最大 | ASR / LLM inflight 峰值 |
| ---: | --- | ---: | --- | ---: | ---: | --- | ---: | ---: | ---: |
| 1 | FAIL（仅并发证明条件） | 5/5 / 0 | 30/30，一次成功 | 5888 / 6151ms | 20ms | 5/5 applied | 5919 / 5919ms | 9415.2 / 9918ms | 5 / 3 |
| 2 | FAIL（仅并发证明条件） | 5/5 / 0 | 30/30，一次成功 | 5017 / 5653ms | 179.8ms | 5/5 applied | 2296 / 2296ms | 5560 / 5616ms | 5 / 2 |
| 3 | PASS | 5/5 / 0 | 30/30，一次成功 | 5052 / 6281ms | 18.8ms | 5/5 applied | 4332 / 4332ms | 8033.8 / 8068ms | 5 / 5 |

结论：

- 按用户可见结果和 10 秒 SLO，本轮三轮均成功：`15/15` 用户收到非空 final、
  `90/90` 次 ASR 和 `15/15` 次整理均一次成功，零降级、零 429、零 5xx、零
  ASR/整理超时；三轮 stop-to-final P95 和最大值均小于 10 秒。
- 自动化汇总中的 `sustained_passed=false` 不能解释为用户失败。前两轮唯一失败项是
  LLM inflight 峰值分别只有 3 和 2，未在该轮证明 `LLM_K=5` 被实际打满；第三轮
  LLM inflight 达到 5 并严格通过，因此已经证明该配置能够在 LLM 并发实际打满时成功。
- 与 18.6 的同参数旧 ASR 链路相比，本轮没有出现 ASR Provider 错误、30 秒超时或
  降级 final，支持“旧轮次主要受 OpenRouter/DeepInfra 路径或其后端推理长尾影响”。
  但当前样本只有三轮、90 次 ASR，不能外推为所有时段都能保证 10 秒内完成。
- 百炼旁路探针三轮分别记录 4、3、4 次 TLS 超时，但 Cloudflare、百度等控制目标
  同时也有 TLS 超时，且 90 次真实百炼请求全部成功；因此这些探针异常没有证据表明
  百炼推理请求失败，更接近探针并发/本机网络瞬时抖动。
- 容器 CPU P95 为 7.765%～10.67%，内存峰值 156.6～158MiB，ASR 队列峰值为 0，
  每轮工作在 3.5～4.3 秒内归零；本地 CPU、容器内存和 ASR 队列均不是瓶颈。

证据目录：`test-results/load/20260828-182307-real-e2e-ladder/`。测试结束后正常配置
恢复成功，服务健康，临时容量测试配置已移除。价格参数本轮未录入，因此
`estimated_cost_usd=0` 只表示未估算，不能解释为实际调用免费。

### 18.8 阿里百炼 ASR-only K=6 连续三轮验证

为单独确认百炼的有效 ASR 并发能否从 5 提高到 6，本轮关闭整理 Provider，执行
`C=6`、`ASR_K=6`、ASR 队列 12、20/30 秒分片、ASR 重试 0、120 秒输入的
ASR-only 测试。每轮 6 个用户、每用户 6 个分片，共连续执行三轮。

执行前发现 `load/docker-compose.real-asr.yml` 没有映射脚本传入的
`CAP_SEGMENT_TARGET_SECONDS` 和 `CAP_SEGMENT_MAX_SECONDS`，会悄悄沿用 `.env`
中的 30/45 秒。补上映射后，Compose 展开值确认是 20/30 秒，再开始真实调用。

| 轮次 | 严格结果 | final / 降级 | ASR | ASR Provider P95 / 最大 | ASR 排队 P95 | stop-to-final P95 / 最大 | ASR inflight / 队列峰值 |
| ---: | --- | ---: | ---: | ---: | ---: | ---: | ---: |
| 1 | PASS | 6/6 / 0 | 36/36，一次成功 | 5934 / 6470ms | 57.2ms | 3880.2 / 3889ms | 6 / 0 |
| 2 | PASS | 6/6 / 0 | 36/36，一次成功 | 6155 / 6805ms | 53.5ms | 4172.8 / 4175ms | 6 / 0 |
| 3 | PASS | 6/6 / 0 | 36/36，一次成功 | 4984 / 5021ms | 10.2ms | 4197.8 / 4217ms | 6 / 0 |

结论：

- 三轮全部严格通过，合计 `108/108` 次真实 ASR 一次成功、`18/18` 非空 final、
  零降级、零 429、零 5xx、零超时，且每轮 ASR inflight 均实际达到 6。因此可以把
  `ASR_K=6` 记为当前账号、网络和时段下已验证的百炼 ASR 并发值。
- ASR 队列峰值三轮均为 0，排队 P95 只有 10.2～57.2ms；提高到 K=6 没有产生
  本地排队瓶颈。容器 CPU P95 为 6.51%～8.58%，内存峰值 153.6～166.2MiB，
  每轮工作在 3.5～4.4 秒内归零。
- 这是 ASR-only 结论，不包含 DMX 整理。它证明百炼能承受 K=6，但不能单独证明
  `C=6` 的完整 `ASR + LLM` 链路仍满足 10 秒 SLO。下一步端到端测试应保持已经
  验证的 `LLM_K=5`，只把 `C/ASR_K` 从 5 提高到 6，以便隔离新增 ASR 容量的影响。

证据目录：`test-results/load/20260828-185158-real-asr-ladder/`。测试结束后正常配置
恢复成功，服务健康，临时容量测试配置已移除。价格参数仍未录入，
`estimated_cost_usd=0` 只表示未估算。

### 18.9 DMX 整理 LLM_K=6 隔离验证

在提高完整端到端并发前，先使用 Mock ASR 和真实 DMX `deepseek-v4-flash-0731`
隔离验证 `C=6、LLM_K=6`。三轮均为 30 秒同步结束，每轮产生 6 次真实整理请求。

| 轮次 | 严格结果 | final / 降级 | 整理 | 整理 inflight 峰值 | Provider P95 / 最大 | stop-to-final P95 |
| ---: | --- | ---: | ---: | ---: | ---: | ---: |
| 1 | PASS | 6/6 / 0 | 6/6 applied | 6 | 3432 / 3432ms | 3996ms |
| 2 | PASS | 6/6 / 0 | 6/6 applied | 6 | 3852 / 3852ms | 4558.2ms |
| 3 | PASS | 6/6 / 0 | 6/6 applied | 6 | 2979 / 2979ms | 3840.8ms |

结论：三轮 `18/18` 次真实整理全部成功、零降级、零 Provider 异常，且每轮
polish inflight 都实际达到 6。因此 `LLM_K=6` 可以记为当前账号和时段下已验证的
隔离整理并发值。证据目录：`test-results/load/20260828-190359-real-polish-ladder/`。

### 18.10 C=6、ASR_K=6、LLM_K=6 端到端验证

隔离 ASR_K=6 和 LLM_K=6 分别通过后，执行三轮完整链路。保持 120 秒输入、
20/30 秒分片、ASR 队列 12、整理队列 6、ASR 重试 0 不变。

| 轮次 | 严格结果 | final / 降级 | ASR | 整理 | stop-to-final P95 / 最大 | ASR / LLM inflight 峰值 |
| ---: | --- | ---: | --- | --- | ---: | ---: |
| 1 | FAIL | 6/6 / 3 | 34 success、2 rate_limited | 5 applied、1 network_error | 8433.5 / 9027ms | 6 / 6 |
| 2 | FAIL（仅并发证明条件） | 6/6 / 0 | 36 success | 6 applied | 7203.2 / 7252ms | 6 / 2 |
| 3 | FAIL | 6/6 / 6 | 24 success、12 provider_error | 6 applied | 9661.2 / 9689ms | 6 / 6 |

根因证据：

- 第 1 轮两次 ASR 失败是百炼明确返回 HTTP 429，`provider_code` 为
  `Throttling.RateQuota`，耗时约 2.4 秒。失败发生在录音中段，整理尚未开始，
  因此不能归因于 ASR 与 LLM 同时出站。
- 第 1 轮还有一次 DMX `APIConnectionError`，耗时 5367ms，导致第三个降级 final。
- 第 3 轮 12 次 ASR 失败全部发生在百炼任务提交的 TLS 握手阶段，错误为
  `ConnectError/SSLEOFError`、`connection.start_tls.failed`，分成两批各 6 次，
  耗时约 5.3～6.4 秒。该轮百炼旁路探针也记录 4 次 TLS 失败，而 Cloudflare、
  百度和 Microsoft 控制目标 TLS 失败均为 0，证据更接近百炼请求路径瞬时异常。
- 三轮 ASR 队列峰值均为 0，CPU P95 为 7.93%～11.195%，容器内存峰值
  168.1～179.1MiB，工作均在 3.5 秒内归零；本地 CPU、内存和队列不是失败原因。

结论：完整 `C=6、ASR_K=6、LLM_K=6` 三轮严格通过率为 0/3，其中两轮产生
真实降级，不能作为稳定配置。隔离测试证明 ASR_K=6 和 LLM_K=6 各自能够工作，
但不证明它们在不同时间窗口都能避开百炼 RateQuota、TLS 路径异常和 DMX 网络异常。
当前可重复的端到端候选仍是 18.7 的 `C=5、ASR_K=5、LLM_K=5`；若继续探索
`C=6`，下一轮应把 ASR_K 降回已稳定的 5、保留已隔离通过的 LLM_K=6，验证
`C=6、ASR_K=5、LLM_K=6` 是否能用小幅 ASR 排队换取更少的百炼并发风险。

证据目录：`test-results/load/20260828-190739-real-e2e-ladder/`。测试结束后正常配置
恢复成功，服务健康，临时容量测试配置已移除。价格参数未录入，
`estimated_cost_usd=0` 只表示未估算。

### 18.11 冻结 5/5/5 候选与 C=6、ASR_K=5、LLM_K=6 阶梯验证

根据 18.7～18.10 的分层结果，当前单实例正式候选冻结为 `C=5、ASR_K=5、
LLM_K=5`，目标/最大分片为 `20s/30s`，相邻分片音频 overlap 为 `1s`。本地
`.env` 和 `.env.example` 已同步该组核心参数，容器重建后展开值与 readiness 均确认
正常。`1s` overlap 暂不随容量阶梯调整：自然切片优先发生在连续 600ms 静音处，
强制切片时 1s 通常足以覆盖边界短语；增至 2s 不减少请求数，却会使 120s、6 分片
场景的重复音频从约 5s 增至约 10s。边界识别质量仍需使用自然连续录音单独 A/B，
不能由容量测试代替。

在冻结配置基础上只升一档，执行 `C=6、ASR_K=5、LLM_K=6`、ASR 队列 10、
整理队列 6、ASR 重试 0、20/30/1 分片和 120s 输入的连续三轮端到端验证。

| 轮次 | 严格结果 | final / 降级 | ASR | 整理 | stop-to-final P95 / 最大 | ASR queue-wait P95 | ASR / LLM inflight 峰值 |
| ---: | --- | ---: | --- | --- | ---: | ---: | ---: |
| 1 | FAIL | 6/6 / 0 | 36/36 success | 6/6 applied | 8029.2 / 8207ms | 8938ms | 5 / 5 |
| 2 | FAIL | 6/6 / 0 | 36/36 success | 6/6 applied | 8788.8 / 10342ms | 5991.5ms | 5 / 4 |
| 3 | FAIL | 6/6 / 0 | 36/36 success | 6/6 applied | 5308.5 / 5839ms | 11947ms | 5 / 5 |

三轮 `18/18` final、`108/108` ASR 和 `18/18` 整理全部成功，没有 Provider 错误
或降级；但 ASR 队列每轮均达到 1，queue-wait P95 为 5.99～11.95s，且第二轮有
一个用户 stop-to-final 达到 10.342s。三轮 LLM inflight 峰值只有 4～5，因此本用例
也没有重新证明 LLM_K=6 被打满；该能力只由 18.9 的 LLM-only 隔离测试证明。

结论：把 ASR_K 从 6 降到 5 避开了本轮百炼 429/TLS 降级，但代价不是“小幅排队”，
而是秒级长等待，并已使单用户越过 10s 目标。按照额定负载队列应接近 0 的既定门槛，
`C=6、ASR_K=5、LLM_K=6` 严格通过率仍为 0/3，不升级为正式候选，也不继续升到
`C=7`。当前候选保持 `5/5/5 + 20/30/1`。

证据目录：`test-results/load/20260828-202455-real-e2e-ladder/`。临时测试配置恢复后，
容器重新加载冻结的 5/5/5 配置且 readiness 正常。百炼结果 URL 中的临时 OSS
AccessKeyId/Signature 已从本轮及前三组相关证据日志中脱敏；服务同时将 `httpx` 日志
级别提高到 WARNING，后续只保留应用自身的结构化 Provider 诊断日志。
