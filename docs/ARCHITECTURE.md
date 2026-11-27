# Personal ATS Resume Checker — Architecture

> สถานะ: Draft v1 · 2026-11-27
> ขอบเขต: ระบบตรวจ resume เทียบ Job Description (ไทย + อังกฤษ) ใช้ส่วนตัวบนเครื่องเดียว รันบน Kubernetes (k3d) ใช้ Claude API เป็นสมอง

---

## 0. เป้าหมายและหลักการ (ตามลำดับความสำคัญ)

1. **Security first** — Anthropic API key ห้ามหลุดเด็ดขาด และข้อมูลส่วนตัวใน resume ต้องถูกส่งออกนอกเครื่องให้น้อยที่สุด
2. **Accuracy** — คะแนนต้องอธิบายได้ ทุกคำตัดสินต้องมีหลักฐานจาก resume และวัดผลได้ด้วย eval set
3. **Bilingual** — resume/JD ภาษาไทย อังกฤษ หรือปนกัน ต้องทำงานได้เท่ากัน
4. **Learnable infra** — ใช้ Kubernetes จริงจัง (NetworkPolicy, RBAC, Secret, PSA) แต่ไม่ซับซ้อนเกินเครื่องเดียว

**Non-goals:** multi-user, เปิดให้คนอื่นเข้าจากอินเทอร์เน็ต, ATS tracker (แบบที่ 1)

---

## 1. ภาพรวมระบบ

```bash
                         localhost only (127.0.0.1:8443, TLS)
                                     │
┌────────────────────────── k3d cluster · namespace: ats ─────────────────────────────┐
│  PSA: restricted · default-deny NetworkPolicy (ingress+egress)                      │
│                                                                                     │
│   Traefik ──► ui (Streamlit) ──► api (FastAPI + auth) ──► redis (queue)             │
│                                      │                       │                      │
│                                      ▼                       ▼                      │
│                              postgres+pgvector ◄──── worker ×N (pipeline)           │
│                                      ▲                 │      │      │              │
│                              minio (resume files) ◄────┘      │      │              │
│                                                               ▼      ▼              │
│                                                        embedder   llm-gateway  🔑   │
│                                                        (bge-m3,   (ตัวเดียวที่       │
│                                                        reranker)   ถือ API key)     │
│                                                                      │              │
│                                                                      ▼              │
│                                                              egress-proxy           │
│                                                              (allowlist:            │
│                                                               api.anthropic.com)    │
└──────────────────────────────────────────────────────────────────────┼──────────────┘
                                                                       ▼
                                                             api.anthropic.com:443
```

| Service | ภาษา/เครื่องมือ | หน้าที่ | ถือ secret? | Egress ออกนอก cluster |
| --- | --- | --- | --- | --- |
| `ui` | Streamlit | อัปโหลด resume, วาง JD, ดูรายงาน | ไม่ | ไม่ |
| `api` | FastAPI | auth, validate, สร้าง job, อ่านผล | session key เท่านั้น | ไม่ |
| `worker` | Python + arq | รัน pipeline ขั้น 1–7 | DB creds | ไม่ |
| `embedder` | sentence-transformers | embedding + rerank (CPU, offline) | ไม่ | ไม่ (โมเดลโหลดล่วงหน้าลง PVC) |
| `llm-gateway` | FastAPI + `anthropic` SDK | ตัวกลางเดียวที่คุยกับ Claude: PII redaction, budget, audit | **ANTHROPIC_API_KEY** | ผ่าน proxy เท่านั้น |
| `egress-proxy` | Squid/Envoy | อนุญาตเฉพาะ `api.anthropic.com:443` | ไม่ | 443 เท่านั้น |
| `postgres` | PostgreSQL 16 + pgvector | ผลวิเคราะห์, vectors, eval set | DB creds | ไม่ |
| `redis` | Redis 7 | job queue | password | ไม่ |
| `minio` | MinIO | เก็บไฟล์ต้นฉบับ (SSE เปิด) | root creds | ไม่ |

> ทำไมไม่มี Ollama แล้ว: ใช้ Claude ทำงาน LLM ทั้งหมดเพื่อความแม่นยำสูงสุด ส่วน embeddings ยังรัน local เพราะ Anthropic ไม่มี embedding endpoint และ local ก็ไม่ต้องส่งข้อมูลออก

---

## 2. Security Architecture

### 2.1 Threat model

| # | ภัย | ผลกระทบ | มาตรการหลัก |
| --- | --- | --- | --- |
| T1 | API key ถูก commit ขึ้น git | ถูกขโมยใช้ เสียเงิน | key ไม่เคยอยู่ในไฟล์ของ repo, gitleaks pre-commit, `.gitignore` |
| T2 | key ฝังใน Docker image / build arg | ใครได้ image ก็ได้ key | key inject ตอน runtime เท่านั้น, `.dockerignore`, scan image |
| T3 | key โผล่ใน log / error / trace | หลุดผ่าน log file | redaction filter, ปิด SDK debug log, ไม่ log headers |
| T4 | pod อื่นถูกเจาะแล้วอ่าน key | ลาม | มีแค่ `llm-gateway` mount secret, RBAC ห้ามอ่าน secret, ไม่ automount SA token |
| T5 | อ่าน Secret จาก etcd/datastore ตรงๆ | key หลุดจาก disk | k3s secrets-encryption at rest |
| T6 | key อยู่ใน env var แล้วหลุดผ่าน `/proc`, crash dump, `kubectl describe` | หลุด | mount เป็น **file** (tmpfs) ไม่ใช่ env var |
| T7 | Prompt injection ใน JD/resume ("ignore previous instructions…") | ผลคะแนนเพี้ยน / พยายามขโมยข้อมูล | key ไม่เคยอยู่ใน prompt, gateway ไม่มี tools, output ผ่าน schema validation, ตรวจ evidence เชิงกำหนด |
| T8 | ข้อมูลรั่วออกไปที่อื่นนอกจาก Anthropic (เช่น lib ส่ง telemetry) | PII หลุด | default-deny egress, proxy allowlist โดเมนเดียว |
| T9 | PII ใน resume ถูกส่งไป Anthropic เกินจำเป็น | privacy | PII redaction ก่อนส่ง (ชื่อ, เบอร์, อีเมล, ที่อยู่, เลขบัตร ปชช.) |
| T10 | ค่าใช้จ่ายพุ่ง (loop bug / key ถูกใช้) | เสียเงิน | spend limit ที่ Anthropic Console + daily budget ใน gateway + circuit breaker |
| T11 | คนอื่นใน LAN เข้า UI | เห็น resume | bind `127.0.0.1` เท่านั้น + TLS + login |
| T12 | Dependency ถูก compromise (supply chain) | โค้ดอันตรายอ่าน key | lockfile พร้อม hash (`uv.lock`), `pip-audit`, image จาก digest, egress-deny ช่วยจำกัดความเสียหาย |

