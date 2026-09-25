"use client";

import { use, useCallback, useEffect, useState } from "react";
import Link from "next/link";
import { apiFetch } from "../../../lib/api";
import styles from "./page.module.css";

type Me = { lab_id: string; principal: string; scopes: string[] };
type Member = { subject: string; role: "owner" | "researcher" | "viewer" };
type Key = { id: string; name?: string };
type Peer = { id: string; name?: string };
type Budget = { budget_thb: string | number | null };

async function read(response: Response) {
  if (!response.ok) throw new Error(`${response.status}: ${response.status === 403 ? "ไม่มีสิทธิ์จัดการ Lab" : "ดำเนินการไม่ได้"}`);
  return response.status === 204 ? null : response.json();
}

export default function LabPage({ params }: { params: Promise<{ id: string }> }) {
  const { id } = use(params);
  const lab = encodeURIComponent(id);
  const [me, setMe] = useState<Me | null>(null);
  const [members, setMembers] = useState<Member[]>([]);
  const [keys, setKeys] = useState<Key[]>([]);
  const [peers, setPeers] = useState<Peer[]>([]);
  const [budget, setBudget] = useState<Budget>({ budget_thb: null });
  const [usage, setUsage] = useState<unknown>(null);
  const [subject, setSubject] = useState("");
  const [role, setRole] = useState<Member["role"]>("researcher");
  const [budgetText, setBudgetText] = useState("");
  const [keyName, setKeyName] = useState("");
  const [keyOnce, setKeyOnce] = useState("");
  const [peerName, setPeerName] = useState("");
  const [peerOnce, setPeerOnce] = useState("");
  const [status, setStatus] = useState<"loading" | "ready" | "denied">("loading");
  const [error, setError] = useState("");

  const refresh = useCallback(async () => {
    setPeerOnce("");
    const identity: Me = await read(await apiFetch("/v1/me"));
    setMe(identity);
    if (identity.lab_id !== id || !identity.scopes.includes("lab:admin")) { setStatus("denied"); return; }
    const base = `/v1/labs/${lab}`;
    const [nextMembers, nextBudget, nextKeys, nextPeers, nextUsage] = await Promise.all([
      apiFetch(`${base}/members`).then(read), apiFetch(`${base}/budget`).then(read),
      apiFetch(`${base}/api-keys`).then(read), apiFetch(`${base}/peers`).then(read),
      apiFetch(`${base}/usage?period=monthly`).then(read),
    ]);
    setMembers(nextMembers.members);
    setBudget(nextBudget);
    setBudgetText(nextBudget.budget_thb === null ? "" : String(nextBudget.budget_thb));
    setKeys(Array.isArray(nextKeys) ? nextKeys : nextKeys.items || []);
    setPeers(Array.isArray(nextPeers) ? nextPeers : nextPeers.items || []);
    setUsage(nextUsage);
    setStatus("ready");
  }, [id, lab]);

  useEffect(() => { void refresh().catch((cause) => { setError(String(cause)); setStatus("denied"); }); }, [refresh]);

  async function mutate(path: string, method: string, body?: object) {
    setError("");
    try {
      const result = await read(await apiFetch(`/v1/labs/${lab}/${path}`, {
        method, headers: body ? { "content-type": "application/json" } : undefined,
        body: body ? JSON.stringify(body) : undefined,
      }));
      await refresh();
      if (path === "api-keys" && method === "POST" && typeof (result?.secret || result?.api_key) !== "string") {
        setError("สร้าง key แล้ว แต่ API ไม่ส่ง secret; โปรดตรวจสอบ backend ก่อนใช้งาน อย่าสร้างซ้ำ");
        return null;
      }
      if (path === "peers" && method === "POST" && (typeof result?.secret !== "string" || !result.secret)) {
        setError("สร้าง peer แล้ว แต่ API ไม่ส่ง secret; โปรดตรวจสอบ backend ก่อนใช้งาน อย่าสร้างซ้ำ");
        return null;
      }
      return result;
    } catch (cause) { setError(cause instanceof Error ? cause.message : "ดำเนินการไม่ได้"); return null; }
  }

  return <main className={styles.main}>
    <header className={styles.header}><div><p className={styles.eyebrow}>LAB ADMINISTRATION</p><h1>จัดการ Lab</h1><p>{id}</p></div><Link href="/">กลับหน้าหลัก</Link></header>
    {status === "loading" && <p role="status">กำลังตรวจสิทธิ์และโหลดข้อมูล Lab…</p>}
    {status === "denied" && <div className={styles.panel} role="alert"><h2>ไม่สามารถเปิดส่วนจัดการ Lab</h2><p>{error || "บัญชีนี้ไม่มีสิทธิ์ Lab admin หรือ Lab ไม่ตรงกับบัญชี"}</p></div>}
    {status === "ready" && <div className={styles.grid}>
      <section className={styles.panel}><h2>สมาชิก</h2><p className={styles.help}>เพิ่ม OIDC subject ที่มีอยู่แล้วเท่านั้น · ไม่มีการส่งคำเชิญหรือสร้างบัญชี</p>
        <ul className={styles.list}>{members.map((member) => <li key={member.subject}><span><strong>{member.subject}</strong><small>{member.role}</small></span><span className={styles.actions}><select aria-label={`บทบาทของ ${member.subject}`} value={member.role} onChange={(event) => void mutate(`members/${encodeURIComponent(member.subject)}`, "PATCH", { role: event.target.value })}><option value="owner">owner</option><option value="researcher">researcher</option><option value="viewer">viewer</option></select><button type="button" onClick={() => void mutate(`members/${encodeURIComponent(member.subject)}`, "DELETE")}>นำออก</button></span></li>)}</ul>
        <form onSubmit={(event) => { event.preventDefault(); void mutate("members", "POST", { subject: subject.trim(), role }).then((value) => { if (value) setSubject(""); }); }} className={styles.formRow}><label>OIDC subject<input required value={subject} onChange={(event) => setSubject(event.target.value)} /></label><label>บทบาท<select value={role} onChange={(event) => setRole(event.target.value as Member["role"])}><option value="researcher">researcher</option><option value="viewer">viewer</option><option value="owner">owner</option></select></label><button type="submit">เพิ่มสมาชิก</button></form>
      </section>
      <section className={styles.panel}><h2>งบ Lab</h2><p className={styles.help}>{budget.budget_thb === null ? "ยังไม่ได้ตั้งงบ" : `เพดานปัจจุบัน ${budget.budget_thb} THB`}</p><form onSubmit={(event) => { event.preventDefault(); const value = budgetText.trim() === "" ? null : budgetText.trim(); if (value !== null && !/^(0|[1-9]\d*)(\.\d+)?$/.test(value)) { setError("งบต้องเป็นเลขฐานสิบไม่ติดลบ"); return; } void mutate("budget", "PUT", { budget_thb: value }); }}><label>งบ (THB) · เว้นว่างเพื่อล้างค่า<input type="text" inputMode="decimal" value={budgetText} onChange={(event) => setBudgetText(event.target.value)} /></label><button type="submit">บันทึกงบ</button></form><h3>การใช้งานเดือนนี้</h3><pre>{usage === null ? "ยังไม่มีข้อมูล" : JSON.stringify(usage, null, 2)}</pre></section>
      <section className={styles.panel}><h2>API keys</h2><ul className={styles.list}>{keys.map((key) => <li key={key.id}><span>{key.name || key.id}</span><button type="button" onClick={() => void mutate(`api-keys?key_id=${encodeURIComponent(key.id)}`, "DELETE")}>ลบ</button></li>)}</ul><form onSubmit={(event) => { event.preventDefault(); setKeyOnce(""); void mutate("api-keys", "POST", { name: keyName.trim() }).then((value) => { if (value) { setKeyName(""); setKeyOnce(value.secret || value.api_key || ""); } }); }}><label>ชื่อ key<input required value={keyName} onChange={(event) => setKeyName(event.target.value)} /></label><button type="submit">สร้าง key</button></form>{keyOnce && <p role="status" className={styles.once}>แสดงครั้งเดียว: <code>{keyOnce}</code></p>}</section>
      <section className={styles.panel}><h2>Peers</h2><ul className={styles.list}>{peers.map((peer) => <li key={peer.id}>{peer.name || peer.id}</li>)}</ul><form onSubmit={(event) => { event.preventDefault(); setPeerOnce(""); void mutate("peers", "POST", { name: peerName.trim() }).then((value) => { if (value) { setPeerName(""); setPeerOnce(value.secret); } }); }}><label>ชื่อ peer<input required value={peerName} onChange={(event) => setPeerName(event.target.value)} /></label><button type="submit">เพิ่ม peer</button></form>{peerOnce && <p role="status" className={styles.once}>แสดงครั้งเดียว: <code>{peerOnce}</code></p>}</section>
    </div>}
    {error && status === "ready" && <p role="alert" className={styles.error}>{error}</p>}
    {me && <p className={styles.identity}>Signed in: {me.principal}</p>}
  </main>;
}
