# 浏览器接入

## 安全要求

- 麦克风只能在 HTTPS 或 localhost 页面使用。
- Provider API Key 只配置在服务端。
- 公开网站不要把长期 `SERVICE_TOKEN` 写入 JavaScript；使用同域反向代理和宿主系统认证。
- AudioWorklet 文件必须能被浏览器直接访问，并返回 JavaScript MIME 类型。

## 输入框适配

SDK 只返回文本。下面的函数可以替换输入框当前选区，并保留前后内容：

```ts
function insertTranscript(input: HTMLTextAreaElement, text: string) {
  const start = input.selectionStart ?? input.value.length;
  const end = input.selectionEnd ?? start;
  input.setRangeText(text, start, end, "end");
  input.dispatchEvent(new Event("input", { bubbles: true }));
}
```

Vue/React 等受控组件需要通过自己的状态更新函数写入文本，不能只修改 DOM value。完整示例位于 `examples/`。

## 生命周期

- 组件挂载时创建 `VoiceInputClient`。
- 点击开始时调用 `start()`。
- 点击停止时调用 `stop()`。
- 页面切换或组件卸载时调用 `destroy()`。
- `finalizing` 期间应禁用重复提交，但仍允许取消。