### 2.2 ชีวิตของ API key (key lifecycle)

```bash
Anthropic Console                OS keyring (GNOME)           k3s (encrypted at rest)          llm-gateway pod
─────────────────                ──────────────────           ───────────────────────          ───────────────
สร้าง key ใน Workspace   ──►    secret-tool store       ──►   Secret "anthropic-api-key"  ──►  /run/secrets/anthropic/api-key
"personal-ats" เท่านั้น          (พิมพ์ผ่าน prompt,              (สร้างด้วย stdin pipe,            (tmpfs, 0400, readOnly)
+ spend limit ต่อเดือน           ไม่ผ่าน shell history)          ไม่มีไฟล์ YAML ใน repo)           อ่านครั้งเดียวตอน start
```

กฎเหล็ก:

1. **ไม่มี key ในไฟล์ใดๆ ใน repo** — ไม่มี `.env` ที่มี key จริง, ไม่มี Secret YAML ที่มีค่า (แม้ base64 ก็ไม่ได้ เพราะ base64 ไม่ใช่การเข้ารหัส)
2. **ไม่พิมพ์ key บน command line** — ใช้ `secret-tool store` ที่ถามผ่าน prompt แล้ว pipe เข้า `kubectl` ผ่าน stdin  

   ```bash
   secret-tool store --label="Anthropic personal-ats" service anthropic account personal-ats
   secret-tool lookup service anthropic account personal-ats \
     | kubectl -n ats create secret generic anthropic-api-key --from-file=api-key=/dev/stdin
   ```

3. **Workspace แยก + key แยก** — สร้าง Workspace `personal-ats` ใน Anthropic Console ตั้ง spend limit รายเดือน ไม่ใช้ key ร่วมกับโปรเจกต์อื่น
4. **Rotation** — หมุน key ทุก 90 วัน หรือทันทีที่สงสัย: สร้างใหม่ → อัปเดต keyring → `kubectl create secret ... --dry-run=client -o yaml | kubectl apply -f -` → restart gateway → ลบ key เก่าใน Console (มี `scripts/rotate-key.sh`)
5. **Incident runbook** — ถ้าสงสัยว่าหลุด: (1) revoke ใน Console ทันที (2) ดู usage log ใน Console (3) หมุน key (4) หา root cause

### 2.3 Kubernetes hardening

| ชั้น | การตั้งค่า |
| --- | --- |
| Cluster | `k3d cluster create ats --k3s-arg "--secrets-encryption@server:0" -p "127.0.0.1:8443:443@loadbalancer"` → Secret เข้ารหัสใน datastore, port bind localhost เท่านั้น |
| Namespace | label `pod-security.kubernetes.io/enforce: restricted` |
| Pod securityContext (ทุก pod) | `runAsNonRoot: true`, `readOnlyRootFilesystem: true`, `allowPrivilegeEscalation: false`, `capabilities.drop: [ALL]`, `seccompProfile: RuntimeDefault` |
| ServiceAccount | 1 SA ต่อ service, `automountServiceAccountToken: false`, ไม่มี Role ไหนอ่าน `secrets` ได้ |
| Secret mount | เฉพาะ `llm-gateway`: volume `secret` → `/run/secrets/anthropic`, `defaultMode: 0400`, `readOnly: true` |
| NetworkPolicy | `default-deny-all` (ingress+egress) แล้วเปิดทีละเส้น: ui→api, api→redis/postgres/minio, worker→embedder/llm-gateway/postgres/minio/redis, llm-gateway→egress-proxy, egress-proxy→0.0.0.0/0:443, ทุก pod→kube-dns:53 |
| Egress FQDN | NetworkPolicy มาตรฐานกรองชื่อโดเมนไม่ได้ จึงให้ `egress-proxy` บังคับ allowlist `api.anthropic.com` (CONNECT เท่านั้น) และ gateway ตั้ง `HTTPS_PROXY` ไปที่ proxy |
| Resources | ตั้ง requests/limits ทุก pod (กัน runaway) |
| Images | multi-stage build, base `python:3.12-slim` (หรือ distroless), user non-root, scan ด้วย `trivy`, อ้าง image ด้วย digest |

### 2.4 Application security

**llm-gateway (จุดเดียวที่สัมผัส key):**  

