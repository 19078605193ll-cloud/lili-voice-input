declare const sampleRate: number;
declare class AudioWorkletProcessor {
  readonly port: MessagePort;
  process(inputs: Float32Array[][]): boolean;
}
declare function registerProcessor(name: string, processorCtor: typeof AudioWorkletProcessor): void;

class LiliPcmCaptureProcessor extends AudioWorkletProcessor {
  private readonly targetSampleRate = 16_000;
  private readonly resampleRatio = sampleRate / this.targetSampleRate;
  private inputSamples: number[] = [];
  private inputPosition = 0;
  private outputSamples: number[] = [];
  private readonly outputFrameSize = 1_600;

  constructor() {
    super();
    this.port.onmessage = (event: MessageEvent<{ type?: string }>) => {
      if (event.data?.type === "flush") {
        this.resample();
        this.emitOutput(true);
        this.port.postMessage({ type: "flushed" });
      }
    };
  }

  process(inputs: Float32Array[][]): boolean {
    const input = inputs[0]?.[0];
    if (!input?.length) return true;
    for (const sample of input) this.inputSamples.push(sample);
    this.resample();
    this.emitOutput(false);
    return true;
  }

  private resample(): void {
    while (this.inputPosition + 1 < this.inputSamples.length) {
      const lowerIndex = Math.floor(this.inputPosition);
      const fraction = this.inputPosition - lowerIndex;
      const lower = this.inputSamples[lowerIndex] ?? 0;
      const upper = this.inputSamples[lowerIndex + 1] ?? lower;
      this.outputSamples.push(lower + (upper - lower) * fraction);
      this.inputPosition += this.resampleRatio;
    }
    const consumed = Math.floor(this.inputPosition);
    if (consumed > 0) {
      this.inputSamples.splice(0, consumed);
      this.inputPosition -= consumed;
    }
  }

  private emitOutput(flush: boolean): void {
    while (this.outputSamples.length >= this.outputFrameSize || (flush && this.outputSamples.length > 0)) {
      const length = flush ? Math.min(this.outputSamples.length, this.outputFrameSize) : this.outputFrameSize;
      const floats = this.outputSamples.splice(0, length);
      const pcm = new Int16Array(floats.length);
      let sumSquares = 0;
      for (let index = 0; index < floats.length; index += 1) {
        const sample = Math.max(-1, Math.min(1, floats[index] ?? 0));
        sumSquares += sample * sample;
        pcm[index] = sample < 0 ? sample * 0x8000 : sample * 0x7fff;
      }
      const rms = floats.length ? Math.sqrt(sumSquares / floats.length) : 0;
      this.port.postMessage({ type: "pcm", buffer: pcm.buffer, rms }, [pcm.buffer]);
    }
  }
}

registerProcessor("lili-pcm-capture", LiliPcmCaptureProcessor);

