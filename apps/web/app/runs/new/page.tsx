"use client";

import { useEffect, useRef, useState } from "react";
import { useRouter } from "next/navigation";
import Link from "next/link";
import { apiFetch } from "../../../lib/api";
import styles from "./page.module.css";

type Me = { lab_id: string; principal: string; scopes: string[] };
type Skill = { id?: string; name?: string; approved?: boolean };

async function json(response: Response) {
  if (!response.ok) throw new Error(response.status === 401 ? "กรุณาเข้าสู่ระบบก่อน" : `ไม่สามารถเชื่อมต่อได้ (${response.status})`);
  return response.json();
}

export default function NewRunPage() {
  const router = useRouter();
  const [me, setMe] = useState<Me | null>(null);
  const [skills, setSkills] = useState<Skill[]>([]);
  const [skill, setSkill] = useState("");
  const [goal, setGoal] = useState("");
  const [files, setFiles] = useState<File[]>([]);
  const [thb, setThb] = useState("100");
  const [minutes, setMinutes] = useState("120");
  const [status, setStatus] = useState<"loading" | "ready" | "submitting" | "error">("loading");
  const [error, setError] = useState("");
  const inFlight = useRef(false);
  const key = useRef<string | null>(null);

  useEffect(() => {
    const controller = new AbortController();
    async function load() {
      try {
        const identity = await json(await apiFetch("/v1/me", { signal: controller.signal }));
        const available = await json(await apiFetch(`/v1/labs/${encodeURIComponent(identity.lab_id)}/skills`, { signal: controller.signal }));
        if (controller.signal.aborted) return;
        setMe(identity);
        const approved = (Array.isArray(available) ? available : available.items || []).filter((item: Skill) => item.approved !== false && Boolean(item.id || item.name));
        setSkills(approved);
        setSkill(approved[0]?.id || approved[0]?.name || "");
        setStatus("ready");
      } catch (cause) {
        if (controller.signal.aborted) return;
        setError(cause instanceof Error ? cause.message : "โหลดข้อมูลไม่ได้");
        setStatus("error");
      }
    }
    void load();
    return () => controller.abort();
  }, []);

  async function submit(event: React.FormEvent<HTMLFormElement>) {
    event.preventDefault();
    if (!me || inFlight.current) return;
    const budget = Number(thb);
    const maxMinutes = Number(minutes);
    if (!goal.trim() || !skill || !Number.isFinite(budget) || budget < 0 || !Number.isInteger(maxMinutes) || maxMinutes < 1) {
      setError("กรุณากรอกโจทย์ เลือก skill pack และกำหนดงบกับเวลาให้ถูกต้อง");
      return;
    }
    inFlight.current = true;
    setStatus("submitting");
    setError("");
    key.current ||= crypto.randomUUID();
    try {
      const inputs: string[] = [];
      for (const file of files) {
        const content = Array.from(new Uint8Array(await file.arrayBuffer()), (byte) => String.fromCharCode(byte)).join("");
        const uploaded = await json(await apiFetch(`/v1/labs/${encodeURIComponent(me.lab_id)}/inputs`, {
          method: "POST",
          headers: { "content-type": "application/json" },
          body: JSON.stringify({ name: file.name, media_type: file.type || "application/octet-stream", data_base64: btoa(content) }),
        }));
        const artifactId = uploaded.artifact_id || uploaded.input_id || uploaded.id;
        if (typeof artifactId !== "string" || !artifactId) throw new Error("API ไม่ส่ง artifact ID ของไฟล์");
        inputs.push(artifactId);
      }
      const run = await json(await apiFetch(`/v1/labs/${encodeURIComponent(me.lab_id)}/runs`, {
        method: "POST",
        headers: { "content-type": "application/json", "Idempotency-Key": key.current },
        body: JSON.stringify({ goal: goal.trim(), inputs, skill_packs: [skill], budget: { thb: budget, max_minutes: maxMinutes } }),
      }));
      router.push(`/runs/${encodeURIComponent(run.run_id || run.id)}`);
    } catch (cause) {
      setError(cause instanceof Error ? cause.message : "ส่งงานไม่ได้ กรุณาลองใหม่");
      setStatus("ready");
    } finally {
      inFlight.current = false;
    }
  }

  return <main className={styles.main}>
    <div className={styles.eyebrow}>NEW RESEARCH RUN</div>
    <header className={styles.header}>
      <div><h1>เริ่มงานวิจัย</h1><p>บอกโจทย์ เลือกชุดทักษะที่อนุมัติแล้ว และกำหนดเพดานการใช้ทรัพยากร</p></div>
      <nav className={styles.links} aria-label="พื้นที่ทำงาน"><Link href="/">กลับหน้าหลัก</Link>{me?.scopes.includes("lab:admin") && <Link href={`/labs/${encodeURIComponent(me.lab_id)}`}>จัดการ Lab</Link>}</nav>
    </header>
    {status === "loading" && <p role="status">กำลังตรวจสิทธิ์และโหลด skill pack…</p>}
    {status === "error" && <p role="alert" className={styles.error}>{error} <button type="button" onClick={() => location.reload()}>ลองใหม่</button></p>}
    {(status === "ready" || status === "submitting") && <form onSubmit={submit} className={styles.form}>
      <div className={styles.primary}>
        <label htmlFor="goal">โจทย์วิจัย</label>
        <textarea id="goal" required rows={7} value={goal} onChange={(event) => setGoal(event.target.value)} placeholder="เช่น สรุปหลักฐานและข้อจำกัดของหัวข้อวิจัยที่สนใจ" />
        <label htmlFor="files">ไฟล์ตั้งต้น <span>(ถ้ามี)</span></label>
        <input id="files" type="file" multiple onChange={(event) => setFiles(Array.from(event.target.files || []))} />
        {files.length > 0 && <p className={styles.help}>เลือกแล้ว {files.length} ไฟล์ · ไฟล์จะส่งก่อนสร้าง Run</p>}
      </div>
      <aside className={styles.settings}>
        <h2>ขอบเขตการทำงาน</h2>
        <label htmlFor="skill">ชุดทักษะที่อนุมัติแล้ว</label>
        <select id="skill" required value={skill} onChange={(event) => setSkill(event.target.value)}>
          {skills.length === 0 && <option value="">ยังไม่มี skill pack ที่ใช้ได้</option>}
          {skills.map((item) => <option value={item.id || item.name} key={item.id || item.name}>{item.name || item.id}</option>)}
        </select>
        <div className={styles.two}>
          <div><label htmlFor="budget">งบสูงสุด (THB)</label><input id="budget" type="number" min="0" step="0.01" required value={thb} onChange={(event) => setThb(event.target.value)} /></div>
          <div><label htmlFor="minutes">เวลาสูงสุด (นาที)</label><input id="minutes" type="number" min="1" step="1" required value={minutes} onChange={(event) => setMinutes(event.target.value)} /></div>
        </div>
        <p className={styles.help}>Lab: {me?.lab_id} · การอนุมัติและงบจริงบังคับใช้ที่ Control Plane</p>
        {error && <p role="alert" className={styles.error}>{error}</p>}
        <button type="submit" disabled={status === "submitting" || !skills.length}>{status === "submitting" ? "กำลังส่งงาน…" : "เริ่ม Run"}</button>
      </aside>
    </form>}
  </main>;
}
