# 语音输入并发链路资源

## Knowledge

- [项目流式会话实现](server/src/lili_voice_input/services/streaming.py)
  本项目端到端流程的第一手来源。用于核对 ASR 结果等待、排序、合并、整理和 final 字段。
- [项目整理调度实现](server/src/lili_voice_input/services/polishing.py)
  用于核对整理成功、关闭、排队失败和 Provider 失败时的 fallback 行为。
- [项目本地并发限制器](server/src/lili_voice_input/services/limiter.py)
  用于核对整理并发、队列容量和排队超时的准确语义。
- [项目整理 Provider 适配器](server/src/lili_voice_input/providers/openai_polisher.py)
  用于核对发送给 LLM 的消息、模型参数、超时、错误分类和响应读取。
- [项目分片文字合并算法](server/src/lili_voice_input/audio/merger.py)
  用于核对分片顺序和 1 秒音频重叠造成的重复文字如何去除。

## Wisdom (Communities)

当前课程只解释本项目源码，不需要依赖外部社区经验。

## Gaps

- 当前 DMX/DeepSeek 整理 Provider 的真实持续并发上限尚未压测。
- 多实例 Redis 全局整理并发尚未验证。
