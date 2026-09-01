import { AudioCapture } from "./audio-capture";
import { parseServerEvent, PROTOCOL_VERSION, SAMPLE_RATE, type StartEvent } from "./protocol";
import type {
  FinalResult,
  QueuedEvent,
  ReadyEvent,
  VoiceInputClientOptions,
  VoiceInputError,
  VoiceInputEvents,
  VoiceInputState,
} from "./types";

type Listener<K extends keyof VoiceInputEvents> = (event: VoiceInputEvents[K]) => void;
type Waiter<T> = { resolve(value: T): void; reject(error: Error): void };
type ServerFailure = Error & { code?: string; recoverable?: boolean; retry_after_ms?: number; closeCode?: number };

const DEFAULT_CONSTRAINTS: MediaTrackConstraints = {
  channelCount: 1,
  echoCancellation: true,
  noiseSuppression: true,
  autoGainControl: true,
};

export class VoiceInputClient {
  state: VoiceInputState = "idle";
  private readonly options: Required<Omit<VoiceInputClientOptions, "fallbackUrl" | "token" | "anonymousTokenUrl">>
    & Pick<VoiceInputClientOptions, "fallbackUrl" | "token" | "anonymousTokenUrl">;
  private readonly listeners = new Map<keyof VoiceInputEvents, Set<(event: never) => void>>();
  private socket: WebSocket | null = null;
  private capture: AudioCapture | null = null;
  private pcmChunks: ArrayBuffer[] = [];
  private stopTimer: number | null = null;
  private readyWaiter: Waiter<ReadyEvent> | null = null;
  private finalWaiter: Waiter<FinalResult> | null = null;
  private destroyed = false;
  private sessionToken: string | undefined;
  private backpressureSince: number | null = null;
  private forceFallback = false;
  private lastSocketFailure: ServerFailure | null = null;
  private readonly visibilityHandler = () => { if (document.visibilityState === "hidden") void this.cancel(); };
  private readonly offlineHandler = () => {
    if (this.state !== "recording") {
      void this.cancel();
      return;
    }
    this.lastSocketFailure = Object.assign(new Error("Network connection lost"), {
      code: "NETWORK_ERROR",
      recoverable: true,
    });
    this.forceFallback = true;
    void this.stop();
  };

  constructor(options: VoiceInputClientOptions) {
    if (!options.wsUrl) throw new Error("wsUrl is required");
    this.options = {
      wsUrl: options.wsUrl,
      fallbackUrl: options.fallbackUrl,
      workletUrl: options.workletUrl ?? "/sdk/pcm-worklet.js",
      maxDurationMs: options.maxDurationMs ?? 600_000,
      connectTimeoutMs: options.connectTimeoutMs ?? 8_000,
      finalTimeoutMs: options.finalTimeoutMs ?? 125_000,
      token: options.token,
      anonymousTokenUrl: options.anonymousTokenUrl,
      clientIdStorageKey: options.clientIdStorageKey ?? "lili-voice-input-client-id",
      language: options.language ?? "zh",
      mediaConstraints: options.mediaConstraints ?? DEFAULT_CONSTRAINTS,
    };
    this.sessionToken = options.token;
    if (typeof document !== "undefined") document.addEventListener("visibilitychange", this.visibilityHandler);
    if (typeof window !== "undefined") window.addEventListener("offline", this.offlineHandler);
  }

  static isSupported(): boolean {
    return AudioCapture.isSupported();
  }

  on<K extends keyof VoiceInputEvents>(type: K, listener: Listener<K>): () => void {
    const set = this.listeners.get(type) ?? new Set();
    set.add(listener as (event: never) => void);
    this.listeners.set(type, set);
    return () => set.delete(listener as (event: never) => void);
  }

  async start(): Promise<void> {
    if (this.destroyed) throw new Error("VoiceInputClient has been destroyed");
    if (!["idle", "completed", "error"].includes(this.state)) throw new Error("Voice input cannot start in the current state");
    if (!VoiceInputClient.isSupported()) throw new Error("This browser or page context does not support voice input");
    await this.cleanup();
    this.pcmChunks = [];
    this.capture = new AudioCapture(this.options.workletUrl, this.options.mediaConstraints, {
      onPcm: (buffer, rms) => this.handlePcm(buffer, rms),
    });
    try {
      this.sessionToken = this.options.token ?? await this.requestAnonymousToken();
      this.setState("requesting-permission");
      await this.capture.requestPermission();
      this.setState("connecting");
      const ready = await this.openSocket();
      this.options.maxDurationMs = Math.min(this.options.maxDurationMs, ready.max_duration_seconds * 1000);
      await this.capture.startGraph();
      this.setState("recording");
      this.stopTimer = window.setTimeout(() => void this.stop(), this.options.maxDurationMs);
    } catch (cause) {
      const error = toVoiceError(cause, "START_FAILED", "Unable to start voice input");
      await this.cleanup();
      this.setState("error");
      this.emit("error", error);
      throw cause;
    }
  }

