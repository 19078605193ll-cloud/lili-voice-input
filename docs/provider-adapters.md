# Provider adapters

核心流水线只依赖两个接口：`AsrProvider.transcribe()` 和 `TextPolisher.polish()`。

新增 ASR Provider 时：

- 接收内存音频，不落盘。
- 返回非空纯文本。
- 把认证、限流、超时、请求错误归一化为 `AsrProviderError`。
- 不在日志中输出音频和文本。

新增润色 Provider 时：

- `TextPolisher.polish()` 接收完整的合并 ASR 文本，并返回非空 `str`。
- 单次整理只发起一次普通文本模型调用，并直接返回整理后的文本。
- 只清理返回文本的首尾空白，不在后端转换或重写字符。
- 把配置、限流、超时、网络和上游错误归一化为 `PolishProviderError`，由 `PolishingService` 回退到原始 ASR 文本。
- 日志可以记录耗时、用量与异常类型，但不得输出输入或整理后的文本。
