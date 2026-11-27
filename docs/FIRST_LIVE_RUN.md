# ใช้ API key จริงครั้งแรก (runbook)

ทำตามลำดับ ทุกขั้นที่แตะ key **คุณทำเองในเทอร์มินัล** ไม่มีขั้นไหนที่ key จะผ่าน chat, ไฟล์ในโปรเจกต์ หรือ shell history

ค่าใช้จ่ายของรอบทดสอบ: ประมาณ 0.05–0.20 USD (ระบบจะแสดงราคาประมาณการและถามก่อนตัดสินทุกครั้ง)

## 1. ตรวจก่อนมี key

```bash
uv run python scripts/security_checklist.py
```

ทุกข้อที่เป็น `[PASS]` ต้องผ่าน ส่วน `[MANUAL]` คือขั้นที่ต้องทำเองด้านล่าง

## 2. สร้าง key ใน Anthropic Console

1. สร้าง Workspace แยกชื่อ `personal-ats`
2. ตั้ง **monthly spend limit** ของ workspace นี้ (เช่น 10 USD) เป็นด่านสุดท้ายถ้าทุกอย่างพัง
3. สร้าง API key **ใน workspace นี้** แล้ว copy ไว้ (ยังไม่ต้องวางที่ไหน)

## 3. เก็บ key ลง keyring

```bash
secret-tool store --label='Anthropic personal-ats' service anthropic account personal-ats
```

คำสั่งจะถาม `Password:` ให้วาง key ตรงนั้น (ไม่แสดงบนจอ และไม่เข้า history เพราะไม่ได้อยู่ในบรรทัดคำสั่ง)
**ห้าม** ใส่ key ใน `export`, ไฟล์ `.env` หรือ argument ของคำสั่งใดๆ

## 4. เปิด gateway

```bash
uv run atsctl start
uv run atsctl status
```

ตอนเปิด gateway จะตรวจกับ Models API (ไม่เสียเงิน) ว่า key ใช้ได้ และโมเดล fallback
`claude-opus-5` อยู่ในรายการที่ API อนุญาตสำหรับ `claude-opus-5-5`
ถ้าไม่ผ่าน gateway จะไม่เปิด และ `atsctl logs` จะบอกเหตุผล (ไม่มี key ใน log)

## 5. ทดสอบด้วยข้อมูลสมมติ

resume ตัวอย่างเป็นบุคคลสมมติ ไม่ใช้ resume ของใครจริงในรอบแรก

```bash
uv run ats check packages/ats_core/tests/fixtures/libreoffice/en_clean.pdf \
    --jd examples/jd_backend_en.txt --ai --show-redacted
```

สิ่งที่ต้องดู:
- ข้อความหลังเส้นคั่น (สิ่งที่ส่งให้ AI) ต้องไม่มีชื่อ อีเมล เบอร์โทร
- ราคาประมาณการขึ้นก่อน แล้วถาม `[y/N]`
- รายงานมีคะแนนพร้อมหลักฐานรายข้อ

## 6. ตรวจยอดเงินให้ตรงกัน

```bash
uv run ats costs
uv run atsctl status
```

เทียบกับหน้า Usage / Cost ใน Console ของ workspace `personal-ats` (Console อาจอัปเดตช้ากว่าไม่กี่นาที)
ถ้าต่างกันเกินเศษสตางค์ให้หยุดใช้ และเปิด issue พร้อมผลของ `ats costs --csv`

## 7. ปิดและตรวจหลังใช้งาน

```bash
uv run atsctl stop
uv run python scripts/security_checklist.py --after-run
```

`--after-run` สแกน log และข้อมูลในเครื่องทั้งหมดว่าไม่มีอะไรหน้าตาเหมือน key และไม่มีไฟล์ key ค้างใน runtime directory

## ถ้าสงสัยว่า key หลุด

1. Console → ลบ (revoke) key ทันที ใช้เวลาไม่กี่วินาที
2. `secret-tool clear service anthropic account personal-ats`
3. สร้าง key ใหม่แล้วเริ่มที่ขั้น 3