  async stop(): Promise<FinalResult | null> {
    if (this.state !== "recording") return null;
    this.setState("finalizing");
    this.clearStopTimer();
    this.capture?.stopTracks();
    try {
      await this.capture?.flush();
      await this.capture?.close();
      const result = await this.commitSocket();
      this.emit("final", result);
      this.setState("completed");
      await this.cleanup(false);
      return result;
    } catch (streamCause) {
      if (shouldSuppressFallback(streamCause)) {
        const error = toVoiceError(streamCause, "CAPACITY_REACHED", "Voice transcription capacity reached");
        await this.cleanup();
        this.setState("error");
        this.emit("error", error);
        return null;
      }
      try {
        const result = await this.fallbackTranscription();
        this.emit("final", result);
        this.setState("completed");
        await this.cleanup(false);
        return result;
      } catch (fallbackCause) {
        const error = toVoiceError(fallbackCause ?? streamCause, "TRANSCRIPTION_FAILED", "Voice transcription failed");
        await this.cleanup();
        this.setState("error");
        this.emit("error", error);
        return null;
      }
    }
  }

  async cancel(): Promise<void> {
    if (this.socket?.readyState === WebSocket.OPEN) this.socket.send(JSON.stringify({ type: "cancel" }));
    await this.cleanup();
    this.setState("idle");
  }

  async destroy(): Promise<void> {
    await this.cancel();
    this.listeners.clear();
    if (typeof document !== "undefined") document.removeEventListener("visibilitychange", this.visibilityHandler);
    if (typeof window !== "undefined") window.removeEventListener("offline", this.offlineHandler);
    this.destroyed = true;
  }

  private async openSocket(): Promise<ReadyEvent> {
    let lastError: ServerFailure | null = null;
    for (let attempt = 0; attempt < 3; attempt += 1) {
      try {
        return await this.openSocketOnce();
      } catch (cause) {
        lastError = cause as ServerFailure;
        await this.closeSocketOnly();
        if (!isRetryableConnectionFailure(lastError) || attempt === 2) throw cause;
        const base = lastError.retry_after_ms ?? 1_000 * (2 ** attempt);
        await delay(base + Math.floor(Math.random() * 250));
      }
    }
    throw lastError ?? new Error("Unable to connect");
  }

  private async openSocketOnce(): Promise<ReadyEvent> {
    this.lastSocketFailure = null;
    this.socket = new WebSocket(this.options.wsUrl);
    this.socket.binaryType = "arraybuffer";
    this.socket.addEventListener("message", (event) => this.handleSocketMessage(event));
    this.socket.addEventListener("close", (event) => this.handleSocketClose(event));
    await new Promise<void>((resolve, reject) => {
      const timeout = window.setTimeout(() => reject(new Error("Connection timed out")), this.options.connectTimeoutMs);
      this.socket?.addEventListener("open", () => { window.clearTimeout(timeout); resolve(); }, { once: true });
      this.socket?.addEventListener("error", () => { window.clearTimeout(timeout); reject(new Error("Unable to connect")); }, { once: true });
    });
    const ready = new Promise<ReadyEvent>((resolve, reject) => {
      const timeout = window.setTimeout(() => reject(new Error("Session initialization timed out")), this.options.connectTimeoutMs);
      this.readyWaiter = {
        resolve: (value) => { window.clearTimeout(timeout); resolve(value); },
        reject: (error) => { window.clearTimeout(timeout); reject(error); },
      };
    });
    const start: StartEvent = {
      type: "start",
      protocol_version: PROTOCOL_VERSION,
      format: "pcm16",
      sample_rate: SAMPLE_RATE,
      language: this.options.language,
      ...(this.sessionToken ? { auth_token: this.sessionToken } : {}),
    };
    this.socket.send(JSON.stringify(start));
    const event = await ready;
    this.readyWaiter = null;
    this.emit("ready", event);
    return event;
  }

  private handlePcm(buffer: ArrayBuffer, rms: number): void {
    this.emit("volume", { rms });
    if (this.options.fallbackUrl) this.pcmChunks.push(buffer.slice(0));
    if ((this.state === "recording" || this.state === "finalizing") && this.socket?.readyState === WebSocket.OPEN) {
      if (this.socket.bufferedAmount >= 512 * 1024) {
        this.backpressureSince ??= performance.now();
        if (performance.now() - this.backpressureSince >= 3_000 && this.state === "recording") {
          this.forceFallback = true;
          this.socket.close(1011, "BACKPRESSURE");
          this.emit("error", { code: "BACKPRESSURE", message: "语音网络发送持续拥塞，正在切换备用上传", recoverable: true });
          void this.stop();
        }
        return;
      }
      if (this.socket.bufferedAmount < 256 * 1024) this.backpressureSince = null;
      this.socket.send(buffer);
    }
  }