- อ่าน key จากไฟล์ครั้งเดียว เก็บในตัวแปร process แล้วส่งให้   `anthropic.Anthropic(api_key=...)` — ไม่เขียนลง env, ไม่ log, ไม่ return ใน response ใดๆ
- API ภายในเป็นแบบ **task-specific** (`/v1/parse-jd`, `/v1/structure-resume`, `/v1/judge`) ไม่ใช่ proxy ทั่วไป → worker ส่ง prompt อิสระไม่ได้ กำหนด model / system prompt / schema ไว้ฝั่ง gateway
- ยืนยันตัวผู้เรียกด้วย shared token ภายใน (แยกจาก API key) + NetworkPolicy อนุญาตเฉพาะ worker
- **PII redaction ก่อนส่ง** (`ats_core.pii`): แทนที่ด้วย placeholder คงที่ต่อค่า (`[EMAIL_1]`) และเก็บ mapping ไว้ในเครื่องเท่านั้น เพื่อแปลงผลลัพธ์กลับตอนแสดง
  - **ลบเสมอ:** ชื่อ-นามสกุล (บรรทัดหัว resume, คำนำหน้า นาย/นางสาว/Mr./Dr., ป้าย "ชื่อ:"), อีเมล (รวมแบบ `[at]`), เบอร์โทรไทย/สากลทุกรูปแบบการเว้นวรรค, เลขบัตรประชาชน (checksum หรือมีป้ายกำกับ), ที่อยู่ (ป้าย "ที่อยู่" หรือ marker ถนน/ซอย/ตำบล/รหัสไปรษณีย์), URL โปรไฟล์ส่วนตัว, LINE/Skype ID
  - **วันเกิด:** ลบวันที่ แต่เก็บอายุโดยประมาณใน placeholder (`[DATE_OF_BIRTH_1 age 31]`) เพราะ JD ไทยมักระบุช่วงอายุ
  - **ข้อมูลอ่อนไหว:** ลบศาสนาและสถานภาพสมรส (ไม่ใช่เกณฑ์ที่ชอบธรรม)
  - **เก็บไว้:** จังหวัด/เมือง, เพศ, สัญชาติ, การเกณฑ์ทหาร — อาจเป็นคุณสมบัติของงานและระบุตัวบุคคลไม่ได้
  - **กัน false positive:** ป้ายต้องอยู่ต้น segment, คำอังกฤษต้องมี word boundary, คำนำหน้าไทยต้องตามด้วยชื่อ + นามสกุล และไม่ใช่คำประสม (นายจ้าง, นางพยาบาล)
  - **Fail closed:** gateway เรียก `find_residual_pii()` ซ้ำกับข้อความที่ลบแล้ว ถ้ายังเจออีเมล/เบอร์/เลขบัตร/URL โปรไฟล์ → ไม่ส่ง
  - ใช้ rule-based แทน NER model: ตรวจสอบได้, deterministic, ไม่ต้องโหลดโมเดลใหญ่; ทดสอบทั้ง recall (ต้องลบครบ) และ precision (ต้องไม่ลบเนื้อหางาน)
- **Budget guard:** นับ token และเงินจาก `response.usage` ลง cost ledger (ดู §11) ถ้าเกิน daily budget ($2) → ปฏิเสธ request (HTTP 429) และ circuit breaker เมื่อ error ติดกัน
- **Audit log:** เก็บ request id, task, token count, latency, model — **ไม่เก็บ** prompt/response เต็ม

**Logging policy (ทุก service):**  

- `logging.Filter` กลาง (ใน `ats_core.logging`) ลบ pattern `sk-ant-[A-Za-z0-9_\-]+`, `Authorization`, `x-api-key` ก่อนเขียน log
- ห้ามตั้ง `ANTHROPIC_LOG=debug` ใน cluster
- exception handler ของ FastAPI คืน error ทั่วไป ไม่คืน stack trace

**Prompt-injection defense:**  

- resume และ JD ถูกห่อใน tag ชัดเจน (`<resume>…</resume>`, `<job_description>…</job_description>`) พร้อม system prompt ว่าเนื้อหาข้างในเป็นข้อมูลเท่านั้น
- ไม่มี tool ใดๆ ให้ Claude เรียก, output ต้องผ่าน structured output schema
- หลักฐานที่ Claude อ้างต้องตรงกับข้อความใน resume จริง (ดู 3.6) — ป้องกันทั้ง hallucination และ injection ที่พยายามปั้นคะแนน
- ตรวจจับข้อความซ่อน (ตัวอักษรสีขาว, font ขนาด 1px) ในขั้น parseability → แจ้งเป็น red flag

**UI / API:**  

- เข้าได้จาก `https://127.0.0.1:8443` เท่านั้น, TLS จาก `mkcert`
- login ผู้ใช้เดียว: รหัสผ่าน hash ด้วย argon2, session cookie `HttpOnly; Secure; SameSite=Strict`, CSRF token
- จำกัดไฟล์อัปโหลด: PDF/DOCX เท่านั้น, ≤ 10 MB, ตรวจ magic bytes, parse ใน worker (ไม่ใช่ api)

**Data at rest:**  

- MinIO เปิด SSE, Postgres อยู่บน PVC (แนะนำให้ disk ของเครื่องเข้ารหัสด้วย LUKS อยู่แล้ว)
- การลบข้อมูล: **ไม่มีการลบอัตโนมัติ** (ผู้ใช้เลือกเก็บไว้เอง) — ลบด้วย `ats forget --client` ซึ่งลบไฟล์ ผลวิเคราะห์ และชื่อลูกค้า เหลือเฉพาะยอดเงินแบบไม่ระบุตัวตน

### 2.5 Repo & supply chain

- `.gitignore`: `.env*`, `*.key`, `secrets/`, `kubeconfig*`
- `pre-commit`: `gitleaks`, `ruff`, `detect-private-key`
- `uv.lock` ล็อก dependency พร้อม hash, CI local: `pip-audit`, `trivy image`
- `.dockerignore` กันไม่ให้ `.git`, `.env*`, `secrets/` เข้า build context

### 2.6 ข้อมูลที่ส่งไป Anthropic

- ส่งเฉพาะ: ข้อความ resume ที่ redact PII แล้ว และ JD
- ไม่ส่ง: ไฟล์ต้นฉบับ (ยกเว้นเปิดโหมด "vision parse" ที่ต้อง redact ภาพก่อน — ปิดไว้เป็นค่าเริ่มต้น)
- ข้อมูล API ไม่ถูกใช้ train ตาม Commercial Terms; data retention เป็นไปตามนโยบายของ Anthropic (ZDR ต้องทำข้อตกลงกับ Anthropic แยก) → นี่คือเหตุผลที่ต้อง redact ฝั่งเรา

---

## 3. Analysis Pipeline (ความแม่นยำ)

```bash
Resume ─► [1 Extract] ─► [2 Parseability] ─► [3 Redact] ─► [4 Structure] ─┐
                                                                          ├─► [6 Match] ─► [7 Score + Report]
JD ─────────────────────────────────────────► [5 JD Parse] ───────────────┘
```

