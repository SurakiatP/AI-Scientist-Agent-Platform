"use client";

import { use, useCallback, useEffect, useRef, useState } from "react";
import Link from "next/link";
import ReactMarkdown from "react-markdown";
import remarkGfm from "remark-gfm";
import remarkMath from "remark-math";
import rehypeKatex from "rehype-katex";
import katex from "katex";
import "katex/dist/katex.min.css";
import { apiFetch } from "../../../lib/api";
import { streamRunEvents } from "../../../lib/events";
import styles from "./page.module.css";

type Run = { id: string; lab_id?: string; state: string };
type Claim = { id: string; text: string; evidence: string[]; confidence: number };
type Manifest = { goal?: string; claims?: Claim[]; [key: string]: unknown };
type Artifact = { id: string; kind: string; url?: string; sha256?: string; metadata?: { name?: string; content_type?: string; provenance?: unknown } };
type Event = { seq: number; type: string; payload?: Record<string, unknown>; occurred_at?: string };

async function read(response: Response) {
  if (!response.ok) throw new Error(response.status === 403 ? "ไม่มีสิทธิ์ดู Run นี้" : `เชื่อมต่อไม่ได้ (${response.status})`);
  return response.json();
}

function texMath(source: string) {
  const expressions = source.matchAll(/\\\[([\s\S]*?)\\\]|\\\(([^\n]*?)\\\)|\$\$([\s\S]*?)\$\$|(?<!\$)\$([^\n$]+)\$(?!\$)/g);
  return [...expressions].flatMap((match) => {
    try {
      return [katex.renderToString(match[1] || match[2] || match[3] || match[4], {
        displayMode: Boolean(match[1] || match[3]), trust: false, throwOnError: true,
      })];
    } catch { return []; }
  });
}

