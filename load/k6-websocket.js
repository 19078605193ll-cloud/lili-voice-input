import http from "k6/http";
import ws from "k6/ws";
import { check, sleep } from "k6";
import { Counter, Trend } from "k6/metrics";

const baseHttp = __ENV.BASE_HTTP || "http://127.0.0.1:9100";
const baseWs = __ENV.BASE_WS || "ws://127.0.0.1:9100";
const fixedDurationSeconds = Number(__ENV.AUDIO_SECONDS || 10);
const durationDistribution = parseDurationDistribution(__ENV.AUDIO_DURATION_DISTRIBUTION || "");
const maxSessionDurationSeconds = durationDistribution.length > 0
  ? Math.max(...durationDistribution.map((entry) => entry.max))
  : fixedDurationSeconds;
const startJitterSeconds = Number(__ENV.START_JITTER_SECONDS || 0);
const frameIntervalMs = 100;
const frameBytes = (16000 * frameIntervalMs * 2) / 1000;
const syntheticFrame = makePcmFrame(frameIntervalMs);
const audioFixture = __ENV.AUDIO_FILE ? new Uint8Array(open(__ENV.AUDIO_FILE, "b")) : null;
const fixtureFrameCount = audioFixture ? Math.floor(audioFixture.byteLength / frameBytes) : 0;
const vus = Number(__ENV.VUS || 10);
const oneShot = (__ENV.ONE_SHOT || "false").toLowerCase() === "true";
let cachedToken;

if (audioFixture && fixtureFrameCount === 0) {
  throw new Error(`AUDIO_FILE must contain at least ${frameBytes} bytes of PCM16 audio`);
}

const thresholds = {
  checks: ["rate>0.995"],
  voice_stop_to_final_ms: ["p(95)<10000", "p(99)<20000", "max<20000"],
  voice_admission_wait_ms: ["p(95)<500"],
  voice_asr_queue_wait_ms: ["p(95)<2000"],
  voice_server_errors: ["count==0"],
  voice_degraded_finals: ["count==0"],
};

export const options = oneShot ? {
  scenarios: {
    one_shot: {
      executor: "per-vu-iterations",
      vus,
      iterations: 1,
      maxDuration: `${fixedDurationSeconds + 60}s`,
    },
  },
  thresholds,
} : {
  scenarios: {
    sustained: {
      executor: "constant-vus",
      vus,
      duration: __ENV.DURATION || "1m",
      gracefulStop: `${maxSessionDurationSeconds + 60}s`,
    },
  },
  thresholds,
};

const stopToFinal = new Trend("voice_stop_to_final_ms");
const admissionWait = new Trend("voice_admission_wait_ms");
const asrQueueWait = new Trend("voice_asr_queue_wait_ms");
const asrLatency = new Trend("voice_asr_latency_ms");
const polishLatency = new Trend("voice_polish_latency_ms");
const serverTotalLatency = new Trend("voice_server_total_latency_ms");
const finalTextChars = new Trend("voice_final_text_chars");
const readyEvents = new Counter("voice_ready_events");
const nonEmptyFinals = new Counter("voice_non_empty_finals");
const serverErrors = new Counter("voice_server_errors");
const capacityErrors = new Counter("voice_capacity_errors");
const queueTimeouts = new Counter("voice_queue_timeouts");
const degradedFinals = new Counter("voice_degraded_finals");
const requestedAudioSeconds = new Counter("voice_requested_audio_seconds");

