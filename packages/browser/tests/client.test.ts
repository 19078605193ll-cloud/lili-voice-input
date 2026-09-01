import { describe, expect, it, vi } from "vitest";

import { VoiceInputClient } from "../src/client";

describe("VoiceInputClient", () => {
  it("does not use HTTP fallback after an EMPTY_AUDIO response", async () => {
    const client = new VoiceInputClient({
      wsUrl: "ws://voice.example/v1/transcriptions/stream",
      fallbackUrl: "https://voice.example/v1/transcriptions",
    });
    const emptyAudio = Object.assign(new Error("没有检测到语音，或说话时间太短，请重试"), {
      code: "EMPTY_AUDIO",
      recoverable: false,
    });
    const commitSocket = vi.fn().mockRejectedValue(emptyAudio);
    const fallbackTranscription = vi.fn().mockRejectedValue(new Error("fallback should not run"));
    const close = vi.fn().mockResolvedValue(undefined);
    const internals = client as unknown as {
      capture: { stopTracks(): void; flush(): Promise<void>; close(): Promise<void> };
      commitSocket: typeof commitSocket;
      fallbackTranscription: typeof fallbackTranscription;
    };
    internals.capture = {
      stopTracks: vi.fn(),
      flush: vi.fn().mockResolvedValue(undefined),
      close,
    };
    internals.commitSocket = commitSocket;
    internals.fallbackTranscription = fallbackTranscription;
    client.state = "recording";
    const errors: Array<{ code: string; message: string; recoverable: boolean }> = [];
    client.on("error", (error) => errors.push(error));

    const result = await client.stop();

    expect(result).toBeNull();
    expect(fallbackTranscription).not.toHaveBeenCalled();
    expect(errors).toEqual([
      {
        code: "EMPTY_AUDIO",
        message: "没有检测到语音，或说话时间太短，请重试",
        recoverable: false,
        cause: emptyAudio,
      },
    ]);
    expect(client.state).toBe("error");
    expect(close).toHaveBeenCalled();
  });
});
