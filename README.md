# LifeLink — Emergency Medical Identity System

> **A Biometric Healthcare & Clinical Decision Platform for Raspberry Pi 5 Touchscreen Displays**

LifeLink is a medical-grade emergency healthcare identity and clinical retrieval application built for hospital trauma bays, emergency departments, field clinics, and ambulances. It pairs 10-digit optical fingerprint enrollment and bilateral dual-NIR iris authentication with instant electronic health record (EHR) retrieval and AI-powered medical summarization.

---

## 🏥 Design Philosophy & System Aesthetics

LifeLink is engineered to look and feel like **certified medical equipment**, **government healthcare infrastructure**, and an **enterprise hospital terminal**:

- **Visual Inspirations:** Swiss International Typographic Style, Windows XP Professional high-contrast ledger lines, Modern Apple Human Interface Guidelines, and Medical Device Dashboards.
- **Strictly Avoids:** Neon colors, glassmorphism, gaming aesthetics, consumer startup dashboards, and extraneous animations.
- **Primary Design Tokens:**
  - **Background:** `#FAFAFA`
  - **Primary Text:** `#1A1A1A`
  - **Secondary Text:** `#666666`
  - **Border:** `#D9D9D9`
  - **Primary Button:** `#1A1A1A` with `#FFFFFF` text (tactile metallic gradient)
  - **Success / Validated:** `#3A7A3A`
  - **Warning / Review:** `#B58B00`
  - **Critical Alert / Anaphylaxis:** `#A12A2A`
- **Typography:** Inter, a single professional sans-serif typeface used throughout the entire interface (fallback: Calibri).
- **Touchscreen Ergonomics:** Generous touch targets (48px+ minimum height), zero keyboard reliance required, rapid 1-tap selectors, and persistent Back button on every screen.

---

## 📁 Project Structure

LifeLink is a **single-file application**: every page, style, and script lives inline in `index.html`, so the whole app can be copied, shared, or deployed as one file (only the Tailwind CDN script and Google Fonts load externally).

```
life link gui/
├── index.html          # The entire app — all 13 page views, inline <style> (design
│                        # tokens & component CSS) and inline <script> modules
│                        # (store, router, biometrics, photo-capture, ai-summary,
│                        # records, settings, app) in load order
├── server.py            # Local static HTTP server (Raspberry Pi 5 & desktop)
├── README.md
└── design-reference/
    └── lifelink-5inch/  # Static HTML mockups for the 5" touchscreen layout
```

---

## 🧭 Complete Application Flow (13 Dedicated Pages)

```
Home (Page 1)
│
├── Register Patient
│   ├── Step 1: Fingerprint Enrollment (Page 2) — 10 Digits (5 Left, 5 Right)
│   ├── Step 2: Iris Enrollment (Page 3) — Bilateral (OD & OS) NIR scan
│   ├── Step 3: Patient Photo Capture (Page 4) — Live webcam identity photo
│   ├── Step 4: Patient Information (Page 5) — Demographics & Emergency Contact
│   ├── Step 5: Medical Information (Page 6) — Allergies, Conditions, Meds, Surgeries
│   ├── Step 6: Medical Summary (Page 7) — Pre-commit Clinical Review
│   └── Step 7: Review & Create Profile (Page 8) — FIPS 140-3 Enclave Commit
│
├── Existing Patient
│   ├── Biometric Authentication (Page 9) — Optical Platen / Dual-NIR Iris
│   ├── Patient Dashboard (Page 10) — Executive Masthead, Triage Alerts, Regimens
│   ├── AI Medical Summary (Page 11) — 30-Second Executive Medical Briefing
│   └── Full Medical Records (Page 12) — 8-Category Clinical Repository & Search
│
└── Settings & Diagnostics (Page 13)
    ├── Cloud Connectivity (Latency & Ping)
    ├── Encrypted Local Database Status
    ├── Fingerprint 500 DPI Platen Calibration
    ├── Dual-NIR 850nm Strobe Diagnostics
    ├── FIPS 140-3 Hardware Enclave Key Rotation
    ├── JSON Database Backup & Restore
    ├── Operator Admin Access & Security Audit Log
    └── Raspberry Pi 5 Host Telemetry
```

---

## ⚡ Key Features

1. **10-Digit Dermal Ridge Capture (Page 2)**
   - Tracks all 10 fingers (Right Thumb `R1` to Right Little `R5`, Left Thumb `L1` to Left Little `L5`).
   - Interactive scanning sweep with real-time ridge minutiae extraction and quality score calculation (NFIQ-1 / ISO/IEC 19794-2).
   - "Fast Auto-Capture All 10 Digits" button for rapid touch testing.

2. **Dual-NIR Iris Capture (Page 3)**
   - Ocular progression: Oculus Dexter (Right) & Oculus Sinister (Left).
   - Dynamic vector reticle with millimeter alignment guides, pupil dilation tracking, and 850nm NIR strobe simulation.
   - ISO 19794-6 compliance score and Hamming distance verification.

3. **Live Patient Photo Capture (Page 4)**
   - Front-facing webcam capture with a live preview, circular alignment guide, and one-tap retake.
   - Captured photo is staged on the registration draft and committed to the patient's profile on enclave commit.
   - Graceful fallback ("Skip for now") when no camera is available or permission is denied.
   - Photo appears as a medical-record-style ID thumbnail on the Medical Summary review, the Review & Create commit preview, the Patient Dashboard masthead, the AI Medical Summary overview, and Full Medical Records.

