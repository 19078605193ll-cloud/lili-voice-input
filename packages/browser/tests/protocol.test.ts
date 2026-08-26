import { describe, expect, it } from "vitest";

import { parseServerEvent, PROTOCOL_VERSION, SAMPLE_RATE } from "../src/protocol";

describe("protocol", () => {
  it("exposes the v1 PCM contract", () => {
    expect(PROTOCOL_VERSION).toBe("1");
    expect(SAMPLE_RATE).toBe(16_000);
  });

  it("parses supported server events and rejects unrelated JSON", () => {
    expect(parseServerEvent('{"type":"ready","protocol_version":"1"}')?.type).toBe("ready");
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
