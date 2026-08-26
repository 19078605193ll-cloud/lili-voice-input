import { useEffect, useRef, useState } from "react";
import { VoiceInputClient, type VoiceInputState } from "@lili-voice-input/browser";

export function VoiceTextarea() {
  const [value, setValue] = useState("");
  const [state, setState] = useState<VoiceInputState>("idle");
  const clientRef = useRef<VoiceInputClient | null>(null);

  useEffect(() => {
    const client = new VoiceInputClient({
      wsUrl: "ws://127.0.0.1:9100/v1/transcriptions/stream",
      fallbackUrl: "http://127.0.0.1:9100/v1/transcriptions",
      workletUrl: "http://127.0.0.1:9100/sdk/pcm-worklet.js",
    });
    client.on("statechange", ({ state: next }) => setState(next));
    client.on("final", ({ text }) => setValue((current) => current + text));
    clientRef.current = client;
    return () => { void client.destroy(); };
  }, []);

  return <>
    <textarea value={value} onChange={(event) => setValue(event.target.value)} />
    <button onClick={() => clientRef.current?.start()}>开始录音</button>
    <button disabled={state !== "recording"} onClick={() => clientRef.current?.stop()}>停止</button>
  </>;
}