### 3.1 Extract (local, worker)  

- PDF: `pypdfium2` (PDFium) ดึงข้อความ ตำแหน่ง ขนาด font สี และ render mode ราย character แล้วประกอบบรรทัดเองจากพิกัด (จัดกลุ่มตาม baseline, สระ/วรรณยุกต์ไทยติดกับพยัญชนะ, เว้นวรรคเมื่อช่องว่าง > 0.25 em); `pdfplumber` ใช้เฉพาะตรวจตารางที่มีเส้น
- DOCX: `python-docx` แยกเนื้อหา body / table / header / footer / text box
- ไม่ทำ OCR: หน้าที่เป็นภาพสแกนถูกรายงานเป็นปัญหา (`no_text_layer`) เพราะ ATS จริงก็อ่านไม่ได้เช่นกัน
- ตรวจไฟล์จาก magic bytes ไม่ใช่นามสกุล, จำกัดขนาด 10 MB, ≤ 20 หน้า, กัน zip bomb ของ DOCX

### 3.2 Parseability audit (local, rule-based) — จำลองว่า ATS อ่านได้แค่ไหน  

| ตรวจ | ทำไม |
| --- | --- |
| หลายคอลัมน์ / ตาราง / text box | ATS หลายเจ้าอ่านลำดับผิด |
| ข้อมูลสำคัญใน header/footer | มักถูกข้าม |
| รูปภาพที่มีข้อความ, ไอคอนแทนคำ | อ่านไม่ได้ |
| **ข้อความเพี้ยน** (font ไม่มี Unicode map, `(cid:N)`, legacy Thai PUA U+F700–F71F, ลำดับสระ/วรรณยุกต์ผิด, ตัวอักษรแยกด้วยช่องว่าง) | ATS ก็อ่านเพี้ยนเหมือนกัน — ใช้ทั้งสัญญาณจาก PDFium (`HasUnicodeMapError`) และกฎอักขรวิธีไทยแบบ conservative |
| หัวข้อ section ไม่มาตรฐาน | ATS แยก section ไม่ได้ |
| ข้อความซ่อน (contrast กับพื้นหลังที่ render จริง < 0.12, render mode invisible, โปร่งใส, < 2 pt) | keyword stuffing → red flag; ตัวหนังสือขาวบน sidebar สีเข้มไม่ถูกนับ |
| วันที่รูปแบบไม่สม่ำเสมอ / พ.ศ. ปน ค.ศ. | คำนวณประสบการณ์ผิด |
| ไฟล์ใหญ่ / จำนวนหน้า | เกณฑ์ทั่วไป |

ผลลัพธ์: `parseability_score` 0–1 + รายการปัญหาพร้อมวิธีแก้

### 3.3 Redact (llm-gateway) — ดู 2.4

### 3.4 Structure resume (Claude)  

- Structured output (`output_config.format` JSON schema) → `{sections, experiences[{title, company, start, end, bullets[]}], education[], certifications[], skills_mentioned[], languages[]}`
- แปลง พ.ศ. → ค.ศ. และคำนวณระยะเวลา **ด้วยโค้ด** ไม่ให้ LLM คิดเลข
- ทุก bullet มี `bullet_id` อ้างกลับไปยัง text ต้นฉบับ (ใช้ตรวจ evidence)

### 3.5 JD parse (Claude)  

- แตก JD เป็น requirement ทีละข้อ: `{id, text, type: skill|experience|education|cert|language|soft, importance: required|preferred, min_years?, canonical_skill_ids[]}`
- ระบุ knockout criteria (เช่น "ต้องมีใบอนุญาต…", "ภาษาอังกฤษ TOEIC ≥ 750")
- cache ผลตาม hash ของ JD (JD เดียวกันใช้กับหลาย resume)

### 3.6 Skill normalization (bilingual)

- คลังคำ `ats_core/taxonomy/skills.toml` (เริ่มต้น ~100 skill ด้านเทคโนโลยี ข้อมูล ธุรกิจ soft skill และภาษา) — แต่ละ skill มีชื่อเรียกภาษาอังกฤษ ภาษาไทย และคำที่ต้องตรงตัวพิมพ์ (`Go`, `React`, `Excel`, `R`) เพราะตัวพิมพ์เล็กเป็นคำทั่วไป ("go to market", "react to", "excel at"); ESCO ใช้ขยายคลังคำภายหลัง
- ตอนโหลดตรวจว่า id ไม่ซ้ำ และไม่มีชื่อเรียกที่ชี้ไปสอง skill
- อังกฤษ: regex ที่เข้าใจขอบคำของ tech term (`Java` ≠ `JavaScript`, `C` ≠ `C++`/`C#`/`C-level`, `main.go` ไม่ใช่ Go, ช่องว่าง = ขีด)
- ไทย: ตัดคำด้วย PyThaiNLP (`newmm`) โดยใส่ชื่อเรียกไทยในพจนานุกรม แล้ว**ตรวจซ้ำว่าขอบของคำตรงกับการตัดคำอีกแบบ (`longest`)** เพื่อกันกรณีคำไปขโมยตัวอักษรของคำข้างเคียง ("จัดการบัญชีผู้ใช้" ≠ การบัญชี)

### 3.6b Keyword match (ATS-filter view, ไม่ใช้ Claude)

- แบ่ง JD ตามหัวข้อ: Requirements/คุณสมบัติ → required, Nice to have/จะพิจารณาเป็นพิเศษ → preferred, Benefits/สวัสดิการ/About us → ไม่นับ; คำบอกใบ้ระดับประโยค ("is a plus", "หากมี") ทำให้เป็น preferred เฉพาะประโยคนั้น
- skill ที่ required ที่ใดที่หนึ่ง = required
- รายงาน: coverage แยก required/preferred, skill ที่ขาด, skill ที่เจอแต่**ใช้คำพ้อง** (JD เขียน "K8s" แต่ resume เขียน "Kubernetes" → แนะนำให้ใส่คำตาม JD เพราะ ATS บางระบบเทียบคำตรงตัว), skill ที่มีใน resume แต่ JD ไม่ถาม
- เฟส 2 จะใช้ Claude แตก requirement ได้ละเอียดกว่า (ปีประสบการณ์, วุฒิ, knockout) ส่วนนี้ยังคงเป็นมุมมองของ ATS ที่กรองด้วย keyword

