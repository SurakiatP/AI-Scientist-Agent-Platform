# TOR Requirement Analysis for AI Scientist Agent Platform

วันที่วิเคราะห์ 21 กันยายน 2569

แหล่งข้อมูลเดียวสำหรับ requirement คือ `TOR_AI_Scientist_Agent_Platform_v1.0.docx` ฉบับ 22 หน้า เอกสารนี้แยกข้อกำหนดจากคำอธิบาย ตัวอย่าง และข้อมูลที่ยังต้องตัดสินใจ โดยยังไม่เพิ่มข้อเท็จจริงที่ไม่มีใน TOR

## Phase 1 ผลการอ่านเอกสาร

- เอกสารหลักมีหัวข้อ 1 หลักการและเหตุผล, 3 วัตถุประสงค์ และ 5 ขอบเขตของงาน; ไม่มีหัวข้อ 2 และ 4 ในต้นฉบับ
- ขอบเขตระดับธุรกิจและระบบอยู่หน้า 3-6; หน้า 7 ว่าง
- ภาคผนวก ก Technical Blueprint อยู่หน้า 8-22 และประกาศว่า ADR-001 ถึง ADR-007 เป็นข้อผูกพันของแบบทั้งหมด
- หน้า 2 และ 7 ไม่มีสาระ requirement
- ฟอนต์ไทยไม่ได้ฝังใน DOCX ทำให้ renderer แสดงอักษรไทยเป็นช่องว่างหรือสี่เหลี่ยม แต่ข้อความใน OOXML ยังอ่านได้ครบ การอ้างอิงเลขหน้าในรายงานนี้ยืนยันกับ render 22 หน้าแล้ว
- ไม่พบกำหนดเวลาโครงการ งบโครงการ เจ้าของงาน เกณฑ์ส่งมอบเชิงสัญญา SLA การรับประกัน หรือกฎหมาย/มาตรฐาน compliance ที่ระบุชื่อไว้

## Phase 2 Requirement Matrix

สถานะ `Explicit` หมายถึง TOR ระบุโดยตรง ส่วน `Derived AC` หมายถึง acceptance criterion ที่ทำให้ข้อความ TOR ตรวจรับได้โดยไม่เพิ่มขอบเขตผลิตภัณฑ์

### Functional Requirements