  private handleSocketMessage(event: MessageEvent): void {
    const message = parseServerEvent(event.data);
    if (!message) return;
    if (message.type === "ready") {
      this.readyWaiter?.resolve(message);
      return;
    }
    if (message.type === "queued") {
      this.setState("queued");
      this.emit("queued", message as QueuedEvent);
      return;
    }
    if (message.type === "final") {
      this.finalWaiter?.resolve({ ...message, source: "websocket" });
      return;
    }
    const error = Object.assign(new Error(message.message), {
      code: message.code,
      recoverable: message.recoverable,
      retry_after_ms: message.retry_after_ms,
    });
    this.lastSocketFailure = error;
    const handled = Boolean(this.readyWaiter || this.finalWaiter);
    this.readyWaiter?.reject(error);
    this.finalWaiter?.reject(error);
    if (!handled) this.emit("error", message);
  }

  private handleSocketClose(event?: CloseEvent): void {
    const error = this.lastSocketFailure
      ?? Object.assign(new Error("Voice transcription connection closed"), { closeCode: event?.code });
    this.readyWaiter?.reject(error);
    this.finalWaiter?.reject(error);
    if (this.state === "recording") {
      this.forceFallback = true;
      void this.stop();
    }
  }

  private async commitSocket(): Promise<FinalResult> {
    if (this.forceFallback) throw this.lastSocketFailure ?? new Error("Voice transcription switched to fallback");
    if (!this.socket || this.socket.readyState !== WebSocket.OPEN) throw new Error("Voice transcription connection closed");
    const result = new Promise<FinalResult>((resolve, reject) => {
      const timeout = window.setTimeout(() => reject(new Error("Final transcription timed out")), this.options.finalTimeoutMs);
      this.finalWaiter = {
        resolve: (value) => { window.clearTimeout(timeout); resolve(value); },
        reject: (error) => { window.clearTimeout(timeout); reject(error); },
      };
    });
    this.socket.send(JSON.stringify({ type: "commit" }));
    const final = await result;
    this.finalWaiter = null;
    return final;
  }

  private async fallbackTranscription(): Promise<FinalResult> {
    if (!this.options.fallbackUrl) throw new Error("HTTP fallback is disabled");
    if (!this.pcmChunks.length) throw new Error("No valid recording was captured");
    const formData = new FormData();
    formData.append("file", encodePcm16Wav(this.pcmChunks, SAMPLE_RATE), "recording.wav");
    formData.append("language", this.options.language);
    const headers: Record<string, string> = { "X-Voice-Fallback": "1" };
    if (this.sessionToken) headers.Authorization = `Bearer ${this.sessionToken}`;
    const response = await fetch(this.options.fallbackUrl, { method: "POST", body: formData, headers });
    const data = await response.json().catch(() => null) as Record<string, unknown> | null;
    if (!response.ok) {
      const message = typeof data?.message === "string" ? data.message : typeof data?.detail === "string" ? data.detail : "HTTP fallback failed";
      throw Object.assign(new Error(message), {
        code: data?.code,
        retry_after_ms: Number(data?.retry_after_ms) || undefined,
      });
    }
    if (typeof data?.text !== "string" || !data.text.trim()) throw new Error("HTTP fallback returned no text");
    return {
      type: "final",
      text: data.text.trim(),
      polished: Boolean(data.polished),
      polish_status: (data.polish_status as FinalResult["polish_status"]) ?? "fallback",
      polish_reason: isPolishReason(data.polish_reason) ? data.polish_reason : null,
      degraded: Boolean(data.degraded),
      degraded_stage: data.degraded_stage === "asr" || data.degraded_stage === "polish" ? data.degraded_stage : null,
      segment_count: Number(data.segment_count) || 0,
      failed_segment_count: Number(data.failed_segment_count) || 0,
      latency_ms: Number(data.latency_ms) || 0,
      polish_latency_ms: Number(data.polish_latency_ms) || 0,
      total_latency_ms: Number(data.total_latency_ms) || 0,
      admission_wait_ms: Number(data.admission_wait_ms) || 0,
      asr_queue_wait_ms: Number(data.asr_queue_wait_ms) || 0,
      source: "http-fallback",
    };
  }

  private async cleanup(clearPcm = true): Promise<void> {
    this.clearStopTimer();
    await this.capture?.close();
    this.capture = null;
    if (this.socket && this.socket.readyState < WebSocket.CLOSING) this.socket.close();
    this.socket = null;
    this.readyWaiter = null;
    this.finalWaiter = null;
    if (clearPcm) this.pcmChunks = [];
    this.backpressureSince = null;
    this.forceFallback = false;
    this.lastSocketFailure = null;
  }

