# lili-voice-input server

FastAPI server package for the repository. The server transcribes audio segments, merges their text, and optionally makes one OpenAI-compatible plain-text polishing call. A polishing failure returns the merged ASR text instead of failing the transcription request.

See the root `README.md` for setup, protocol and deployment instructions.