| ID | Requirement | แหล่งอ้างอิง | Acceptance criterion | สถานะ |
|---|---|---|---|---|
| FR-001 | ผู้ใช้ส่ง goal แนบไฟล์ เลือก skill pack และกำหนดงบต่อ run ผ่านเว็บ | หน้า 4 ข้อ 5.1 ก | สร้าง run ได้โดยข้อมูลทั้งสี่ชนิดถูกบันทึกและอ่านกลับได้ | Explicit |
| FR-002 | แสดงขั้นตอน tool calls และค่าใช้จ่ายสะสมแบบ real time | หน้า 4 ข้อ 5.1 ก | UI รับ event stream และอัปเดตสามข้อมูลโดยไม่ reload | Explicit |
| FR-003 | ผู้ใช้อนุมัติหรือปฏิเสธ approval gate | หน้า 4 ข้อ 5.1 ก; หน้า 15 ข้อ A.4.1 | การตัดสินใจเปลี่ยน run จาก awaiting_approval ตาม state machine และมี audit record | Explicit |
| FR-004 | แสดงรายงาน Markdown/LaTeX รูป ตาราง และ artifact | หน้า 4 ข้อ 5.1 ก | run ที่สำเร็จแสดงและดาวน์โหลดผลลัพธ์แต่ละชนิดที่มีได้ | Explicit |
| FR-005 | ผลลัพธ์มี provenance และ citation | หน้า 4 ข้อ 5.1 ก; หน้า 16 ข้อ A.4.3 | ทุก claim ในรายงานอ้าง artifact หรือ citation identifier และ manifest ถูก sealed | Explicit |
| FR-006 | จัดการ Lab ได้แก่สมาชิก API keys MCP/A2A งบประมาณและการใช้งาน | หน้า 5 ข้อ 5.1 ก | owner จัดการรายการที่ระบุและดู usage ของ Lab ได้ | Explicit |
| FR-007 | Web ใช้ OIDC และบทบาท owner researcher viewer | หน้า 5 ข้อ 5.1 ก | login ผ่าน OIDC และการกระทำที่สงวนสิทธิ์ถูกปฏิเสธตาม role | Explicit |
| FR-008 | Platform Gateway ทำ authN/authZ tenant routing rate limiting และ REST/A2A/MCP façades | หน้า 5 ข้อ 5.1 ข | request ทุก protocol ถูกตรวจ identity/quota และ map เป็น Run resource เดียวกัน | Explicit |
| FR-009 | Run Service มี state machine queue retry idempotency และ SSE | หน้า 5 ข้อ 5.1 ข; หน้า 14-15 ข้อ A.4.1-A.4.2 | state/timeout/retry/event resume ทำงานตามตารางใน TOR | Explicit |
| FR-010 | Lab Manager จัดสรร Hermes pod PVC secrets และ NetworkPolicy ต่อ Lab | หน้า 5 ข้อ 5.1 ข | สร้าง Lab แล้วได้ resource แยกตามรายการและลบ/หยุดได้โดยไม่กระทบ Lab อื่น | Explicit |
| FR-011 | Approval and Policy Service รองรับ HITL และ OPA policy | หน้า 5 ข้อ 5.1 ข | tool call ที่ตรง policy ถูกพักและต้องมี approval ก่อนดำเนินต่อ | Explicit |
| FR-012 | Artifact and Provenance Service เก็บ artifact manifest และ lineage | หน้า 5 ข้อ 5.1 ข; หน้า 16 ข้อ A.4.3 | artifact มี metadata/hash/producer และ manifest เชื่อม input step claim output ได้ | Explicit |
| FR-013 | Metering and Audit บันทึก token compute cost และ audit ต่อ run ผู้ใช้ ช่องทาง | หน้า 5 ข้อ 5.1 ข | query ข้อมูลทั้งสี่มิติตาม run/actor/source ได้ | Explicit |
| FR-014 | มี Hermes image แบบ pinned พร้อม config template platform skills และ hooks | หน้า 5 ข้อ 5.1 ค | deployment ใช้ immutable version และบันทึก image/config hash ใน manifest | Explicit |
| FR-015 | Orchestrator มีบทบาท PI Literature Data Scientist Domain Specialist Reviewer Writer ผ่าน delegate_task/output_schema | หน้า 5 ข้อ 5.1 ค; หน้า 16-17 ข้อ A.5.1 | PI delegate แต่ละบทบาทด้วย schema และเก็บผล validation | Explicit |
| FR-016 | Skills ผ่าน pin scan pack eval และ build เป็น image/volume | หน้า 5 ข้อ 5.1 ค; หน้า 9 ADR-006 | build ปฏิเสธ unpinned/scan fail/eval fail และ runtime mount read-only | Explicit |
| FR-017 | พัฒนา platform skills 4 รายการ sci-run-protocol artifact-store provenance-manifest approval-etiquette | หน้า 6 ข้อ 5.1 ค | แต่ละ skill ติดตั้งและมี scenario proof ตามชื่อหน้าที่ | Explicit |
| FR-018 | ใช้ serverless terminal Modal หรือ Daytona สำหรับข้อมูลไม่อ่อนไหว | หน้า 6 ข้อ 5.1 ง | run ที่ผ่านเกณฑ์ non-sensitive ถูกส่งไป backend ที่เลือกและติดตามกลับได้ | Explicit |
| FR-019 | งานหนักส่งไป Nextflow/Modal เป็น child job ของ run | หน้า 6 ข้อ 5.1 ง | child job มี parent run สถานะ log cost และ cancellation propagation | Explicit |
| FR-020 | REST API และ SSE มี OpenAPI specification และ Python SDK ตัวอย่าง | หน้า 6 ข้อ 5.1 จ; หน้า 19-20 ข้อ A.6.2 | OpenAPI 3.1 ครอบคลุม endpoint ที่ระบุ และ SDK เรียก create/status/events/stop ได้ | Explicit |
| FR-021 | A2A v1.0 มี Agent Card ต่อ Lab streaming push notification | หน้า 6 ข้อ 5.1 จ; หน้า 20-21 ข้อ A.6.3 | card แสดง security/capabilities และ state/stream/push map ตาม TOR | Explicit |
| FR-022 | MCP Streamable HTTP OAuth 2.1 มี 7 tools 2 resources และ 1 prompt | หน้า 6 ข้อ 5.1 จ; หน้า 21-22 ข้อ A.6.4 | client ที่ได้รับ scope เรียก surface ทั้งหมดได้และข้าม scope ไม่ได้ | Explicit |
| FR-023 | Telemetry ส่ง trace ไป Langfuse metrics ไป Prometheus/Grafana logs ไป Loki | หน้า 6 ข้อ 5.1 ฉ | run_id/lab_id ใช้เชื่อม trace metric log ของ run เดียวกันได้ | Explicit |
| FR-024 | LLM Gateway ใช้ LiteLLM พร้อม virtual key และ budget ต่อ Lab | หน้า 6 ข้อ 5.1 ฉ | model request ผูก Lab key ตรวจ budget และบันทึก usage ได้ | Explicit |
| FR-025 | วงจรวิจัยเป็น Plan Gather Analyze Critique Report | หน้า 17-18 ข้อ A.5.2 | run สร้าง plan.md ลงทะเบียน evidence/scripts/logs วิจารณ์ไม่เกิน 2 รอบ และ seal report/manifest | Explicit |
| FR-026 | ask_lab ตอบจาก memory/knowledge ของ Lab โดยไม่เปิด sandbox | หน้า 20 ข้อ A.6.2 | request ask_lab ไม่มี sandbox job และไม่เข้าถึง Lab อื่น | Explicit |
| FR-027 | REST API มี endpoint 14 กลุ่มตามตาราง | หน้า 19-20 ข้อ A.6.2 | contract test ครบ method/path/auth/response/เงื่อนไขพิเศษทุกแถว | Explicit |
| FR-028 | A2A map contextId เป็น Hermes session, map task states, stream SSE และ push HMAC-SHA256 | หน้า 20-21 ข้อ A.6.3 | protocol test ยืนยัน mapping และลายเซ็น webhook | Explicit |
| FR-029 | Event schema มี event_id run_id lab_id ts type seq payload source และ payload ตาม type | หน้า 15 ข้อ A.4.2 | schema validation และลำดับ seq ต่อ run ผ่านสำหรับ event ทุกชนิดที่ระบุ | Explicit |
| FR-030 | Delegation schema ต้องมี claims artifacts caveats และ claim มี evidence/confidence | หน้า 17 ข้อ A.5.1 | child แก้ schema ได้ 1 รอบ; failure คืน schema_valid false พร้อม raw summary | Explicit |

