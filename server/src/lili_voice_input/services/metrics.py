from __future__ import annotations

from prometheus_client import Counter, Gauge, Histogram

ACTIVE_SESSIONS = Gauge("voice_active_sessions", "Active admitted voice sessions", ["transport"])
ADMISSION_QUEUE_DEPTH = Gauge("voice_admission_queue_depth", "Sessions waiting for admission")
REDIS_ADMISSION_QUEUE_DEPTH = Gauge(
    "voice_redis_admission_queue_depth", "Sessions waiting in the cross-instance admission queue"
)
ADMISSION_WAIT = Histogram("voice_admission_wait_seconds", "Admission queue wait")
CAPACITY_REJECTIONS = Counter("voice_capacity_rejections_total", "Rejected sessions", ["reason", "transport"])
SESSION_OUTCOMES = Counter("voice_sessions_total", "Voice session outcomes", ["outcome", "transport"])
WS_DISCONNECTS = Counter("voice_ws_disconnects_total", "WebSocket disconnects", ["reason"])

ASR_INFLIGHT = Gauge("voice_asr_inflight", "ASR requests in flight")
ASR_QUEUE_DEPTH = Gauge("voice_asr_queue_depth", "ASR segment queue depth")
ASR_QUEUE_WAIT = Histogram("voice_asr_queue_wait_seconds", "ASR queue wait")
ASR_REQUESTS = Counter("voice_asr_requests_total", "ASR requests", ["status"])
ASR_LATENCY = Histogram("voice_asr_latency_seconds", "ASR provider latency")
ASR_RETRIES = Counter("voice_asr_retries_total", "ASR retries", ["reason"])

POLISH_INFLIGHT = Gauge("voice_polish_inflight", "Polish requests in flight")
POLISH_QUEUE_DEPTH = Gauge("voice_polish_queue_depth", "Polish queue depth")
POLISH_QUEUE_WAIT = Histogram("voice_polish_queue_wait_seconds", "Polish queue wait")
POLISH_REQUESTS = Counter("voice_polish_requests_total", "Polish outcomes", ["status"])
POLISH_LATENCY = Histogram("voice_polish_latency_seconds", "Polish provider latency")

FFMPEG_INFLIGHT = Gauge("voice_ffmpeg_inflight", "FFmpeg processes in flight")
FFMPEG_QUEUE_DEPTH = Gauge("voice_ffmpeg_queue_depth", "FFmpeg queue depth")
FFMPEG_QUEUE_WAIT = Histogram("voice_ffmpeg_queue_wait_seconds", "FFmpeg queue wait")
FFMPEG_REQUESTS = Counter("voice_ffmpeg_requests_total", "FFmpeg outcomes", ["status"])
FFMPEG_LATENCY = Histogram("voice_ffmpeg_latency_seconds", "FFmpeg conversion latency")

FINAL_LATENCY = Histogram("voice_final_latency_seconds", "Commit-to-final latency", ["transport"])
DEGRADED_RESULTS = Counter("voice_degraded_results_total", "Degraded final results", ["stage"])
HTTP_FALLBACKS = Counter("voice_http_fallback_total", "HTTP fallback attempts", ["status"])
POLISH_TOKENS = Counter("voice_polish_tokens_total", "Polishing model token usage", ["kind"])
REDIS_READY = Gauge("voice_redis_ready", "Whether Redis coordination is currently available")
LEASE_RELEASE_FAILURES = Counter("voice_lease_release_failures_total", "Failed Redis lease releases", ["stage"])