export default function () {
  const selectedDuration = oneShot
    ? { seconds: fixedDurationSeconds, bucket: "fixed" }
    : chooseDuration(durationDistribution, fixedDurationSeconds);
  const sessionDurationSeconds = selectedDuration.seconds;
  if (__ITER === 0 && startJitterSeconds > 0) {
    sleep(Math.random() * startJitterSeconds);
  }
  requestedAudioSeconds.add(sessionDurationSeconds, { bucket: selectedDuration.bucket });
  const token = obtainToken();
  let finalReceived = false;
  const response = ws.connect(
    `${baseWs}/v1/transcriptions/stream`,
    { headers: { Origin: __ENV.ORIGIN || "http://localhost:5173" } },
    (socket) => {
      let ready = false;
      let sentFrames = 0;
      let committedAt = 0;
      socket.on("open", () => {
        socket.send(
          JSON.stringify({
            type: "start",
            protocol_version: "1",
            format: "pcm16",
            sample_rate: 16000,
            language: "zh",
            auth_token: token,
          }),
        );
      });
      socket.on("message", (raw) => {
        if (typeof raw !== "string") return;
        const event = JSON.parse(raw);
        if (event.type === "ready" && !ready) {
          ready = true;
          readyEvents.add(1);
          socket.setInterval(() => {
            if (sentFrames < (sessionDurationSeconds * 1000) / frameIntervalMs) {
              socket.sendBinary(frameFor(sentFrames));
              sentFrames += 1;
            } else if (!committedAt) {
              committedAt = Date.now();
              socket.send(JSON.stringify({ type: "commit" }));
            }
          }, frameIntervalMs);
        }
        if (event.type === "final") {
          finalReceived = Boolean(event.text);
          if (finalReceived) {
            nonEmptyFinals.add(1);
            finalTextChars.add(event.text.length);
          }
          stopToFinal.add(Date.now() - committedAt);
          admissionWait.add(event.admission_wait_ms || 0);
          asrQueueWait.add(event.asr_queue_wait_ms || 0);
          asrLatency.add(event.latency_ms || 0);
          polishLatency.add(event.polish_latency_ms || 0);
          serverTotalLatency.add(event.total_latency_ms || 0);
          if (event.degraded) {
            degradedFinals.add(1, { stage: event.degraded_stage || "unknown" });
          }
          socket.close();
        }
        if (event.type === "error") {
          serverErrors.add(1, { code: event.code || "UNKNOWN" });
          if (event.code === "CAPACITY_REACHED") capacityErrors.add(1);
          if (event.code === "QUEUE_TIMEOUT") queueTimeouts.add(1);
          socket.close();
        }
      });
      socket.setTimeout(() => socket.close(), (sessionDurationSeconds + 30) * 1000);
    },
  );
  check(response, { "websocket upgraded": (result) => result && result.status === 101 });
  check(finalReceived, { "non-empty final received": (value) => value === true });
  sleep(Math.random());
}

function parseDurationDistribution(spec) {
  if (!spec.trim()) return [];
  return spec.split(",").map((rawEntry) => {
    const entry = rawEntry.trim();
    const match = /^(\d+)-(\d+):(\d+(?:\.\d+)?)$/.exec(entry);
    if (!match) {
      throw new Error(`Invalid AUDIO_DURATION_DISTRIBUTION entry: ${entry}`);
    }
    const min = Number(match[1]);
    const max = Number(match[2]);
    const weight = Number(match[3]);
    if (min <= 0 || max < min || weight <= 0) {
      throw new Error(`Invalid AUDIO_DURATION_DISTRIBUTION range: ${entry}`);
    }
    return { min, max, weight, bucket: `${min}-${max}s` };
  });
}

function chooseDuration(distribution, fallbackSeconds) {
  if (distribution.length === 0) {
    return { seconds: fallbackSeconds, bucket: "fixed" };
  }
  const totalWeight = distribution.reduce((total, entry) => total + entry.weight, 0);
  let selectedWeight = Math.random() * totalWeight;
  for (const entry of distribution) {
    selectedWeight -= entry.weight;
    if (selectedWeight <= 0) {
      return {
        seconds: Math.floor(entry.min + Math.random() * (entry.max - entry.min + 1)),
        bucket: entry.bucket,
      };
    }
  }
  const last = distribution[distribution.length - 1];
  return { seconds: last.max, bucket: last.bucket };
}

function obtainToken() {
  if (__ENV.SERVICE_TOKEN) return __ENV.SERVICE_TOKEN;
  if (cachedToken !== undefined) return cachedToken;
  const response = http.post(
    `${baseHttp}/v1/anonymous-tokens`,
    JSON.stringify({ client_id: `k6-load-${__VU}-${__ITER}-00000000` }),
    { headers: { "Content-Type": "application/json", Origin: __ENV.ORIGIN || "http://localhost:5173" } },
  );
  if (response.status === 404) {
    cachedToken = "";
    return cachedToken;
  }
  check(response, { "anonymous token issued": (result) => result.status === 200 });
  cachedToken = response.status === 200 ? response.json("token") : "";
  return cachedToken;
}

function makePcmFrame(milliseconds) {
  const samples = (16000 * milliseconds) / 1000;
  const pcm = new Int16Array(samples);
  for (let index = 0; index < samples; index += 1) {
    pcm[index] = Math.round(Math.sin((2 * Math.PI * 440 * index) / 16000) * 5000);
  }
  return pcm;
}

function frameFor(frameNumber) {
  if (!audioFixture) return syntheticFrame.buffer;
  const fixtureIndex = frameNumber % fixtureFrameCount;
  const start = fixtureIndex * frameBytes;
  return audioFixture.slice(start, start + frameBytes).buffer;
}