### Non Functional Requirements

| ID | Requirement | แหล่งอ้างอิง | Acceptance criterion | สถานะ |
|---|---|---|---|---|
| NFR-001 | Multi-tenancy แยกข้อมูล สิทธิ์ ค่าใช้จ่าย และสภาพแวดล้อมรันโค้ด | หน้า 4 ข้อ 3.5; หน้า 10-11 ข้อ A.2 | isolation tests พิสูจน์ cross-Lab read/write/compute/usage ถูกปฏิเสธ | Explicit |
| NFR-002 | Hermes ports 8642/9900 เป็น ClusterIP และ NetworkPolicy allow เฉพาะ service ที่กำหนด | หน้า 9 ข้อควรระวังสูงสุด | ไม่มี public route และ network probe จาก namespace/pod อื่นถูกปฏิเสธ | Explicit |
| NFR-003 | ทุก protocol normalize เป็น lab_id principal scopes ก่อน Run Service | หน้า 18 ข้อ A.6.1 | request ที่ identity ไม่ครบถูกปฏิเสธและ identity เดียวกันให้ authorization เท่ากันทุก protocol | Explicit |
| NFR-004 | API key hash ใน DB; MCP OAuth 2.1 audience mcp.scilab; A2A bearer หรือ mTLS | หน้า 18 ข้อ A.6.1 | secret ไม่ถูกเก็บ plaintext และ negative auth tests ครบทุก protocol | Explicit |
| NFR-005 | Skill scan รายสัปดาห์และตรวจ license รายสกิล | หน้า 14 ข้อ A.3.2 | มีผล scan ล่าสุดไม่เกิน 7 วันและ license inventory ของ pinned pack | Explicit |
| NFR-006 | Tooling ต้องใช้ Python 3.13+ และ uv; dependencies อยู่ใน sandbox image | หน้า 14 ข้อ A.3.2 | image report แสดงเวอร์ชันและ run ไม่ติดตั้ง dependency ลง control plane | Explicit |
| NFR-007 | Queue timeout 30 นาที; run default 120 นาที; approval 24 ชม.; heartbeat loss 90 วินาที; retry สูงสุด 2 | หน้า 14-15 ข้อ A.4.1 | deterministic timeout tests ทำให้ state/reason ตรงตาราง | Explicit |
| NFR-008 | SSE resume ด้วย from_seq heartbeat 15 วินาที; presigned URL หมดอายุ 15 นาที | หน้า 19 ข้อ A.6.2 | reconnect ไม่ตก event และ URL ใช้ไม่ได้หลังหมดอายุ | Explicit |
| NFR-009 | Manifest ระบุ version/hash/input/steps/claims/cost และ sealed hash | หน้า 16 ข้อ A.4.3 | JSON schema ผ่านและการแก้หลัง seal ทำให้ hash verification fail | Explicit |
| NFR-010 | Reviewer ใช้ provider ต่างจาก PI และ critique สูงสุด 2 รอบ | หน้า 16-18 ข้อ A.5.1-A.5.2 | run evidence แสดง provider แยกและไม่เกินรอบที่กำหนด | Explicit |
| NFR-011 | Hermes API default concurrency 10 และ delegation default concurrency 10 | หน้า 12-13 ข้อ A.3.1 | config/runtime แสดง limit และ request เกิน limit ถูก queue/reject ตาม behavior ที่อนุมัติ | Explicit แต่ behavior เกิน limit ยังไม่ระบุ |
| NFR-012 | tool event ต้องใช้ args_redacted | หน้า 15 ข้อ A.4.2 | secret fixture ไม่ปรากฏใน event/log/audit output | Explicit |

