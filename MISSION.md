# Mission: 理解并安全配置语音输入并发链路

## Why
理解 `lili-voice-input` 从浏览器录音、ASR 分段、文字整理到返回 final 的真实实现，以便为 2 核 4GB 单实例选择可解释、可测试的容量参数，而不是靠猜测上线。

## Success looks like
- 能用自己的话解释语音、ASR 分片、合并文字、LLM 整理和 final 的先后关系
- 能区分用户准入 `C/Q`、ASR `K/Q`、整理 `K/Q` 各自限制的对象
- 能根据压测证据选择生产候选值并指出尚未验证的 Provider 上限

## Constraints
- 使用简明中文、具体例子和流程图，控制单次信息量
- 先理解一个阶段，再进入下一个阶段
- 压测要控制时间和真实 Provider 费用

## Out of scope
- 深入研究 ASR 或 LLM 模型内部神经网络结构
- 在没有压测证据前直接修改生产容量