### 3.7 Match engine (hybrid, ราย requirement)  

| ตัวจับคู่ | วัดอะไร | เครื่องมือ |
| --- | --- | --- |
| a. Lexical | keyword ตรงตัว (สิ่งที่ ATS จริงทำ) | canonical skill id + BM25 (ไทยตัดคำแล้ว) |
| b. Semantic | ความหมายใกล้กัน ข้ามภาษาได้ | `BAAI/bge-m3` embeddings, ราย bullet |
| c. Rerank | เลือก top-k bullet ที่เป็นหลักฐานดีสุด | `BAAI/bge-reranker-v2-m3` |
| d. LLM Judge | requirement นี้ "met / partial / not_met" พร้อม `evidence_bullet_ids` + เหตุผล | Claude, structured output |
| e. Rules | ปีประสบการณ์, ระดับวุฒิ, cert, knockout | โค้ด |

**Evidence verification (สำคัญที่สุดต่อความแม่น):** ทุก `evidence_bullet_id` ที่ Claude อ้างต้องมีอยู่จริงและอยู่ใน top-k candidate ที่ส่งไป ถ้าไม่ผ่าน → ลดเป็น `not_met` และบันทึก `judge_inconsistency`

**Self-consistency (เฉพาะ requirement ที่ importance=required):** judge 2 ครั้งแยกกัน ถ้าผลไม่ตรงกัน → ครั้งที่ 3 เป็นตัวตัดสินและติดธง `low_confidence` ในรายงาน

### 3.8 Scoring  

```bash
req_score(r)  = {met: 1.0, partial: 0.5, not_met: 0}
required      = Σ req_score / |required|
preferred     = Σ req_score / |preferred|
experience    = min(1, actual_years / min_years)
lexical_cov   = % ของ required keywords ที่พบตรงตัว         ← มุมมอง ATS
semantic      = mean(top bullet similarity ต่อ requirement)

match = 0.30·required + 0.15·preferred + 0.15·experience
      + 0.15·lexical_cov + 0.10·education_cert + 0.15·semantic
final = match × (0.6 + 0.4·parseability)      ; knockout ไม่ผ่าน → ธงแดง ไม่ว่าคะแนนเท่าไร
```  

น้ำหนักเป็นค่าเริ่มต้น จะ calibrate ด้วย logistic regression จาก eval set (เฟส 3)

### 3.9 Report  

- คะแนนรวม + คะแนนย่อย 6 ด้าน + ATS parseability issues
- ตาราง requirement: สถานะ, หลักฐาน (quote), เหตุผล
- keyword ที่ขาด (แยก required/preferred) + ตำแหน่งที่ควรใส่
- ข้อเสนอ rewrite bullet (Claude) — ห้ามแต่งประสบการณ์ที่ไม่มี, แสดงเป็น "ข้อเสนอ" ให้ผู้ใช้ยืนยัน
- รายงานภาษาเดียวกับ JD หรือเลือกเองได้

---

## 4. การใช้ Claude API

| หัวข้อ | การตั้งค่า |
| --- | --- |
| Model | `claude-opus-5-5` (ค่าเริ่มต้น, แม่นยำสูงและราคาเหมาะสม) — กำหนดผ่าน config ของ gateway เปลี่ยนได้ เช่นเป็น `claude-fable-5-2` ถ้าต้องการเพดานความสามารถสูงสุดและยอมจ่ายแพงขึ้น |
| Thinking / effort | adaptive thinking (เปิดเสมอบน Opus 5.5), `output_config.effort`: `high` สำหรับ judge และ JD parse, `medium` สำหรับ structure resume — ตั้งค่าเองเสมอเพราะค่าเริ่มต้นของ Opus 5.5 คือ `medium` |
| Output | Structured outputs (`output_config.format` + JSON schema) ทุก task → Pydantic validate อีกชั้น |
| Citations | **ไม่ใช้** ฟีเจอร์ citations ของ API เพราะใช้ร่วมกับ structured outputs ไม่ได้ → ใช้ `bullet_id` + การตรวจแบบ deterministic แทน (แม่นกว่าและตรวจได้) |
| Prompt caching | system prompt + schema + JD (เมื่อ judge หลาย requirement ของ JD เดียวกัน) วางเป็น prefix คงที่ + `cache_control`; ตรวจ `usage.cache_read_input_tokens` |
| Refusal | ตรวจ `stop_reason == "refusal"` ก่อนอ่าน content, เปิด server-side `fallbacks: "default"` |
| Errors | SDK retry 2 ครั้งในตัว, จับ `RateLimitError` → backoff, `APIStatusError` 4xx → ไม่ retry, บันทึก request id |
| Batch | โหมด "ตรวจ resume หลายเวอร์ชันกับหลาย JD" ใช้ Message Batches (ถูกลง 50%) |
| Prompt files | เก็บใน `services/llm-gateway/prompts/` เวอร์ชันชัดเจน (`judge.v1.md`) — บันทึกเวอร์ชันลงผลทุกครั้งเพื่อเทียบ eval |

---

## 5. Data model (Postgres)

```bash
documents      (id, kind[resume|jd], sha256, lang, object_key, created_at, expires_at)
resume_parses  (id, document_id, structure_json, parseability_json, extractor_version)
jd_parses      (id, document_id, requirements_json, prompt_version, model)
bullets        (id, resume_parse_id, section, text, embedding vector(1024))
analyses       (id, resume_parse_id, jd_parse_id, status, scores_json, report_json,
                weights_version, created_at)
judgments      (id, analysis_id, requirement_id, verdict, evidence_bullet_ids[], rationale,
                confidence, votes)
llm_calls      (id, task, model, input_tokens, output_tokens, cache_read_tokens,
                latency_ms, request_id, created_at)        ← ไม่มี prompt/response
eval_cases     (id, resume_doc_id, jd_doc_id, human_score, human_labels_json)
```