### Compliance Requirements

TOR ไม่ระบุกฎหมายหรือมาตรฐานภายนอก เช่น PDPA ISO 27001 หรือมาตรฐาน retention ดังนั้นตารางนี้ครอบคลุมเฉพาะข้อผูกพันภายใน TOR

| ID | ข้อผูกพัน | แหล่งอ้างอิง | หลักฐานตรวจรับ | สถานะปัจจุบัน |
|---|---|---|---|---|
| CR-001 | ADR-001 ถึง ADR-007 เป็นข้อผูกพันของแบบ | หน้า 8-9 ข้อ A.1 | ADR conformance review และ architecture test | Unverified |
| CR-002 | Identity/authorization เดียวกันทุก protocol | หน้า 4 ข้อ 3.3; หน้า 18 ข้อ A.6.1 | cross-protocol authorization matrix | Unverified |
| CR-003 | Tenant isolation | หน้า 4 ข้อ 3.5 | adversarial cross-tenant tests | Unverified |
| CR-004 | Hermes API/A2A ไม่เปิดสาธารณะ | หน้า 9 ข้อควรระวังสูงสุด | cluster route และ network policy evidence | Unverified |
| CR-005 | Skill supply chain pin/scan/eval/read-only | หน้า 9 ADR-006; หน้า 14 ข้อ A.3.2 | SBOM/lock/scan/eval/mount evidence | Unverified |
| CR-006 | License แยกราย skill | หน้า 14 ข้อ A.3.2 | license inventory และ approval record | Unverified |
| CR-007 | Audit/metering ต่อ run actor source | หน้า 5 ข้อ 5.1 ข | immutable queryable audit sample | Unverified |
| CR-008 | Claims อ้าง evidence และ manifest ตรวจย้อนกลับได้ | หน้า 16-18 ข้อ A.4.3-A.5.2 | sealed manifest plus claim-to-evidence trace | Unverified |
| CR-009 | Secrets และ sensitive args ไม่รั่ว | หน้า 5 ข้อ 5.1 ข; หน้า 15 ข้อ A.4.2; หน้า 18 ข้อ A.6.1 | secret storage review และ redaction tests | Unverified |

### Deliverables

