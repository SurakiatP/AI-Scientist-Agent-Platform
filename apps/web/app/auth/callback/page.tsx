"use client";

import { useEffect, useRef, useState } from "react";
import { useRouter } from "next/navigation";
import { finishLogin } from "../../../lib/auth";

export default function CallbackPage() {
  const router = useRouter();
  const [error, setError] = useState("");
  const started = useRef(false);
  useEffect(() => {
    if (started.current) return;
    started.current = true;
    const query = new URLSearchParams(location.search);
    const code = query.get("code");
    const state = query.get("state");
    if (!code || !state) { setError("OIDC callback ไม่มี code/state"); return; }
    void finishLogin(code, state).then((destination) => router.replace(destination)).catch((cause) => setError(String(cause)));
  }, [router]);
  return <main style={{ maxWidth: 700, margin: "80px auto", padding: 24 }}><h1>กำลังเข้าสู่ระบบ</h1>{error ? <p role="alert">{error}</p> : <p role="status">กำลังตรวจสอบสิทธิ์…</p>}</main>;
}
