export type VoiceInputState =
  | "idle"
  | "requesting-permission"
  | "connecting"
  | "queued"
  | "recording"
  | "finalizing"
  | "completed"
  | "error";

export type PolishStatus = "applied" | "disabled" | "fallback";
export type PolishReason =
  | "empty_input"
  | "configuration_error"
  | "rate_limited"
  | "timeout"
  | "network_error"
  | "provider_error"
  | "invalid_output"
  | "empty_output"
  | "capacity_reached";
export type DegradedStage = "asr" | "polish";
export type FinalSource = "websocket" | "http-fallback";

export interface ReadyEvent {
  type: "ready";
  protocol_version: "1";
  session_id: string;
  sample_rate: 16000;
  max_duration_seconds: number;
  capabilities: {
    partial: false;
    http_fallback: boolean;
  };
}

export interface QueuedEvent {
  type: "queued";
  position: number;
  estimated_wait_ms: number;
  max_wait_ms: number;
}

export interface FinalResult {
  type: "final";
  text: string;
  polished: boolean;
  polish_status: PolishStatus;
  polish_reason: PolishReason | null;
  degraded: boolean;
  degraded_stage: DegradedStage | null;
  segment_count: number;
  failed_segment_count: number;
  latency_ms: number;
  polish_latency_ms: number;
  total_latency_ms: number;
  admission_wait_ms: number;
  asr_queue_wait_ms: number;
  source: FinalSource;
}

export interface VoiceInputError {
  code: string;
  message: string;
  recoverable: boolean;
  retry_after_ms?: number;
  cause?: unknown;
}

export interface VoiceInputEvents {
  statechange: { state: VoiceInputState };
  volume: { rms: number };
  ready: ReadyEvent;
  queued: QueuedEvent;
  final: FinalResult;
  error: VoiceInputError;
}

export interface VoiceInputClientOptions {
  wsUrl: string;
  fallbackUrl?: string;
  workletUrl?: string;
  maxDurationMs?: number;
  connectTimeoutMs?: number;
  finalTimeoutMs?: number;
  token?: string;
  anonymousTokenUrl?: string;
  clientIdStorageKey?: string;
  language?: string;
  mediaConstraints?: MediaTrackConstraints;
}