| ID | Deliverable | แหล่งอ้างอิง | หลักฐานตรวจรับ |
|---|---|---|---|
| DEL-001 | Web application สำหรับ submit track approve result และ Lab admin | หน้า 4-5 ข้อ 5.1 ก | end-to-end demo และ browser test |
| DEL-002 | Control Plane ได้แก่ Gateway Run Service Lab Manager Policy Artifact Metering | หน้า 5 ข้อ 5.1 ข | deployed services plus service-level contract tests |
| DEL-003 | Pinned Hermes organizational image/config/hooks | หน้า 5 ข้อ 5.1 ค | image digest config hash smoke test |
| DEL-004 | Scientific skill packs และ platform skills 4 รายการ | หน้า 5-6 ข้อ 5.1 ค | pinned image scan/eval results และ skill scenarios |
| DEL-005 | Sandbox/compute integrations | หน้า 6 ข้อ 5.1 ง | non-sensitive terminal และ heavy child-job demonstrations |
| DEL-006 | REST API OpenAPI 3.1 SSE และ Python SDK example | หน้า 6 ข้อ 5.1 จ; หน้า 19-20 ข้อ A.6.2 | generated contract and executable sample |
| DEL-007 | A2A v1.0 server Agent Card ต่อ Lab และ push notification | หน้า 6 ข้อ 5.1 จ; หน้า 20-21 ข้อ A.6.3 | A2A conformance scenario and signed webhook |
| DEL-008 | MCP Streamable HTTP OAuth 2.1 server | หน้า 6 ข้อ 5.1 จ; หน้า 21-22 ข้อ A.6.4 | MCP client interoperability test |
| DEL-009 | Observability and LLM gateway configuration | หน้า 6 ข้อ 5.1 ฉ | trace/metric/log/cost correlation evidence |
| DEL-010 | Architecture runbook user admin and API documents | หน้า 6 ข้อ 5.1 ฉ | document review checklist; required sections present |

### Constraints and Binding Decisions

| ID | Constraint | แหล่งอ้างอิง |
|---|---|---|
| CON-001 | ไม่สร้าง agent framework ใหม่; ใช้ Hermes เป็น PI orchestrator หนึ่ง instance ต่อ Lab | หน้า 3 ข้อ 1; หน้า 8 ADR-001 |
| CON-002 | งานวิจัยเข้าทาง Runs API ไม่ใช่ chat completions | หน้า 8 ADR-002 |
| CON-003 | REST A2A MCP map เป็น Run resource เดียวผ่าน Platform Gateway | หน้า 8 ADR-003 |
| CON-004 | MCP server เขียนด้วย FastMCP ไม่ใช้ hermes mcp serve | หน้า 8 ADR-004 |
| CON-005 | A2A server เขียนด้วย a2a-sdk; Hermes A2A ใช้ภายใน cluster | หน้า 9 ADR-005 |
| CON-006 | Skills pinned scanned packed evaluated built และ runtime mount read-only | หน้า 9 ADR-006 |
| CON-007 | Model access ผ่าน OpenRouter/LiteLLM; data residency เลื่อนไป Phase 2 | หน้า 9 ADR-007 |
| CON-008 | Kubernetes เป็นแพลตฟอร์ม multi-tenant | หน้า 4 ข้อ 3.5 |
| CON-009 | Temporal เป็น Phase 2 และ cron literature watch เป็น P3 | หน้า 10-13 ข้อ A.2-A.3.1 |
| CON-010 | Backend ของแพลตฟอร์มใช้ Python | คำยืนยันจากเจ้าของโครงการ 21 กันยายน 2569 |
| CON-011 | Scientific Agent Skills ใช้ repository `K-Dense-AI/scientific-agent-skills` เป็น upstream | คำยืนยันจากเจ้าของโครงการ 21 กันยายน 2569 |

## ข้อกำกวม ความขัดแย้ง และข้อมูลที่ขาด

