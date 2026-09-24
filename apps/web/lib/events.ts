import { apiFetch } from "./api";

export type StreamEvent = { seq: number; type: string; payload?: Record<string, unknown>; occurred_at?: string };

export async function* streamRunEvents(runId: string, fromSeq: number, signal: AbortSignal): AsyncGenerator<StreamEvent> {
  const response = await apiFetch(`/v1/runs/${encodeURIComponent(runId)}/events?from_seq=${fromSeq}`, { signal });
  if (!response.ok || !response.body) throw new Error(`event stream ${response.status}`);
  const reader = response.body.getReader();
  const decoder = new TextDecoder();
  let buffer = "";
  try {
    while (true) {
      const { value, done } = await reader.read();
      if (done) return;
      buffer += decoder.decode(value, { stream: true });
      buffer = buffer.replace(/\r\n/g, "\n");
      let boundary: number;
      while ((boundary = buffer.indexOf("\n\n")) !== -1) {
        const frame = buffer.slice(0, boundary);
        buffer = buffer.slice(boundary + 2);
        const data = frame.split("\n").filter((line) => line.startsWith("data:")).map((line) => line.slice(5).trimStart()).join("\n");
        if (!data) continue;
        const event = JSON.parse(data) as StreamEvent;
        if (Number.isInteger(event.seq) && event.seq > fromSeq) yield event;
      }
    }
  } finally {
    await reader.cancel().catch(() => {});
  }
}
