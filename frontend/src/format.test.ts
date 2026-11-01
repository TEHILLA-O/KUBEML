import { describe, expect, it } from "vitest";
import { formatLatency, formatLog, formatRate, formatTime, shortHash } from "./format";

describe("formatters", () => {
  it("does not invent a latency when no samples exist", () => {
    expect(formatLatency(null)).toBe("no samples");
    expect(formatRate(undefined)).toBe("no samples");
  });

  it("formats measured latency and rates", () => {
    expect(formatLatency(18.2)).toBe("18 ms");
    expect(formatRate(0.052)).toBe("5.2%");
  });

  it("keeps an empty digest visible as unavailable", () => {
    expect(shortHash("")).toBe("unavailable");
    expect(shortHash("abcdef1234567890")).toBe("abcdef123456");
  });

  it("shows a local time and leaves an unparseable stamp unchanged", () => {
    expect(formatTime("")).toBe("time unavailable");
    expect(formatTime("not-a-date")).toBe("not-a-date");
    expect(formatTime("2026-11-01T16:33:53.541636+00:00")).not.toContain("+00:00");
  });

  it("turns a stored split dict into labeled rows", () => {
    expect(formatLog("Rows 4000 split {'train': 2800, 'validation': 600}")).toContain("Split");
    expect(formatLog("Rows 4000 split {'train': 2800, 'validation': 600}")).toContain("train: 2800");
  });
});