export default function RunPage({ params }: { params: Promise<{ id: string }> }) {
  const { id } = use(params);
  const [run, setRun] = useState<Run | null>(null);
  const [events, setEvents] = useState<Event[]>([]);
  const [artifacts, setArtifacts] = useState<Artifact[]>([]);
  const [reportText, setReportText] = useState("");
  const [reportType, setReportType] = useState("");
  const [manifest, setManifest] = useState<Manifest | null>(null);
  const [scopes, setScopes] = useState<string[]>([]);
  const [connection, setConnection] = useState("กำลังเชื่อมต่อ");
  const [error, setError] = useState("");
  const [busy, setBusy] = useState(false);
  const lastSeq = useRef(0);
  const path = `/v1/runs/${encodeURIComponent(id)}`;

  const refresh = useCallback(async () => {
    const [nextRun, nextArtifacts] = await Promise.all([apiFetch(path).then(read), apiFetch(`${path}/artifacts`).then(read).catch((cause) => { setError(`Artifacts: ${String(cause)}`); return []; })]);
    setRun(nextRun);
    const listed: Artifact[] = Array.isArray(nextArtifacts) ? nextArtifacts : nextArtifacts.items || [];
    const report = listed.find((artifact) => artifact.kind === "report");
    const provenance = listed.find((artifact) => artifact.kind === "manifest");
    const details = Promise.all(listed.map((artifact) => apiFetch(`/v1/artifacts/${encodeURIComponent(artifact.id)}`).then(read).catch(() => artifact)));
    const reportBody = nextRun.state === "completed" && report
      ? apiFetch(`/v1/artifacts/${encodeURIComponent(report.id)}/content`).then(async (response) => response.ok ? { text: await response.text(), type: response.headers.get("content-type") || "text/markdown" } : null).catch(() => null)
      : Promise.resolve(null);
    const manifestBody = nextRun.state === "completed" && provenance
      ? apiFetch(`/v1/artifacts/${encodeURIComponent(provenance.id)}/content`).then(async (response) => response.ok ? await response.json() as Manifest : null).catch(() => null)
      : Promise.resolve(null);
    const [enriched, text, source] = await Promise.all([details, reportBody, manifestBody]);
    setArtifacts(enriched);
    setReportText(text?.text || "");
    setReportType(text?.type || "");
    setManifest(source);
  }, [path]);

  useEffect(() => {
    const controller = new AbortController();
    void Promise.all([apiFetch("/v1/me", { signal: controller.signal }).then(read), refresh()])
      .then(([me]) => setScopes(me.scopes || []))
      .catch((cause) => { if (!controller.signal.aborted) setError(String(cause)); });
    return () => controller.abort();
  }, [refresh]);

  useEffect(() => {
    const controller = new AbortController();
    async function connect() {
      while (!controller.signal.aborted) {
        try {
          setConnection("กำลังรับข้อมูลสด");
          for await (const item of streamRunEvents(id, lastSeq.current, controller.signal)) {
            if (item.seq <= lastSeq.current) continue;
            lastSeq.current = item.seq;
            setEvents((prior) => [...prior, item]);
            void refresh().catch((cause) => setError(String(cause)));
          }
          setConnection("กำลังเชื่อมต่อใหม่");
        } catch (cause) {
          if (controller.signal.aborted) return;
          setConnection("ขาดการเชื่อมต่อ · กำลังลองใหม่");
          setError(cause instanceof Error ? cause.message : "รับข้อมูลสดไม่ได้");
        }
        await new Promise((resolve) => setTimeout(resolve, 1500));
      }
    }
    void connect();
    return () => controller.abort();
  }, [path, refresh]);

  async function decide(decision: "approve" | "reject") {
    if (!approvalId || busy) return;
    setBusy(true);
    setError("");
    try {
      await read(await apiFetch(`${path}/approvals/${encodeURIComponent(approvalId)}`, { method: "POST", headers: { "content-type": "application/json" }, body: JSON.stringify({ decision }) }));
      await refresh();
    } catch (cause) { setError(cause instanceof Error ? cause.message : "ตัดสินใจไม่ได้"); }
    finally { setBusy(false); }
  }

  const lastCost = [...events].reverse().find((event) => event.type === "cost.updated");
  const approval = [...events].reverse().find((event) => event.type === "approval.required" && typeof event.payload?.approval_id === "string");
  const approvalId = approval?.payload?.approval_id as string | undefined;
  const completed = [...events].reverse().find((event) => event.type === "run.completed");
  const cost = lastCost && typeof lastCost.payload?.llm_cost_thb === "number" && typeof lastCost.payload?.compute_cost_thb === "number"
    ? lastCost.payload.llm_cost_thb + lastCost.payload.compute_cost_thb : null;
  const isTex = reportType.split(";")[0].trim() === "application/x-tex";
  return <main className={styles.main}>
    <header className={styles.header}><div><p className={styles.eyebrow}>RESEARCH RUN</p><h1>ติดตามงานวิจัย</h1><p className={styles.id}>{id}</p></div><Link href="/runs/new">เริ่ม Run ใหม่</Link></header>
    {error && <p role="alert" className={styles.error}>{error} <button type="button" onClick={() => void refresh()}>ลองโหลดใหม่</button></p>}
    {!run && <p role="status">กำลังโหลด Run…</p>}
    {run && <div className={styles.grid}>
      <div className={styles.primary}>
        <section className={styles.panel}><div className={styles.sectionHead}><h2>สถานะ</h2><span className={styles.state}>{run.state}</span></div><p>{manifest?.goal || (typeof completed?.payload?.summary === "string" ? completed.payload.summary : `Run ${run.id}`)}</p><div className={styles.metrics}><span>Live: {connection}</span><span>ค่าใช้จ่ายสะสม: {typeof cost === "number" ? `${cost} THB` : "ยังไม่มีข้อมูล"}</span></div></section>
        <section className={styles.panel}><h2>ลำดับขั้นตอนและเครื่องมือ</h2>{events.length === 0 ? <p className={styles.muted}>กำลังรอ event จาก Run</p> : <ol className={styles.timeline}>{events.map((event) => <li key={event.seq}><span className={styles.seq}>{String(event.seq).padStart(2, "0")}</span><div><strong>{event.payload?.step ? String(event.payload.step) : event.payload?.tool ? String(event.payload.tool) : event.type}</strong><p>{event.type}{event.type === "cost.updated" && typeof event.payload?.llm_cost_thb === "number" && typeof event.payload?.compute_cost_thb === "number" ? ` · ${event.payload.llm_cost_thb + event.payload.compute_cost_thb} THB` : ""}</p></div></li>)}</ol>}</section>
        {run.state === "awaiting_approval" && <section className={styles.approval}><h2>รอการอนุมัติ</h2><p>{typeof approval?.payload?.reason === "string" ? approval.payload.reason : "กำลังรอรายละเอียดการอนุมัติจาก event stream"}</p>{scopes.includes("runs:approve") && approvalId ? <div className={styles.actions}><button disabled={busy} onClick={() => void decide("approve")}>อนุมัติ</button><button disabled={busy} className={styles.reject} onClick={() => void decide("reject")}>ปฏิเสธ</button></div> : <p>{scopes.includes("runs:approve") ? "ยังไม่มี approval ID ที่ยืนยันจาก event stream" : "บัญชีนี้ไม่มีสิทธิ์ตัดสินใจ"}</p>}</section>}
        {reportText && <section className={styles.panel}><h2>รายงาน</h2><h3>{isTex ? "LaTeX" : "Markdown"}</h3>{isTex ? <><p className={styles.muted}>ตัวอย่าง source LaTeX (ยังไม่ได้ compile)</p><pre>{reportText}</pre>{texMath(reportText).map((html, index) => <div className={styles.mathPreview} key={index} dangerouslySetInnerHTML={{ __html: html }} />)}</> : <div className={styles.reportBody}><ReactMarkdown remarkPlugins={[remarkGfm, remarkMath]} rehypePlugins={[rehypeKatex]} components={{ table: ({ node: _node, ...props }) => <div className={styles.tableScroll}><table {...props} /></div> }}>{reportText}</ReactMarkdown></div>}</section>}
      </div>
      <aside className={styles.side}>
        <section className={styles.panel}><h2>Artifacts</h2>{artifacts.length === 0 ? <p className={styles.muted}>ยังไม่มี artifact</p> : <ul className={styles.list}>{artifacts.map((artifact) => <li key={artifact.id}><strong>{artifact.metadata?.name || `${artifact.kind} · ${artifact.id}`}</strong>{artifact.url && <a href={artifact.url} target="_blank" rel="noopener noreferrer">เปิด/ดาวน์โหลด</a>}{artifact.metadata?.content_type?.startsWith("image/") && artifact.url && <img src={artifact.url} alt={artifact.metadata?.name || "ภาพผลลัพธ์"} />}{artifact.sha256 && <small>SHA-256: {artifact.sha256}</small>}{Boolean(artifact.metadata?.provenance) && <details><summary>Provenance</summary><pre>{JSON.stringify(artifact.metadata?.provenance, null, 2)}</pre></details>}</li>)}</ul>}</section>
        <section className={styles.panel}><h2>Citations และ Provenance</h2>{!manifest?.claims?.length ? <p className={styles.muted}>ยังไม่มี citation ใน manifest</p> : <ul className={styles.list}>{manifest.claims.map((claim) => <li key={claim.id}><strong>{claim.text}</strong><span>ความเชื่อมั่น {claim.confidence}</span>{claim.evidence.map((source) => /^https?:\/\//.test(source) ? <a key={source} href={source} rel="noopener noreferrer" target="_blank">{source}</a> : <span key={source}>{source}</span>)}</li>)}</ul>}{manifest && <details><summary>ตรวจ manifest ทั้งหมด</summary><pre>{JSON.stringify(manifest, null, 2)}</pre></details>}</section>
      </aside>
    </div>}
  </main>;
}
