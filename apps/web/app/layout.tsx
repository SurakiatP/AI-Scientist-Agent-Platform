import type { Metadata } from "next";
import "./globals.css";

export const metadata: Metadata = {
  title: "AI Scientist Agent Platform",
  description: "พื้นที่ทำงานวิจัยที่ติดตามขั้นตอน ตรวจหลักฐาน และควบคุมการอนุมัติได้",
};

export default function RootLayout({ children }: Readonly<{ children: React.ReactNode }>) {
  const demo = process.env.NODE_ENV === "development" && process.env.NEXT_PUBLIC_SCILAB_DEMO === "1";
  return <html lang="th"><body>{demo && <header role="banner" style={{ background: "#173c81", color: "white", padding: "7px 20px", textAlign: "center", fontSize: 13, fontWeight: 700 }}>โหมดทดลองในเครื่อง · ข้อมูลและผลลัพธ์เป็นตัวอย่าง ไม่ใช่ระบบจริง</header>}{children}</body></html>;
}