---

## 6. โครงโปรเจกต์

```bash
Personal_ATS/
├── docs/ARCHITECTURE.md
├── packages/ats_core/          # logic ล้วน ทดสอบได้โดยไม่ต้องมี K8s
│   ├── extract/  parseability/  redact/  taxonomy/  match/  scoring/  report/
│   └── logging.py              # redaction filter กลาง
├── services/
│   ├── api/  ui/  worker/  embedder/
│   └── llm-gateway/            # ที่เดียวที่ import anthropic
│       └── prompts/
├── data/taxonomy/              # ESCO subset + aliases.yaml (ไทย/อังกฤษ)
├── eval/                       # cases/, labels/, run_eval.py
├── k8s/
│   ├── base/                   # deployments, services, netpol, rbac, psa
│   └── overlays/dev/
├── scripts/                    # cluster-up.sh, load-secret.sh, rotate-key.sh
├── Tiltfile
├── pyproject.toml / uv.lock    # uv workspace
├── .pre-commit-config.yaml     # gitleaks, ruff
└── .gitignore / .dockerignore
```

`anthropic` SDK อยู่ใน dependency ของ `llm-gateway` เท่านั้น — service อื่น import ไม่ได้ (บังคับด้วย test ที่ตรวจ dependency)

---

## 7. การวัดความแม่นยำ (Evaluation)

| สิ่งที่วัด | metric | เป้า v1 |
| --- | --- | --- |
| Skill extraction จาก JD | precision / recall เทียบ label มือ | ≥ 0.90 / ≥ 0.85 |
| Requirement judgment | accuracy เทียบ label มือ (met/partial/not_met) | ≥ 0.85 |
| Evidence validity | % evidence ที่ผ่านการตรวจ | ≥ 0.98 |
| คะแนนรวม | Spearman ρ กับคะแนนที่คนให้ | ≥ 0.75 |
| Parseability | ตรงกับผลทดสอบจริงบน ATS (เช่น resume parser ฟรี) | ≥ 0.80 |
| Redaction | recall ของ PII (ต้องไม่หลุด) | 1.00 บนชุดทดสอบ |

- ชุดข้อมูลเริ่มต้น: 30–50 คู่ resume–JD (ไทย/อังกฤษ/ปน) จาก resume ของตัวเองหลายเวอร์ชัน + JD จริง + resume ตัวอย่างสาธารณะ
- ทุกครั้งที่แก้ prompt / น้ำหนัก / โมเดล → รัน `eval/run_eval.py` และบันทึกผลเทียบเวอร์ชันก่อน

---

## 8. Dev workflow

```bash
# ติดตั้งครั้งแรก
k3d, kubectl, helm, tilt, uv, mkcert, gitleaks, trivy

scripts/cluster-up.sh     # k3d + secrets-encryption + localhost-only port + local registry
scripts/load-secret.sh    # keyring → K8s Secret ผ่าน stdin
tilt up                   # build + deploy + live reload
```

พัฒนา `ats_core` ด้วย `uv run pytest` ได้โดยไม่ต้องเปิด cluster

---

## 9. Roadmap

| เฟส | งาน | ผลลัพธ์ |
| --- | --- | --- |
| **0. Foundation** | git init, pre-commit (gitleaks), uv workspace, logging filter, threat model tests | repo ที่ปลอดภัยตั้งแต่ commit แรก |
| **1. Core local** | extract + parseability + redaction + lexical match + CLI | `ats check resume.pdf jd.txt` ทำงานได้โดยไม่ใช้ Claude |
| **2. Claude** | cost ledger, llm-gateway (unix socket, key จาก tmpfs), `atsctl start/stop`, JD parse, structure, judge, evidence verification, scoring, คิดเงินรายลูกค้า | รายงานเต็มผ่าน CLI + ยอดเงินต่องาน (ดู §11) |
| **3. Eval** | label ชุดข้อมูล, run_eval, calibrate น้ำหนัก | ตัวเลขความแม่นยำจริง |
| **4. Kubernetes** | k3d, manifests, NetworkPolicy, RBAC, PSA, egress-proxy, Tilt | ทั้งระบบรันใน cluster แบบ hardened |
| **5. UI & report** | Streamlit, login, export PDF, dashboard ค่าใช้จ่าย | ใช้งานจริงประจำวัน |

**Security checklist ก่อนใส่ key จริงครั้งแรก (จบเฟส 2):**  

- [ ] `gitleaks detect` ผ่านทั้ง history
- [ ] grep log ทั้งหมดหลังรัน pipeline ไม่เจอ `sk-ant-`
- [ ] Workspace แยก + spend limit ตั้งแล้วใน Console
- [ ] key อยู่ใน keyring เท่านั้น ไม่มีใน shell history (`history | grep sk-ant` ว่าง)
- [ ] test redaction ผ่าน 100% บนชุด PII

**เพิ่มเติมก่อนเฟส 4 จบ:**  

- [ ] `kubectl auth can-i get secrets --as=system:serviceaccount:ats:worker` → `no`
- [ ] จาก pod `worker`: `curl https://example.com` ต้อง fail, จาก gateway ผ่าน proxy ไป `example.com` ต้อง fail
- [ ] `kubectl get secret -o yaml` ไม่อยู่ในไฟล์ใดใน repo
- [ ] `trivy image` ไม่มี CRITICAL

---

## 10. Decision log

