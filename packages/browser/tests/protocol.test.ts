import { describe, expect, it } from "vitest";

import { parseServerEvent, PROTOCOL_VERSION, SAMPLE_RATE } from "../src/protocol";

describe("protocol", () => {
  it("exposes the v1 PCM contract", () => {
    expect(PROTOCOL_VERSION).toBe("1");
    expect(SAMPLE_RATE).toBe(16_000);
  });

  it("parses supported server events and rejects unrelated JSON", () => {
    expect(parseServerEvent('{"type":"ready","protocol_version":"1"}')?.type).toBe("ready");
    const queued = parseServerEvent(
      '{"type":"queued","position":3,"estimated_wait_ms":1200,"max_wait_ms":5000}',
    );
    expect(queued?.type).toBe("queued");
    if (queued?.type === "queued") {
      expect(queued.position).toBe(3);
      expect(queued.max_wait_ms).toBe(5000);
    }
    const final = parseServerEvent(
      '{"type":"final","text":"结果","polished":false,"polish_status":"fallback","polish_reason":"timeout","degraded":true,"degraded_stage":"polish"}',
    );
    expect(final?.type).toBe("final");
    if (final?.type === "final") {
      expect(final.polish_reason).toBe("timeout");
      expect(final.degraded_stage).toBe("polish");
      expect("polish_reason_codes" in final).toBe(false);
    }
    expect(parseServerEvent('{"type":"partial","text":"draft"}')).toBeNull();
    expect(parseServerEvent("not-json")).toBeNull();
  });
});
