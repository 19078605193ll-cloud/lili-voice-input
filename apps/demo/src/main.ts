import "@fontsource-variable/geist";
import "@fontsource-variable/geist-mono";
import { VoiceInputClient, type FinalResult, type VoiceInputState } from "@lili-voice-input/browser";
import "./styles.css";

const byId = <T extends HTMLElement>(id: string): T => {
  const element = document.getElementById(id);
  if (!element) throw new Error(`Missing element: ${id}`);
  return element as T;
};

const serviceUrl = byId<HTMLInputElement>("service-url");
const tokenInput = byId<HTMLInputElement>("access-token");
const connectionForm = byId<HTMLFormElement>("connection-form");
const healthButton = byId<HTMLButtonElement>("health-button");
const docsLink = byId<HTMLAnchorElement>("docs-link");
const recordButton = byId<HTMLButtonElement>("record-button");
const recordLabel = byId<HTMLSpanElement>("record-label");
const stopButton = byId<HTMLButtonElement>("stop-button");
const cancelButton = byId<HTMLButtonElement>("cancel-button");
const recordStatus = byId<HTMLParagraphElement>("record-status");
const timer = byId<HTMLParagraphElement>("timer");
const meterFill = byId<HTMLSpanElement>("meter-fill");
const supportCopy = byId<HTMLParagraphElement>("support-copy");
const permissionBadge = byId<HTMLSpanElement>("permission-badge");
const connectionBadge = byId<HTMLSpanElement>("connection-badge");
const resultText = byId<HTMLTextAreaElement>("result-text");
const copyButton = byId<HTMLButtonElement>("copy-button");
const clearButton = byId<HTMLButtonElement>("clear-button");
const copyDiagnosticsButton = byId<HTMLButtonElement>("copy-diagnostics-button");
const eventList = byId<HTMLOListElement>("event-list");

const metrics = {
  source: byId<HTMLElement>("metric-source"),
  polish: byId<HTMLElement>("metric-polish"),
  segments: byId<HTMLElement>("metric-segments"),
  failed: byId<HTMLElement>("metric-failed"),
  asr: byId<HTMLElement>("metric-asr"),
  polishTime: byId<HTMLElement>("metric-polish-time"),
  total: byId<HTMLElement>("metric-total"),
};

let client: VoiceInputClient | null = null;
let startedAt = 0;
let timerId: number | null = null;
let lastResult: FinalResult | null = null;
const diagnosticEvents: Array<Record<string, unknown>> = [];

const stateCopy: Record<VoiceInputState, string> = {
  idle: "等待开始",
  "requesting-permission": "正在请求麦克风权限",
  connecting: "正在连接语音服务",
  queued: "服务繁忙，正在等待可用语音通道",
  recording: "正在录音并发送 PCM 分块",
  finalizing: "正在等待识别、合并和润色",
  completed: "转写完成",
  error: "转写失败，请检查诊断信息",
};

function normalizeBaseUrl(): string {
  const base = serviceUrl.value.trim().replace(/\/$/, "");
  const parsed = new URL(base);
  if (!(["http:", "https:"].includes(parsed.protocol))) throw new Error("Service URL 必须使用 http 或 https");
  return parsed.toString().replace(/\/$/, "");
}

function buildClient(): VoiceInputClient {
  const base = normalizeBaseUrl();
  const wsBase = base.replace(/^http:/, "ws:").replace(/^https:/, "wss:");
  const instance = new VoiceInputClient({
    wsUrl: `${wsBase}/v1/transcriptions/stream`,
    fallbackUrl: `${base}/v1/transcriptions`,
    anonymousTokenUrl: `${base}/v1/anonymous-tokens`,
    workletUrl: `${window.location.origin}/demo/pcm-worklet.js`,
    token: tokenInput.value || undefined,
  });
  instance.on("statechange", ({ state }) => updateState(state));
  instance.on("ready", (event) => {
    setBadge(connectionBadge, "已连接", "success");
    addEvent("ready", { session_id: event.session_id, capabilities: event.capabilities });
    });
    instance.on("queued", (event) => {
      setBadge(connectionBadge, `排队中 · 前方 ${Math.max(0, event.position - 1)}`, "warning");
    addEvent("queued", { ...event });
  });
  instance.on("volume", ({ rms }) => {
    meterFill.style.transform = `scaleX(${Math.min(1, rms * 5).toFixed(3)})`;
  });
  instance.on("final", (result) => showResult(result));
  instance.on("error", (error) => {
    setBadge(connectionBadge, "错误", "error");
    addEvent("error", { code: error.code, message: error.message, recoverable: error.recoverable });
  });
  return instance;
}

function updateState(state: VoiceInputState): void {
  recordStatus.textContent = stateCopy[state];
  const recording = state === "recording";
  const busy = ["requesting-permission", "connecting", "queued", "finalizing"].includes(state);
  recordButton.disabled = recording || busy;
  recordButton.dataset.recording = String(recording);
  recordButton.dataset.state = busy ? "loading" : state === "error" ? "error" : state === "completed" ? "success" : "default";
  recordLabel.textContent = busy ? "处理中" : state === "completed" ? "再次录音" : "开始录音";
  stopButton.disabled = !recording;
  cancelButton.disabled = !recording && !busy;
  if (recording) {
    startedAt = performance.now();
    startTimer();
    setBadge(permissionBadge, "麦克风使用中", "success");
  } else {
    stopTimer();
    meterFill.style.transform = "scaleX(0)";
  }
  addEvent("statechange", { state });
}

