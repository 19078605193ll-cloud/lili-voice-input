import { AudioCapture } from "./audio-capture";
import { parseServerEvent, PROTOCOL_VERSION, SAMPLE_RATE, type StartEvent } from "./protocol";
import type {
  FinalResult,
  ReadyEvent,
  VoiceInputClientOptions,
  VoiceInputError,
  VoiceInputEvents,
  VoiceInputState,
} from "./types";

type Listener<K extends keyof VoiceInputEvents> = (event: VoiceInputEvents[K]) => void;
type Waiter<T> = { resolve(value: T): void; reject(error: Error): void };

const DEFAULT_CONSTRAINTS: MediaTrackConstraints = {
  channelCount: 1,
  echoCancellation: true,
  noiseSuppression: true,
  autoGainControl: true,
};

export class VoiceInputClient {
  state: VoiceInputState = "idle";
  private readonly options: Required<Omit<VoiceInputClientOptions, "fallbackUrl" | "token">> & Pick<VoiceInputClientOptions, "fallbackUrl" | "token">;
  private readonly listeners = new Map<keyof VoiceInputEvents, Set<(event: never) => void>>();
  private socket: WebSocket | null = null;
  private capture: AudioCapture | null = null;
  private pcmChunks: ArrayBuffer[] = [];
  private stopTimer: number | null = null;
  private readyWaiter: Waiter<ReadyEvent> | null = null;
  private finalWaiter: Waiter<FinalResult> | null = null;
  private destroyed = false;

  constructor(options: VoiceInputClientOptions) {
    if (!options.wsUrl) throw new Error("wsUrl is required");
    this.options = {
      wsUrl: options.wsUrl,
      fallbackUrl: options.fallbackUrl,
      workletUrl: options.workletUrl ?? "/sdk/pcm-worklet.js",
      maxDurationMs: options.maxDurationMs ?? 600_000,
      connectTimeoutMs: options.connectTimeoutMs ?? 5_000,
      finalTimeoutMs: options.finalTimeoutMs ?? 125_000,
      token: options.token,
      language: options.language ?? "zh",
      mediaConstraints: options.mediaConstraints ?? DEFAULT_CONSTRAINTS,
    };
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
    this.destroyed = true;
  }

  private async openSocket(): Promise<ReadyEvent> {
    this.socket = new WebSocket(this.options.wsUrl);
    this.socket.binaryType = "arraybuffer";
    this.socket.addEventListener("message", (event) => this.handleSocketMessage(event));
    this.socket.addEventListener("close", () => this.handleSocketClose());
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
      ...(this.options.token ? { auth_token: this.options.token } : {}),
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
    if (message.type === "final") {
      this.finalWaiter?.resolve({ ...message, source: "websocket" });
      return;
    }
    const error = Object.assign(new Error(message.message), { code: message.code });
    const handled = Boolean(this.readyWaiter || this.finalWaiter);
    this.readyWaiter?.reject(error);
    this.finalWaiter?.reject(error);
    if (!handled) this.emit("error", message);
  }

  private handleSocketClose(): void {
    const error = new Error("Voice transcription connection closed");
    this.readyWaiter?.reject(error);
    this.finalWaiter?.reject(error);
  }

  private async commitSocket(): Promise<FinalResult> {
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
    const headers = this.options.token ? { Authorization: `Bearer ${this.options.token}` } : undefined;
    const response = await fetch(this.options.fallbackUrl, { method: "POST", body: formData, headers });
    const data = await response.json().catch(() => null) as Record<string, unknown> | null;
    if (!response.ok) throw new Error(typeof data?.detail === "string" ? data.detail : "HTTP fallback failed");
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
  return {
    code,
    message: cause instanceof Error ? cause.message : fallback,
    recoverable: true,
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
  ].includes(value as string);
}