| # | ตัดสินใจ | เหตุผล | ทางเลือกที่ไม่เลือก |
| --- | --- | --- | --- |
| D1 | Claude API แทน local LLM | ความแม่นยำสูงสุด ทั้งไทยและอังกฤษ | Ollama 7–8B (ส่วนตัวกว่าแต่แม่นน้อยกว่า) |
| D2 | Gateway เดียวถือ key | ลด blast radius, ตรวจสอบง่าย | ให้ worker เรียก API เอง |
| D3 | Key: keyring → Secret (encrypted at rest) → file mount | ไม่มี key บน disk แบบ plaintext และไม่อยู่ใน git | SOPS+age ใน git (ซับซ้อนกว่าสำหรับคนเดียว), env var (หลุดง่ายกว่า) |
| D4 | Redact PII ก่อนส่ง | ข้อมูลติดต่อไม่ช่วยการ match แต่เพิ่มความเสี่ยง | ส่งทั้งฉบับ |
| D5 | ไม่ใช้ API citations, ใช้ bullet_id | ใช้ร่วมกับ structured outputs ได้ และตรวจแบบ deterministic | citations + parse ข้อความ |
| D6 | Embeddings local (bge-m3) | multilingual ดี, ไม่ต้องส่งข้อมูลออก | embedding API ภายนอก |
| D7 | k3d + egress proxy แทน Cilium | ติดตั้งง่ายบนเครื่องเดียว แต่ยังกรอง FQDN ได้ | Cilium FQDN policy (เรียนรู้เยอะกว่า) |
| D8 | PDFium (`pypdfium2`) ดึงข้อความ PDF | ทดสอบกับ PDF จริงจาก LibreOffice 6 font ไทย: pdfminer อ่านไทยเพี้ยน (cid/ตัวผิด) ทั้งที่ไฟล์ถูกต้อง ทำให้แจ้ง error ผิดกับ resume ที่ดี; PDFium อ่านตัวอักษรถูก 56/60 บรรทัด | pdfminer/pdfplumber (ไทยเพี้ยน), PyMuPDF (AGPL ไม่เข้ากับ MIT), poppler (GPL) |
| D9 | ตัดสินข้อความซ่อนด้วย contrast กับพื้นหลังที่ render จริง | กฎ "สีขาว = ซ่อน" แจ้งเตือนผิดกับ resume ที่มี sidebar สีเข้ม | ตรวจเฉพาะสีของตัวอักษร |
| D10 | ตรวจขอบคำไทยด้วยการตัดคำ `longest` ซ้ำอีกชั้น | `newmm` (ค่าเริ่มต้นของ PyThaiNLP) ตัด "จัดการบัญชีผู้ใช้" เป็น "จัด\|การบัญชี" ทำให้ได้ skill ผิด; วัดบน 19 ประโยคบวก + 5 ประโยคลบ: `longest` ไม่ทิ้ง true positive และตัด false positive ได้ | เชื่อ `newmm` อย่างเดียว |
| D11 | Operator mode: ผู้ใช้คนเดียวตรวจ resume ให้ลูกค้า แล้วคิดเงินตามต้นทุนจริงรายงาน | ตรงกับการใช้งานจริง ไม่ต้องเปิดระบบออกนอกเครื่อง | ระบบหลายผู้ใช้ (ต้องมี auth, แยกข้อมูล, เปิด network) |
| D12 | gateway ฟังบน **unix socket** (สิทธิ์ 0600) ไม่ใช่ TCP | ไม่มี port ให้โปรแกรมอื่นหรือเครื่องอื่นเชื่อมต่อ มีแค่ผู้ใช้เจ้าของไฟล์ | TCP บน 127.0.0.1 (process ไหนในเครื่องก็ต่อได้) |
| D13 | key อยู่บน tmpfs เฉพาะช่วง `atsctl start` และลบทิ้งทันทีเมื่อ gateway โหลดเสร็จ | key ไม่ค้างบน disk แม้แต่ใน RAM disk ระหว่างรัน; stop แล้วไม่เหลือร่องรอย | key file ค้างตลอดเวลาที่รัน |
| D14 | Cost ledger เป็น SQLite + Alembic ในเฟส 2, ย้ายไป Postgres ในเฟส 4 | ใช้ได้ทันทีโดยไม่ต้องมี infra; migration ทำให้ย้ายฐานข้อมูลได้ | Postgres ตั้งแต่แรก (ต้องรัน container ก่อนใช้) |
| D15 | เงินเป็น `Decimal` ทั้งหมด, ราคาเก็บเป็น config พร้อมแหล่งที่มาและวันที่, คิดตาม model ที่ตอบจริง | คิดเงินลูกค้าต้องตรวจย้อนได้และไม่มีเศษปัดเพี้ยน | float, ราคา hard-code |
| D16 | ไม่ลบข้อมูลลูกค้าอัตโนมัติ — ลบด้วยคำสั่งเท่านั้น | ผู้ใช้ต้องการเก็บไว้และจัดการเอง | retention job อัตโนมัติ |

---

## 11. เฟส 2: รายละเอียดการออกแบบ

### 11.1 การใช้งานแบบ operator

ผู้ใช้คนเดียว (เจ้าของเครื่อง) ตรวจ resume ที่คนอื่นนำมาให้ แล้วคิดเงินตามต้นทุน API จริงของงานนั้น

```bash
atsctl start                                        # เปิดระบบ (โหลด key, เปิด gateway)
ats check resume.pdf --jd job.txt --client "น้องเอ"  # แสดงราคาประมาณการ → ยืนยัน → ตรวจ
ats costs --client "น้องเอ"                          # ต้นทุนแยกรายงาน + รวม
ats costs --month 2026-12 --fx 36.20 --markup 50% --csv > bill.csv
ats forget --client "น้องเอ"                         # ลบข้อมูลของลูกค้าคนนี้ (ทำเอง ไม่มีลบอัตโนมัติ)
atsctl stop                                         # ปิดระบบ คืนทรัพยากร ไม่เหลือ key
```

### 11.2 `atsctl` — start / stop ทั้งโปรเจกต์

