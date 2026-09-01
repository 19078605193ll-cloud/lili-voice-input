# @lili-voice-input/browser

UI-agnostic browser client for the self-hosted `lili-voice-input` service. See the repository root README for setup and integration examples.

The client understands admission `queued` events, requests short-lived anonymous tokens by default, starts audio capture only after `ready`, retries recoverable connection failures, and switches once to HTTP upload on sustained WebSocket backpressure. `EMPTY_AUDIO`, `CAPACITY_REACHED`, and `QUEUE_TIMEOUT` never trigger HTTP fallback.