| ID | ประเภท | ประเด็น | ผลกระทบ |
|---|---|---|---|
| D-001 | Scope | TOR กล่าวถึง Phase 2/P3 แต่ไม่มีนิยาม Phase 1 หรือ milestone รวม | ไม่ทราบว่าต้องส่งอะไรในสัญญารอบแรก |
| D-002 | Choice | Modal หรือ Daytona; Nextflow หรือ Modal; kopf หรือ kubebuilder; gVisor หรือ Kata ยังไม่เลือก | เปลี่ยน architecture effort และ acceptance evidence |
| D-003 | Choice | A2A ใช้ bearer หรือ mTLS; Model gateway ต้องใช้ OpenRouter ร่วม LiteLLM หรือเลือกหนึ่ง | มีผล security และ deployment |
| D-004 | Conflict | ข้อมูลไม่อ่อนไหวใช้ serverless terminal แต่ blueprint มี sandbox pod ต่อ run; ไม่มี routing rule สำหรับ sensitive data | ไม่สามารถเขียน testable compute policy |
| D-005 | Conflict | ask_lab ระบุใช้ memory/knowledge แต่ map ไป chat completions แบบ stateless | ต้องนิยามว่า stateless หมายถึงไม่มี conversation state หรือห้ามใช้ memory |
| D-006 | Conflict | failed retry ใช้ Idempotency-Key เดิม แต่ queued กำหนด key ไม่ซ้ำ | ต้องกำหนด internal retry exception และ external duplicate semantics |
| D-007 | Version and selection | ไม่มี exact pinned version ของ Hermes; upstream Scientific Agent Skills ณ วันที่ตรวจมี latest tag v2.69.0, 166 skills และ Python >=3.13 ซึ่งต่างจาก TOR ที่ระบุ 156 skills; upstream แนะนำติดตั้งเฉพาะ subset ที่ใช้และรองรับ security fixes เฉพาะ main/latest tag | ต้องเลือก tag/commit และ initial skill packs; ห้าม hard-code จำนวน 156 |
| D-008 | Capacity | ไม่มีจำนวนผู้ใช้ Labs concurrent runs storage throughput หรือ data size | ประเมิน effort/infrastructure/SLA ไม่ได้ |
| D-009 | Security | ไม่มี data classification encryption retention backup DR RPO/RTO key rotation vulnerability response หรือ PDPA requirement | compliance/security acceptance ไม่ครบ |
| D-010 | Product | ไม่มีภาษา UI browser accessibility attachment type/size artifact retention และ notification UX | UI acceptance ยังไม่ปิด |
| D-011 | API | ไม่มี canonical error schema API version policy pagination limit rate-limit values และ webhook retry policy | contract tests ยังไม่ครบ |
| D-012 | Policy | ไม่ระบุ tool/actions ที่ต้อง approval หรือ OPA rules เริ่มต้น | HITL behavior ยังไม่ปิด |
| D-013 | Cost | ไม่ยืนยันสกุลเงิน/default budget; THB และ 150.0 ปรากฏใน schema/example | budget behavior อาจตีความผิด |
| D-014 | Delivery | ไม่มี environment CI/CD source ownership training support warranty schedule หรือ formal acceptance process | แตก owner/effort และหลักฐานเชิงสัญญาไม่ได้ |
| D-015 | Document | หัวข้อหลักกระโดด 1 ไป 3 ไป 5; หน้า 2/7 ว่าง; ฟอนต์ไทยไม่ฝัง | ควรยืนยันว่าไม่มีข้อ 2/4 สูญหายและแก้ไฟล์ก่อนใช้เป็นเอกสารสัญญา |

## Assumptions ที่ปลอดภัยสำหรับการวิเคราะห์เท่านั้น

- Appendix ก เป็น normative เพราะระบุว่า ADR-001 ถึง ADR-007 เป็นข้อผูกพัน แต่ตัวอย่าง JSON/code ไม่ถือเป็นค่า production ที่อนุมัติ
- คำว่า stateless ใน ask_lab ตีความชั่วคราวว่าไม่มี conversational session ไม่ได้หมายความว่าห้ามอ่าน Lab knowledge; ต้องยืนยันใน D-005
- สิ่งที่ระบุ Phase 2/P3 ไม่อยู่ใน initial delivery จนกว่าจะยืนยัน D-001
- ค่า default ที่ระบุในตาราง state machine ถือเป็น requirement; ค่าใน code example ถือเป็นตัวอย่างจนกว่าจะยืนยัน
- ไม่มีการอ้าง compliance ภายนอกที่ TOR ไม่ระบุ

## Acceptance Boundary จาก Lean Build

### Acceptance criteria ระดับโครงการ

1. ผู้ใช้หนึ่ง Lab ทำ workflow submit to report ได้ครบผ่าน web และ run ถูก trace ไปถึง artifacts claims citations cost และ sealed manifest
2. REST A2A MCP เรียก Run resource เดียวกันภายใต้ identity/scopes เดียวกันและมี negative authorization tests
3. สอง Labs ไม่สามารถอ่านหรือเปลี่ยนข้อมูล secrets artifacts compute usage ของกันและกัน
4. State machine timeout retry approval cancellation และ event resume ผ่านตามค่าที่ TOR กำหนด
5. Hermes endpoints ไม่เปิดภายนอก cluster และ skill/runtime supply chain ผ่าน security evidence
6. Deliverables DEL-001 ถึง DEL-010 มี executable proof หรือ review checklist ที่ผูกกลับ Requirement ID

### Out of Scope ที่ TOR ระบุหรือยังไม่ให้อำนาจ

