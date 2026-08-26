interface WorkletPcmMessage {
  type: "pcm";
  buffer: ArrayBuffer;
  rms: number;
}

interface WorkletFlushedMessage {
  type: "flushed";
}

type WorkletMessage = WorkletPcmMessage | WorkletFlushedMessage;

declare global {
  interface Window {
    webkitAudioContext?: typeof AudioContext;
  }
}

export interface AudioCaptureCallbacks {
  onPcm(buffer: ArrayBuffer, rms: number): void;
}

export class AudioCapture {
  private stream: MediaStream | null = null;
  private context: AudioContext | null = null;
  private source: MediaStreamAudioSourceNode | null = null;
  private worklet: AudioWorkletNode | null = null;
  private silentGain: GainNode | null = null;
  private flushResolve: (() => void) | null = null;

  constructor(
    private readonly workletUrl: string,
    private readonly constraints: MediaTrackConstraints,
    private readonly callbacks: AudioCaptureCallbacks,
  ) {}

  static isSupported(): boolean {
    if (typeof window === "undefined" || typeof navigator === "undefined") return false;
    const isLocalhost = ["localhost", "127.0.0.1", "::1"].includes(window.location.hostname);
    const AudioContextClass = window.AudioContext ?? window.webkitAudioContext;
    return Boolean(
      (window.isSecureContext || isLocalhost)
      && typeof navigator.mediaDevices?.getUserMedia === "function"
      && AudioContextClass
      && "audioWorklet" in AudioContextClass.prototype
      && window.WebSocket,
    );
  }

  async requestPermission(): Promise<void> {
    this.stream = await navigator.mediaDevices.getUserMedia({ audio: this.constraints });
  }

  async startGraph(): Promise<void> {
    if (!this.stream) throw new Error("Microphone permission was not granted");
    const AudioContextClass = window.AudioContext ?? window.webkitAudioContext;
    if (!AudioContextClass) throw new Error("AudioContext is not supported");
    this.context = new AudioContextClass({ latencyHint: "interactive" });
    await this.context.audioWorklet.addModule(this.workletUrl);
    this.source = this.context.createMediaStreamSource(this.stream);
    this.worklet = new AudioWorkletNode(this.context, "lili-pcm-capture");
    this.silentGain = this.context.createGain();
    this.silentGain.gain.value = 0;
    this.worklet.port.onmessage = (event: MessageEvent<WorkletMessage>) => {
      if (event.data.type === "flushed") {
        this.flushResolve?.();
        this.flushResolve = null;
        return;
      }
      this.callbacks.onPcm(event.data.buffer, Number(event.data.rms) || 0);
    };
    this.source.connect(this.worklet);
    this.worklet.connect(this.silentGain);
    this.silentGain.connect(this.context.destination);
    await this.context.resume();
  }

  stopTracks(): void {
    this.stream?.getTracks().forEach((track) => track.stop());
    this.stream = null;
  }

  async flush(): Promise<void> {
    if (!this.worklet) return;
    await new Promise<void>((resolve) => {
      const timeout = window.setTimeout(resolve, 250);
      this.flushResolve = () => {
        window.clearTimeout(timeout);
        resolve();
      };
      this.worklet?.port.postMessage({ type: "flush" });
    });
  }

  async close(): Promise<void> {
    this.source?.disconnect();
    this.worklet?.disconnect();
    this.silentGain?.disconnect();
    this.stopTracks();
    if (this.context && this.context.state !== "closed") await this.context.close();
    this.context = null;
    this.source = null;
    this.worklet = null;
    this.silentGain = null;
    this.flushResolve = null;
  }
}