| คำสั่ง | สิ่งที่ทำ |
| --- | --- |
| `start` | 1) ตรวจ preflight: key มีใน keyring, `$XDG_RUNTIME_DIR` เป็น tmpfs, DB migrate ล่าสุด, ไม่มี gateway ค้าง · 2) เขียน key ลงไฟล์ใน `$XDG_RUNTIME_DIR/personal-ats/` (dir 0700, file 0400) · 3) เปิด gateway แบบ background พร้อม pid file · 4) รอ health check ผ่าน unix socket (timeout 15 s) · 5) **ลบไฟล์ key ทันที** (gateway ถือ key ไว้ใน memory เท่านั้น) |
| `stop` | SIGTERM → รองาน in-flight จบ (สูงสุด 30 s) → SIGKILL ถ้าจำเป็น · ลบ pid, socket และไฟล์ key ที่อาจค้าง · แสดงยอดเงินวันนี้ |
| `status` | running/stopped, pid, uptime, health, ยอดเงินวันนี้เทียบงบ $2, เวอร์ชัน |
| `restart` | `stop` แล้ว `start` |
| `logs` | แสดง log ของ gateway (ผ่าน `SecretRedactingFilter` แล้ว ไม่มีเนื้อหา resume) |

- ทุกคำสั่ง **idempotent**: `start` ตอนรันอยู่แล้ว หรือ `stop` ตอนหยุดอยู่แล้ว ไม่ error และไม่ทำซ้ำ
- ถ้า gateway crash จะไม่ restart เอง เพราะไม่มี key แล้ว (fail closed) ผู้ใช้สั่ง `atsctl start` ใหม่
- `ats check` ยังใช้งานส่วนที่ไม่ใช้ AI ได้แม้ระบบหยุดอยู่ ส่วนที่ต้องใช้ AI จะแจ้งให้รัน `atsctl start`
- **เฟส 4 (k3d):** คำสั่งเดิม แต่ `start` = `k3d cluster start` + สร้าง Secret จาก keyring + รอ pod พร้อม และ `stop` = **ลบ Secret ก่อน** (key ไม่ค้างใน etcd ระหว่างหยุด) แล้ว `k3d cluster stop` โดยข้อมูลใน volume ยังอยู่

### 11.3 Cost ledger

**ตารางราคา** `ats_core/billing/pricing.toml`: ราคาต่อล้าน token แยก input / output / cache write / cache read ต่อ model พร้อม `source` (URL หน้าราคา) และ `as_of` (วันที่ตรวจ) — ทุกแถวของ `llm_calls` เก็บ `pricing_version` ที่ใช้คำนวณ

**ต้นทุนต่อ call** = Σ (token แต่ละประเภท × ราคาประเภทนั้น) ของ **model ที่ตอบจริง** (`response.model` — กรณี fallback อาจไม่ใช่ model ที่ขอ) · thinking token นับเป็น output · call ที่ retry หรือถูกปฏิเสธก็นับ เพราะเสียเงินจริง

**Schema (SQLite + Alembic, ย้ายไป Postgres ได้ในเฟส 4):**

```
clients    (id, nickname, created_at, forgotten_at)
analyses   (id, client_id, created_at, resume_sha256, jd_sha256, status, prompt_versions, cost_usd)
llm_calls  (id, analysis_id, client_id, task, requested_model, model, effort,
            input_tokens, output_tokens, cache_write_tokens, cache_read_tokens,
            cost_usd, pricing_version, latency_ms, stop_reason, request_id, attempt, created_at)
invoices   (id, client_id, period, cost_usd, fx_rate, markup_pct, total_thb, created_at)  ← snapshot ไม่เปลี่ยนย้อนหลัง
```

- ชื่อลูกค้าอยู่ใน `clients.nickname` ที่เดียว ตารางอื่นใช้ `client_id`
- `ats forget` ลบ nickname, ไฟล์ และผลวิเคราะห์ แต่เก็บแถวตัวเลขใน `llm_calls`/`invoices` ไว้แบบไม่ระบุตัวตนเพื่อบัญชีของผู้ใช้

**ประมาณราคาก่อนรัน:** input นับด้วย `count_tokens` (แม่น, ไม่เสียเงิน) + output ใช้ค่า p75 ของงานประเภทเดียวกันในอดีต (ก่อนมีประวัติใช้ค่าเริ่มต้นที่ระวังไว้สูง) → แสดงเป็นช่วง และต้องไม่เกินงบที่เหลือของวัน

**งบรายวัน:** $2 ต่อวันตามเวลา Asia/Bangkok · ถ้า ยอดวันนี้ + ประมาณการ > งบ → ไม่เริ่มงาน · ระหว่างงาน gateway ตรวจทุก call

**กระทบยอด:** `ats costs reconcile --month` เทียบยอดของเรากับยอดจาก Anthropic Console (กรอกเอง) ถ้าต่างเกิน 2% แจ้งเตือน เพื่อไม่ให้คิดเงินลูกค้าเกินหรือขาด

**Data science:** `ats costs export --parquet` → วิเคราะห์ด้วย DuckDB/notebook: เงินต่องาน, cache hit rate, latency, และ (เฟส 3) ความแม่นยำต่อเงินหนึ่งดอลลาร์เมื่อเปลี่ยน prompt/effort

### 11.4 ลำดับ PR ของเฟส 2

| # | PR | ต้องใช้ key จริงไหม |
| --- | --- | --- |
| 2.1 | ตารางราคา + ตัวคำนวณต้นทุน (Decimal) + ledger schema + งบรายวัน | ไม่ |
| 2.2 | `llm-gateway`: unix socket, โหลด key จากไฟล์, PII guard แบบ fail closed, Anthropic wrapper (ทดสอบด้วย client ปลอม), บันทึกต้นทุนทุก call | ไม่ |
| 2.3 | `atsctl start/stop/status/restart/logs` | ไม่ (ทดสอบด้วย key ปลอม) |
| 2.4 | งานของ Claude: แตก JD, จัดโครง resume, judge + ตรวจหลักฐาน + รวมคะแนน | ไม่ (client ปลอม) |
| 2.5 | `--client`, ประมาณราคา + ยืนยัน, `ats costs`, `ats forget`, export | ไม่ |
| 2.6 | security checklist (§9) + smoke test ครั้งแรกกับ key จริง | **ใช่** — ผู้ใช้สร้าง key และเก็บลง keyring เอง |