- สร้าง agent framework ใหม่
- Data residency implementation ซึ่ง ADR-007 ระบุ Phase 2
- Temporal ซึ่งตารางสถาปัตยกรรมระบุ Phase 2
- Cron literature watch ซึ่งระบุ P3
- กฎหมาย มาตรฐาน SLA HA/DR และ performance target ที่ TOR ไม่ได้กำหนด
- การเลือก provider/runtime จากตัวเลือกที่ TOR ยังไม่ตัดสิน
- ฟีเจอร์ UI/API เพิ่มเติมนอก matrix

## Phase 3 Specification Draft ก่อน Decision Gate

### Problem

นักวิจัยต้องประสานหลายขั้นตอนและเครื่องมือ ขณะที่ Hermes Agent และ Scientific Agent Skills ยังขาดชั้นองค์กรสำหรับ multi-tenancy security provenance cost control และมาตรฐานการเชื่อมต่อ หน้า 3 ข้อ 1

### Solution

สร้าง control plane บางรอบ Hermes โดยรวม web REST A2A MCP เข้าสู่ Run resource เดียว แยก Lab บน Kubernetes รันโค้ดใน sandbox เก็บ artifact/provenance และใช้ skills ผ่าน supply chain ที่ควบคุมได้ หน้า 4-11 ข้อ 3, 5.1 และ A.1-A.2

### User Stories

- ในฐานะ researcher ฉันส่งงานวิจัย ติดตาม run อนุมัติขั้นตอน และรับรายงานพร้อมหลักฐานได้
- ในฐานะ Lab owner ฉันจัดการสมาชิก keys peers budgets และ usage ได้
- ในฐานะ external system หรือ agent ฉันเริ่มและติดตามงานผ่าน REST A2A หรือ MCP ด้วย identity/scopes ที่สอดคล้องกันได้
- ในฐานะ reviewer/auditor ฉันตรวจ claim ย้อนกลับไปยัง citation artifact step image/config และ cost ได้
- ในฐานะ administrator ฉัน provision Lab และยืนยัน isolation policy observability และ security posture ได้

### Decisions ที่ TOR ยืนยันแล้ว

- ใช้ Hermes ไม่สร้าง agent framework ใหม่
- หนึ่ง Hermes PI ต่อหนึ่ง Lab และ specialist ผ่าน delegation
- Runs API เป็นทางหลักสำหรับงานวิจัย
- Platform Gateway map REST A2A MCP เป็น Run เดียว
- FastMCP และ a2a-sdk เป็น façade ของแพลตฟอร์ม
- Skill supply chain ต้อง pin/scan/pack/eval/build และ mount read-only
- Backend ใช้ Python
- Scientific Agent Skills ใช้ `https://github.com/K-Dense-AI/scientific-agent-skills` เป็น upstream

### Testing Decisions ที่ยืนยันได้

- ใช้ end-to-end proof สำหรับ workflow หลัก
- ใช้ contract tests สำหรับ REST/A2A/MCP และ schema
- ใช้ isolation/security negative tests ระหว่าง Labs
- ใช้ deterministic state-machine tests สำหรับ timeout/retry/approval
- ใช้ provenance integrity test สำหรับ manifest/hash/claim evidence
- ใช้ deployment/network tests ยืนยัน ClusterIP/NetworkPolicy

### Deliverables

ใช้ DEL-001 ถึง DEL-010 โดยไม่เพิ่ม deliverable นอก TOR

### Out of Scope

ใช้รายการใน Acceptance Boundary ข้างต้น

### Decision Gate

ปิดแล้วเมื่อ 21 กันยายน 2569 ผลตัดสินอยู่ใน `docs/specs/ai-scientist-agent-platform-spec.md` สรุปหลักคือ backend Python 3.13/FastAPI, Kopf, Keycloak, selected Scientific Agent Skills v2.69.0, OpenSandbox release-1.1.0 บน Kata, Modal เฉพาะข้อมูลไม่อ่อนไหว, A2A bearer ผ่าน TLS และใช้ค่า capacity จาก TOR โดยไม่เพิ่ม SLA

## Phase 4 และ Phase 5

- Phase 4: `docs/superpowers/plans/2026-09-21-ai-scientist-agent-platform.md`
- Phase 5: `docs/TOR/TOR_Traceability_Verification_v1.0.md`
- ไม่มีการสร้าง issue และไม่มีการเขียน product code