  private async closeSocketOnly(): Promise<void> {
    if (this.socket && this.socket.readyState < WebSocket.CLOSING) this.socket.close();
    this.socket = null;
    this.readyWaiter = null;
    this.finalWaiter = null;
  }

  private async requestAnonymousToken(): Promise<string | undefined> {
    const endpoint = this.options.anonymousTokenUrl ?? deriveAnonymousTokenUrl(this.options.fallbackUrl, this.options.wsUrl);
    if (!endpoint) return undefined;
    let clientId = localStorage.getItem(this.options.clientIdStorageKey);
    if (!clientId) {
      clientId = crypto.randomUUID();
      localStorage.setItem(this.options.clientIdStorageKey, clientId);
    }
    const response = await fetch(endpoint, {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ client_id: clientId }),
    });
    if (response.status === 404) return undefined;
    const data = await response.json().catch(() => null) as { token?: string; message?: string; detail?: string } | null;
    if (!response.ok || !data?.token) throw new Error(data?.message ?? data?.detail ?? "Unable to obtain anonymous voice token");
    return data.token;
  }

  private clearStopTimer(): void {
    if (this.stopTimer !== null) window.clearTimeout(this.stopTimer);
    this.stopTimer = null;
  }

  private setState(state: VoiceInputState): void {
    if (this.state === state) return;
    this.state = state;
    this.emit("statechange", { state });
  }

  private emit<K extends keyof VoiceInputEvents>(type: K, event: VoiceInputEvents[K]): void {
    for (const listener of this.listeners.get(type) ?? []) listener(event as never);
  }
}

function encodePcm16Wav(chunks: ArrayBuffer[], sampleRate: number): Blob {
  const dataLength = chunks.reduce((total, chunk) => total + chunk.byteLength, 0);
  const buffer = new ArrayBuffer(44 + dataLength);
  const view = new DataView(buffer);
  writeAscii(view, 0, "RIFF");
  view.setUint32(4, 36 + dataLength, true);
  writeAscii(view, 8, "WAVE");
  writeAscii(view, 12, "fmt ");
  view.setUint32(16, 16, true);
  view.setUint16(20, 1, true);
  view.setUint16(22, 1, true);
  view.setUint32(24, sampleRate, true);
  view.setUint32(28, sampleRate * 2, true);
  view.setUint16(32, 2, true);
  view.setUint16(34, 16, true);
  writeAscii(view, 36, "data");
  view.setUint32(40, dataLength, true);
  const output = new Uint8Array(buffer, 44);
  let offset = 0;
  for (const chunk of chunks) {
    output.set(new Uint8Array(chunk), offset);
    offset += chunk.byteLength;
  }
  return new Blob([buffer], { type: "audio/wav" });
}

function writeAscii(view: DataView, offset: number, value: string): void {
  for (let index = 0; index < value.length; index += 1) view.setUint8(offset + index, value.charCodeAt(index));
}

function toVoiceError(cause: unknown, code: string, fallback: string): VoiceInputError {
  const failure = cause as ServerFailure | null;
  return {
    code: failure?.code ?? code,
    message: cause instanceof Error ? cause.message : fallback,
    recoverable: failure?.recoverable ?? true,
    ...(failure?.retry_after_ms ? { retry_after_ms: failure.retry_after_ms } : {}),
    cause,
  };
}

function isPolishReason(value: unknown): value is FinalResult["polish_reason"] & string {
  return [
    "empty_input",
    "configuration_error",
    "rate_limited",
    "timeout",
    "network_error",
    "provider_error",
    "invalid_output",
    "empty_output",
    "capacity_reached",
  ].includes(value as string);
}

function isRetryableConnectionFailure(error: ServerFailure): boolean {
  return Boolean(
    error.recoverable
    || error.closeCode === 1012
    || error.closeCode === 1013
    || ["CAPACITY_REACHED", "QUEUE_TIMEOUT", "RATE_LIMITED", "SERVER_RESTART"].includes(error.code ?? ""),
  );
}

function delay(milliseconds: number): Promise<void> {
  return new Promise((resolve) => window.setTimeout(resolve, milliseconds));
}

function deriveAnonymousTokenUrl(fallbackUrl: string | undefined, wsUrl: string): string | undefined {
  try {
    const source = fallbackUrl ? new URL(fallbackUrl) : new URL(wsUrl.replace(/^ws:/, "http:").replace(/^wss:/, "https:"));
    return new URL("/v1/anonymous-tokens", source.origin).toString();
  } catch {
    return undefined;
  }
}

function shouldSuppressFallback(cause: unknown): boolean {
  const code = (cause as ServerFailure | null)?.code;
  return code === "CAPACITY_REACHED" || code === "QUEUE_TIMEOUT";
}