function startTimer(): void {
  stopTimer();
  const update = () => {
    const elapsed = Math.max(0, performance.now() - startedAt);
    const minutes = Math.floor(elapsed / 60_000).toString().padStart(2, "0");
    const seconds = Math.floor((elapsed % 60_000) / 1_000).toString().padStart(2, "0");
    const tenths = Math.floor((elapsed % 1_000) / 100);
    timer.textContent = `${minutes}:${seconds}.${tenths}`;
  };
  update();
  timerId = window.setInterval(update, 100);
}

function stopTimer(): void {
  if (timerId !== null) window.clearInterval(timerId);
  timerId = null;
}

function showResult(result: FinalResult): void {
  lastResult = result;
  resultText.value = result.text;
  metrics.source.textContent = result.source;
  metrics.polish.textContent = result.polish_reason
    ? `${result.polish_status} (${result.polish_reason})`
    : result.polish_status;
  metrics.segments.textContent = String(result.segment_count);
  metrics.failed.textContent = String(result.failed_segment_count);
  metrics.asr.textContent = `${result.latency_ms} ms`;
  metrics.polishTime.textContent = `${result.polish_latency_ms} ms`;
  metrics.total.textContent = `${result.total_latency_ms} ms`;
  addEvent("final", { ...result, text: "[excluded from diagnostics]" });
}

function addEvent(type: string, detail: Record<string, unknown>): void {
  const entry = { at: new Date().toISOString(), type, ...detail };
  diagnosticEvents.push(entry);
  if (diagnosticEvents.length > 100) diagnosticEvents.shift();
  const item = document.createElement("li");
  item.textContent = `${entry.at} · ${type} · ${JSON.stringify(detail)}`;
  eventList.append(item);
  while (eventList.children.length > 100) eventList.firstElementChild?.remove();
}

function setBadge(element: HTMLElement, text: string, tone: "neutral" | "success" | "warning" | "error"): void {
  element.textContent = text;
  element.dataset.tone = tone;
}

async function copyWithFeedback(button: HTMLButtonElement, value: string): Promise<void> {
  if (!value) return;
  await navigator.clipboard.writeText(value);
  const original = button.textContent;
  button.textContent = "已复制";
  button.dataset.state = "success";
  window.setTimeout(() => {
    button.textContent = original;
    button.dataset.state = "default";
  }, 2500);
}

recordButton.addEventListener("click", async () => {
  try {
    await client?.destroy();
    client = buildClient();
    await client.start();
  } catch (error) {
    setBadge(permissionBadge, error instanceof DOMException && error.name === "NotAllowedError" ? "权限被拒绝" : "启动失败", "error");
  }
});

stopButton.addEventListener("click", () => void client?.stop());
cancelButton.addEventListener("click", () => void client?.cancel());

connectionForm.addEventListener("submit", async (event) => {
  event.preventDefault();
  healthButton.disabled = true;
  healthButton.dataset.state = "loading";
  healthButton.textContent = "检查中";
  try {
    const base = normalizeBaseUrl();
    const response = await fetch(`${base}/health/ready`);
    const data = await response.json() as { status?: string; errors?: string[] };
    docsLink.href = `${base}/docs`;
    if (!response.ok) throw new Error(data.errors?.join("；") || "服务尚未就绪");
    setBadge(connectionBadge, "服务就绪", "success");
    addEvent("health", { status: data.status });
    healthButton.dataset.state = "success";
  } catch (error) {
    setBadge(connectionBadge, "服务未就绪", "error");
    addEvent("health_error", { message: error instanceof Error ? error.message : "检查失败" });
    healthButton.dataset.state = "error";
  } finally {
    healthButton.disabled = false;
    healthButton.textContent = "检查服务";
  }
});

serviceUrl.addEventListener("blur", () => {
  try {
    normalizeBaseUrl();
    serviceUrl.setAttribute("aria-invalid", "false");
  } catch (error) {
    serviceUrl.setAttribute("aria-invalid", "true");
    byId("service-helper").textContent = error instanceof Error ? error.message : "Service URL 无效";
  }
});

copyButton.addEventListener("click", () => void copyWithFeedback(copyButton, resultText.value));
clearButton.addEventListener("click", () => {
  resultText.value = "";
  lastResult = null;
});
copyDiagnosticsButton.addEventListener("click", () => {
  const diagnostics = { service_url: serviceUrl.value, result: lastResult ? { ...lastResult, text: "[excluded]" } : null, events: diagnosticEvents };
  void copyWithFeedback(copyDiagnosticsButton, JSON.stringify(diagnostics, null, 2));
});

if (VoiceInputClient.isSupported()) {
  supportCopy.textContent = "浏览器支持 AudioWorklet、WebSocket 和安全麦克风访问。";
  setBadge(permissionBadge, "能力可用", "success");
} else {
  supportCopy.textContent = "当前浏览器或页面环境不支持所需的音频能力。";
  setBadge(permissionBadge, "不支持", "error");
  recordButton.disabled = true;
}

window.addEventListener("beforeunload", () => { void client?.destroy(); });
