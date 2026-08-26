import { VoiceInputClient } from "@lili-voice-input/browser";

const input = document.querySelector<HTMLTextAreaElement>("#input")!;
const client = new VoiceInputClient({
  wsUrl: "ws://127.0.0.1:9100/v1/transcriptions/stream",
  fallbackUrl: "http://127.0.0.1:9100/v1/transcriptions",
  workletUrl: "http://127.0.0.1:9100/sdk/pcm-worklet.js",
});

client.on("final", ({ text }) => {
  input.setRangeText(text, input.selectionStart, input.selectionEnd, "end");
  input.dispatchEvent(new Event("input", { bubbles: true }));
});

document.querySelector("#start")?.addEventListener("click", () => void client.start());
document.querySelector("#stop")?.addEventListener("click", () => void client.stop());

