import styles from "./page.module.css";
import Link from "next/link";

export default function Home() {
  return <main className={styles.page}>
    <nav className={styles.nav} aria-label="หลัก"><Link href="/" className={styles.brand}><span className={styles.mark}>✳</span><span>AI Scientist</span></Link><div className={styles.navLinks}><a href="#how-it-works">วิธีทำงาน</a><Link href="/runs/new" className={styles.navStart}>เริ่มงานวิจัย <span aria-hidden="true">↗</span></Link></div></nav>
    <section className={styles.hero}>
      <div className={styles.heroCopy}><p className={styles.kicker}><span className={styles.dot} /> RESEARCH WORKSPACE</p><h1>จากโจทย์วิจัย<br />สู่ผลลัพธ์ที่ตรวจสอบได้</h1><p className={styles.lead}>พื้นที่เดียวสำหรับส่งโจทย์ ติดตามการทำงานของ Agent อนุมัติจุดสำคัญ และกลับไปดูหลักฐานของคำตอบ</p><div className={styles.heroActions}><Link className={styles.primaryButton} href="/runs/new">เริ่มงานวิจัย <span aria-hidden="true">→</span></Link><a className={styles.textButton} href="#how-it-works">ดูวิธีทำงาน <span aria-hidden="true">↓</span></a></div><p className={styles.heroNote}>สำหรับทีมวิจัยที่ต้องการเห็นที่มา ไม่ใช่แค่คำตอบสุดท้าย</p></div>
      <div className={styles.workbench} aria-label="ภาพประกอบลำดับงานวิจัย">
        <div className={styles.workbenchTop}><span>ภาพรวมการทำงาน</span><span>01 / 03</span></div>
        <div className={styles.flowCard}><span className={styles.flowIndex}>01</span><div><strong>กำหนดโจทย์</strong><p>เป้าหมาย · ไฟล์ตั้งต้น · ชุดทักษะ · งบ</p></div><span className={styles.flowMark}>↗</span></div>
        <div className={styles.flowLine} />
        <div className={styles.flowCard}><span className={styles.flowIndex}>02</span><div><strong>ติดตามและตัดสินใจ</strong><p>ขั้นตอน · Tool calls · ค่าใช้จ่าย · Approval</p></div><span className={styles.flowMark}>↗</span></div>
        <div className={styles.flowLine} />
        <div className={styles.flowCard}><span className={styles.flowIndex}>03</span><div><strong>ตรวจผลลัพธ์</strong><p>รายงาน · Artifacts · Citations · Provenance</p></div><span className={styles.flowMark}>✓</span></div>
        <p className={styles.illustrationNote}>แผนภาพอธิบายขั้นตอน ไม่ใช่ผลการวิจัยจริง</p>
      </div>
    </section>
    <section className={styles.principle}><p>คำตอบที่ดีต้องตรวจย้อนกลับได้</p><h2>เห็นทุกขั้นตอนที่สำคัญ<br />ก่อนเชื่อผลลัพธ์</h2><div className={styles.principleLine}><span>กำกับงบต่อ Run</span><span>อนุมัติก่อนดำเนินการที่มีผลกระทบ</span><span>ตรวจที่มาและหลักฐาน</span></div></section>
    <section id="how-it-works" className={styles.how}><div className={styles.sectionHead}><div><p className={styles.kicker}>HOW IT WORKS</p><h2>เส้นทางงานวิจัยที่ตามทัน</h2></div><p>เริ่มจากโจทย์ของคุณ แล้วค่อย ๆ ตรวจสิ่งที่ระบบทำและสิ่งที่ระบบสรุป</p></div><div className={styles.steps}>
      <article><span>01 / ตั้งต้น</span><h3>ส่งโจทย์และข้อมูลตั้งต้น</h3><p>ระบุเป้าหมาย แนบไฟล์ เลือก skill pack ที่ได้รับอนุมัติ และกำหนดเพดานงบกับเวลา</p></article>
      <article><span>02 / ระหว่างทาง</span><h3>ติดตามขั้นตอนและการอนุมัติ</h3><p>ดูขั้นตอน เครื่องมือ และค่าใช้จ่ายที่อัปเดตต่อเนื่อง พร้อมตัดสินใจเมื่อ Run ขออนุมัติ</p></article>
      <article><span>03 / ปลายทาง</span><h3>ตรวจรายงานและหลักฐาน</h3><p>เปิดรายงาน รูปภาพ Artifact และ Citation ที่มี พร้อมตรวจ Provenance ย้อนกลับ</p></article>
    </div></section>
    <section className={styles.end}><div><p className={styles.kicker}>READY TO RESEARCH?</p><h2>เริ่มจากคำถามของคุณ</h2><p>ส่งโจทย์แรก แล้วติดตาม Run ในพื้นที่ทำงานเดียว</p></div><Link className={styles.primaryButton} href="/runs/new">เริ่มงานวิจัย <span aria-hidden="true">→</span></Link></section>
    <footer className={styles.footer}><span>✳ AI Scientist Agent Platform</span><span>Research workbench · ตรวจสอบได้ทุกขั้นตอน</span><a href="#how-it-works">วิธีทำงาน ↑</a></footer>
  </main>;
}