4. **Clinical Anamnesis Matrix (Pages 5 & 6)**
   - Patient demographics with auto-calculated age from DOB.
   - Categorized Allergies (Drug, Food, Environmental) with severity tagging (*Mild, Moderate, Severe, Anaphylaxis*).
   - One-tap toggle chips for Chronic Conditions (*Diabetes, Hypertension, Asthma, Heart Disease, Kidney Disease, Liver Disease, Cancer, Epilepsy*) plus custom additions.
   - Dynamic medication and surgical history tables (add/remove row items on touch).
   - Medical device indicators (*Pacemaker, Insulin Pump, Stents, Implants*).

5. **Cryptographic Secure Enclave Commit (Page 8)**
   - Validates all 4 nodes before commit:
     - ✓ Fingerprints Logged (10/10)
     - ✓ Dual Iris Bilateral Scan (OD/OS)
     - ✓ Demographic Manifest Complete
     - ✓ Clinical Directives Authenticated
   - Generates unique Patient ID (`LL-XXXX-X`) and commits encrypted SHA-256 hash to local storage.

6. **Biometric Authentication & Existing Patient Retrieval (Page 9)**
   - Interactive tactile fingerprint and iris scan triggers.
   - Fallback emergency MRN search & 1-tap reference profile selector.
   - Preloaded reference profiles:
     - **Eleanor R. Vance** (`LL-8942-A`): Type 1 Diabetes, Pacemaker, Penicillin Anaphylaxis.
     - **Marcus Chen** (`LL-6120-B`): Coronary Artery Disease, Stents, Contrast Dye Anaphylaxis.
     - **Sarah Jenkins** (`LL-3401-C`): Severe Persistent Asthma, Latex Anaphylaxis.

7. **Executive Patient Dashboard (Page 10)**
   - Prominent Emergency Blood Group Callout (e.g. `O-`).
   - High-visibility red Level 1 Allergy Alert Card.
   - Active pharmacological regimen with dosage, frequency, and adherence indicator.
   - Emergency contact and directives banner.

8. **AI Medical Summary — 30-Second Triage Briefing (Page 11)**
   - Clean, authoritative medical report synthesized on the fly for any active patient.
   - Highlights absolute contraindications (e.g. Beta-Lactam exclusions for Penicillin-allergic patients).
   - Provides concrete clinical action directives for first responders and trauma bay teams.
   - Synthesis confidence score, latency telemetry, and one-click printable dossier layout.

9. **Full Medical Records Repository (Page 12)**
   - Search across 8 clinical categories:
     1. Visits (ER trauma, cardiology checkup, outpatient)
     2. Diagnoses (ICD-10 coded conditions)
     3. Prescriptions (RxNorm codes, dosage, refill count)
     4. Lab Reports (CBC, BMP, HbA1c, Cardiac Troponin with reference ranges)
     5. Surgeries (Operative notes, surgeons, hospitals)
     6. Vaccinations (COVID-19, Flu, Tdap, lot numbers)
     7. Scans (Radiology reports: Chest X-Ray, CT, MRI)
     8. Discharge Summaries (Inpatient hospital courses)
   - Real-time search filter and interactive "Inspect Dossier" report modal.
   - "+ Add Clinical Record" modal to enter new encounters directly from the terminal.

10. **Terminal Settings & Diagnostics (Page 13)**
   - Live hardware status and interactive diagnostics tests:
     - Cloud connectivity ping test
     - Database integrity block verification
     - Optical sensor platen calibration
     - Dual-NIR illuminator strobe test
     - Ephemeral encryption key rotation
     - JSON database export & restore
     - Real-time operator audit log

---

## 🚀 Running on Raspberry Pi 5 / Any Computer

### Method 1: Local HTTP Server (Recommended)
```bash
python3 server.py 3000
```
Open your browser at `http://localhost:3000`.

### Method 2: Direct File Loading
Open `index.html` directly in any Chromium / Firefox / Safari browser.

### Raspberry Pi 5 Touchscreen Kiosk Mode:
To launch LifeLink in full-screen kiosk mode on the official 10.1" or 7" touchscreen:
```bash
chromium-browser --kiosk --noerrdialogs --disable-infobars --check-for-update-interval=31536000 http://localhost:3000
```

### Hardware Terminal Hotkeys:
- **`[ESC]`**: Return to Previous Screen / Dismiss Active Modal
- **`[F1]`**: Quick Launch Patient Registration
- **`[F2]`**: Quick Launch Patient Identification
- **`[F3]`**: Open Terminal Settings & Diagnostics
- **`[F5]` / `[ENTER]`**: Trigger Scanner / Commit Record

---

## 🔒 Security & Medical Standards Compliance
- **Biometrics:** ISO/IEC 19794-2 (Fingerprint Minutiae), ISO/IEC 19794-6 (Iris Images)
- **EHR Interoperability:** HL7 FHIR R4 Schema, ICD-10-CM, RxNorm, LOINC
- **Encryption:** FIPS 140-3 Hardware Security Module (AES-256-GCM, SHA-256)
