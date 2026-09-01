import type { FinalResult, QueuedEvent, ReadyEvent, VoiceInputError } from "./types";

export const PROTOCOL_VERSION = "1" as const;
export const SAMPLE_RATE = 16_000 as const;

export interface StartEvent {
  type: "start";
  protocol_version: typeof PROTOCOL_VERSION;
  format: "pcm16";
  sample_rate: typeof SAMPLE_RATE;
  language: string;
  auth_token?: string;
}

export interface ServerErrorEvent extends VoiceInputError {
  type: "error";
}

export type ServerEvent = ReadyEvent | QueuedEvent | Omit<FinalResult, "source"> | ServerErrorEvent;

export function parseServerEvent(raw: unknown): ServerEvent | null {
  if (typeof raw !== "string") return null;
  try {
    const value = JSON.parse(raw) as Record<string, unknown>;
    if (value.type === "ready" || value.type === "queued" || value.type === "final" || value.type === "error") {
      return value as unknown as ServerEvent;
    }
  } catch {
    return null;
  }
  return null;
}
