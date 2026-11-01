export function formatLatency(value: number | null | undefined): string {
  if (value == null || Number.isNaN(value)) return "no samples";
  if (value < 1000) return `${Math.round(value)} ms`;
  return `${(value / 1000).toFixed(2)} s`;
}

export function formatRate(value: number | null | undefined): string {
  if (value == null || Number.isNaN(value)) return "no samples";
  return `${(value * 100).toFixed(1)}%`;
}

export function shortHash(value: string | null | undefined): string {
  if (!value) return "unavailable";
  return value.slice(0, 12);
}

export function formatTime(value: string | null | undefined): string {
  if (!value) return "time unavailable";
  const date = new Date(value);
  if (Number.isNaN(date.getTime())) return value;
  return new Intl.DateTimeFormat(undefined, {
    month: "short",
    day: "numeric",
    hour: "2-digit",
    minute: "2-digit",
    second: "2-digit",
  }).format(date);
}

export function formatLog(log: string): string {
  return log.replace(/split \{([^}]*)\}/i, (_match, body: string) => {
    const parts = body.split(",").map((part) => part.trim().replace(/['"]/g, ""));
    return ["Split", ...parts.map((part) => `  ${part}`)].join("\n");
  });
}
