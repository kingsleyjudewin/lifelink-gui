#!/usr/bin/env python3
"""
One-time seed: loads the 3 reference patients (Eleanor Vance, Marcus Chen,
Sarah Jenkins) that used to live in the frontend's localStorage seed data
into the real backend, via the running Flask API.

Run backend/app.py first, then:  backend/.venv/bin/python backend/seed.py
Safe to re-run — skips any patient id that already exists.
"""

import sys
import requests

API = "http://localhost:3000/api"

PATIENTS = [
    {
        "id": "LL-8942-A",
        "fullName": "Eleanor R. Vance",
        "dob": "1983-04-12",
        "gender": "Female",
        "bloodGroup": "O-",
        "phone": "+1 (555) 382-9014",
        "email": "e.vance@clinicalnet.org",
        "emergencyContact": "Thomas Vance (Spouse)",
        "emergencyPhone": "+1 (555) 492-8810",
        "address": "442 Beacon Crest Way, Boston, MA 02116",
        "nationalId": "SSN: ***-**-4912",
        "photoDataUrl": None,
        "allergies": {
            "drug": [
                {"name": "Penicillin", "severity": "Anaphylaxis", "note": "Severe bronchospasm & angioedema. Level 1 exclusion."},
                {"name": "Sulfa Antibiotics", "severity": "Severe", "note": "Urticaria, extensive erythema."},
            ],
            "food": [
                {"name": "Peanuts", "severity": "Moderate", "note": "GI distress, facial swelling."},
            ],
            "environmental": [
                {"name": "Latex", "severity": "Severe", "note": "Contact dermatitis and respiratory hypersensitivity."},
            ],
        },
        "chronicConditions": [
            "Type 1 Diabetes Mellitus",
            "Hypertension",
            "Sick Sinus Syndrome (Cardiac Arrhythmia)",
        ],
        "medications": [
            {"name": "Insulin Glargine", "dosage": "24 Units", "frequency": "SubQ QHS", "purpose": "Basal Glycemic Control"},
            {"name": "Insulin Lispro", "dosage": "Sliding Scale", "frequency": "SubQ AC Meals", "purpose": "Prandial Glycemic Control"},
            {"name": "Lisinopril", "dosage": "10 mg", "frequency": "Oral QD", "purpose": "Renoprotective & BP Control"},
            {"name": "Atorvastatin", "dosage": "20 mg", "frequency": "Oral QHS", "purpose": "Cardiovascular Lipid Prophylaxis"},
        ],
        "surgeries": [
            {"name": "Dual-Chamber Pacemaker Implantation", "date": "2022-08-14", "hospital": "Massachusetts General Hospital"},
            {"name": "Appendectomy (Laparoscopic)", "date": "2015-03-22", "hospital": "Brigham and Women's Hospital"},
        ],
        "medicalDevices": [
            "Pacemaker (Medtronic Azure XT DR MRI)",
            "Insulin Pump (Tandem t:slim X2)",
        ],
        "emergencyNotes": "REQUIRES STRICT INSULIN MONITORING. High cardiac vulnerability. MRI restricted to 1.5T with pacemaker programmer standby. Absolute beta-lactam contraindication.",
        "records": [
            {"category": "Visits", "date": "2026-08-19", "title": "Emergency Department Encounter — Acute Hypoglycemia",
             "doctor": "Dr. Sarah Thornton, MD (Attending Emergency Physician)", "facility": "Boston Metro Trauma Center",
             "summary": "Patient presented with diaphoresis and altered sensorium following insulin dosing error. Fingerstick glucose 38 mg/dL. Resolved with IV D10W bolus. Pacemaker interrogated: normal sinus capture 72 bpm.",
             "code": "ICD-10: E10.649", "badge": "ER Trauma Bay"},
            {"category": "Diagnoses", "date": "2022-07-11", "title": "Sick Sinus Syndrome & Tachy-Brady Arrhythmia",
             "doctor": "Dr. Robert Sterling, FACC (Cardiac Electrophysiology)", "facility": "MGH Heart Center",
             "summary": "Symptomatic sinus pauses exceeding 3.8 seconds documented on Holter monitor. Recommended permanent pacemaker placement.",
             "code": "ICD-10: I49.5", "badge": "Chronic"},
            {"category": "Prescriptions", "date": "2026-09-01", "title": "Insulin Glargine 100 U/mL SoloStar Pen",
             "doctor": "Dr. Miriam Patel, MD (Endocrinology)", "facility": "MGH Ambulatory Care",
             "summary": "Inject 24 units subcutaneous once daily at bedtime. 5 pens / 30 day supply. Refills: 3.",
             "code": "RxNorm: 285018", "badge": "Active Rx"},
            {"category": "Lab Reports", "date": "2026-09-15", "title": "Comprehensive Metabolic Panel & Glycated Hemoglobin (HbA1c)",
             "doctor": "Quest Diagnostics Clinical Pathology", "facility": "Boston Central Lab",
             "summary": "HbA1c: 6.8% (Target < 7.0%). Fasting Glucose: 112 mg/dL. Creatinine: 0.92 mg/dL (Normal). eGFR: > 90 mL/min/1.73m². Potassium: 4.2 mmol/L.",
             "code": "LOINC: 4548-4 / 24323-8", "badge": "Target Reached"},
            {"category": "Surgeries", "date": "2022-08-14", "title": "Permanent Dual-Chamber Pacemaker Implantation",
             "doctor": "Dr. Robert Sterling, MD (Primary Surgeon)", "facility": "MGH Operating Room 04",
             "summary": "Transvenous implantation of Medtronic Azure XT DR MRI dual-chamber pacemaker via left cephalic vein cutdown. RA and RV lead pacing thresholds and impedance optimal. Post-op CXR confirmed stable lead positioning.",
             "code": "CPT: 33208", "badge": "Operative Note"},
            {"category": "Vaccinations", "date": "2025-10-10", "title": "Influenza Quadrivalent & COVID-19 mRNA Booster",
             "doctor": "Clinical Health Hub", "facility": "Public Health Station 12",
             "summary": "Administered 0.5 mL IM left deltoid (Flu) and right deltoid (COVID-19). Lot #KJ-99214. No adverse reactions.",
             "code": "CVX: 197, 229", "badge": "Immunized"},
            {"category": "Scans", "date": "2026-08-19", "title": "Portable Chest Radiograph (1 View, AP Erect)",
             "doctor": "Dr. Alan Weiss, MD (Radiology)", "facility": "Trauma Imaging Suite B",
             "summary": "Dual-chamber pacemaker generator in left prepectoral pocket. RA and RV leads intact with normal trajectory. No pneumothorax, effusion, or acute pulmonary infiltrate.",
             "code": "CPT: 71045", "badge": "Radiology Final"},
            {"category": "Discharge Summaries", "date": "2026-08-19", "title": "Hospital Emergency Discharge Summary",
             "doctor": "Dr. Sarah Thornton, MD", "facility": "Boston Metro Trauma Center",
             "summary": "Discharged in stable condition following glycemic stabilization. Follow-up scheduled with endocrinologist. Pacemaker telemetry verified. Strict avoidance of beta-lactams reiterated.",
             "code": "CMS Discharge Code: 01", "badge": "Discharge"},
        ],
    },
    {
        "id": "LL-6120-B",
        "fullName": "Marcus Chen",
        "dob": "1967-11-04",
        "gender": "Male",
        "bloodGroup": "A+",
        "phone": "+1 (555) 720-4199",
        "email": "m.chen@baytech.org",
        "emergencyContact": "Lily Chen (Daughter)",
        "emergencyPhone": "+1 (555) 720-4101",
        "address": "88 Harborview Blvd, Cambridge, MA 02142",
        "nationalId": "SSN: ***-**-8120",
        "photoDataUrl": None,
        "allergies": {
            "drug": [
                {"name": "Iodinated Contrast Media", "severity": "Anaphylaxis", "note": "Severe hemodynamic collapse during 2021 cath lab procedure."},
            ],
            "food": [
                {"name": "Shellfish", "severity": "Severe", "note": "Laryngeal edema, hives."},
            ],
            "environmental": [],
        },
        "chronicConditions": ["Coronary Artery Disease", "Hypertension", "Hyperlipidemia"],
        "medications": [
            {"name": "Clopidogrel", "dosage": "75 mg", "frequency": "Oral QD", "purpose": "Antiplatelet Therapy (Stent Patency)"},
            {"name": "Atorvastatin", "dosage": "40 mg", "frequency": "Oral QHS", "purpose": "Plaque Stabilization"},
            {"name": "Metoprolol Succinate", "dosage": "50 mg", "frequency": "Oral QD", "purpose": "Rate Control & Ischemia Protection"},
        ],
        "surgeries": [
            {"name": "Percutaneous Coronary Intervention (DES x2)", "date": "2021-05-18", "hospital": "Brigham and Women's Hospital"},
        ],
        "medicalDevices": ["Coronary Stents (LAD & LCx Drug-Eluting Stents)"],
        "emergencyNotes": "CONTRAST DYE ALLERGY. Requires steroid/Benadryl premedication protocol before any non-emergent iodinated imaging. On dual antiplatelet therapy.",
        "records": [
            {"category": "Visits", "date": "2026-06-10", "title": "Cardiology Follow-Up & Treadmill Stress Test",
             "doctor": "Dr. Kevin Zhao, MD", "facility": "Brigham Heart & Vascular",
             "summary": "Asymptomatic at 8 METs exercise workload. No ST deviations. Blood pressure appropriately controlled on current regimen.",
             "code": "ICD-10: I25.10", "badge": "Cardiology"},
            {"category": "Surgeries", "date": "2021-05-18", "title": "Drug-Eluting Stent Implantation (LAD / Circumflex)",
             "doctor": "Dr. Linda Ross, MD", "facility": "Brigham Cath Lab 2",
             "summary": "Successful deployment of two Onyx DES stents into proximal LAD (90% stenosis) and mid-LCx (85% stenosis). TIMI 3 flow restored.",
             "code": "CPT: 92928", "badge": "Operative Note"},
        ],
    },
    {
        "id": "LL-3401-C",
        "fullName": "Sarah Jenkins",
        "dob": "1997-02-18",
        "gender": "Female",
        "bloodGroup": "B+",
        "phone": "+1 (555) 914-6022",
        "email": "sarah.j@outlook.com",
        "emergencyContact": "David Jenkins (Father)",
        "emergencyPhone": "+1 (555) 914-6000",
        "address": "12 Commonwealth Ave, Somerville, MA 02143",
        "nationalId": "SSN: ***-**-3401",
        "photoDataUrl": None,
        "allergies": {
            "drug": [],
            "food": [
                {"name": "Tree Nuts", "severity": "Severe", "note": "Bronchospasm within 10 minutes."},
            ],
            "environmental": [
                {"name": "Latex", "severity": "Anaphylaxis", "note": "Severe contact urticaria and laryngeal spasm."},
            ],
        },
        "chronicConditions": ["Severe Persistent Asthma"],
        "medications": [
            {"name": "Budesonide / Formoterol (Symbicort)", "dosage": "160/4.5 mcg", "frequency": "2 Inhalations BID", "purpose": "Maintenance Bronchodilation & Anti-inflammatory"},
            {"name": "Albuterol HFA Inhaler", "dosage": "90 mcg", "frequency": "2 Puffs Q4H PRN", "purpose": "Rescue Bronchodilation"},
        ],
        "surgeries": [],
        "medicalDevices": [],
        "emergencyNotes": "LATEX ANAPHYLAXIS. Use strictly latex-free surgical gloves, tourniquets, and IV sets. Asthma exacerbation protocol: high-dose nebulized bronchodilators.",
        "records": [
            {"category": "Visits", "date": "2026-05-14", "title": "Pulmonary Function Testing (Spirometry)",
             "doctor": "Dr. Rebecca Stone, MD", "facility": "Somerville Respiratory Clinic",
             "summary": "FEV1/FVC ratio 72%. 18% improvement post-bronchodilator. Reversible airway obstruction consistent with asthma.",
             "code": "CPT: 94010", "badge": "Pulmonary"},
        ],
    },
]


def seed():
    existing_ids = set()
    try:
        resp = requests.get(f"{API}/patients", timeout=10)
        resp.raise_for_status()
        existing_ids = {p["id"] for p in resp.json()}
    except requests.RequestException as e:
        print(f"Could not reach backend at {API} — is app.py running? ({e})", file=sys.stderr)
        sys.exit(1)

    for patient in PATIENTS:
        pid = patient["id"]
        if pid in existing_ids:
            print(f"skip  {pid} ({patient['fullName']}) — already exists")
            continue

        records = patient.pop("records")
        resp = requests.post(f"{API}/patients", json=patient, timeout=15)
        if not resp.ok:
            print(f"FAIL  {pid} ({patient['fullName']}): {resp.status_code} {resp.text}", file=sys.stderr)
            continue
        print(f"OK    {pid} ({patient['fullName']}) created")

        for record in records:
            rresp = requests.post(f"{API}/patients/{pid}/records", json=record, timeout=15)
            if not rresp.ok:
                print(f"  FAIL record '{record['title']}': {rresp.status_code} {rresp.text}", file=sys.stderr)
            else:
                print(f"  + record {rresp.json()['id']}: {record['title']}")


if __name__ == "__main__":
    seed()
