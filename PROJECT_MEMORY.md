# LifeLink — Project Memory

A reference for anyone (human or AI) picking this project up later: how it is
built, how the fingerprint hardware really behaves, and every bug that has
been found and fixed. Last updated 2026-09-26.

---

## 1. What the project is

LifeLink is an emergency medical identity system. A clinician registers a
patient (10 fingerprints, iris, photo, demographics, medical history). In an
emergency, the patient is identified by fingerprint and their critical
medical record is shown.

| Part | Location | Notes |
|---|---|---|
| Frontend | `index.html` | Single page app, Tailwind CSS **inlined** (pre-built, not CDN) |
| Backend | `backend/app.py` | Flask, port **3000**, serves `index.html` and `/api/*` |
| Patient data | Supabase | Patients, records, photos (`backend/schema.sql`) |
| Fingerprint data | **Local Postgres** (`lifelink` DB) | Encrypted templates + device command queue (`fingerprint-device/db/schema.sql`) |
| Device firmware | `fingerprint-device/firmware/` + `fingerprint-device/fplibMicro.py` | MicroPython on ESP32 |
| Sensor | GT-521Fx2 (firmware `20171018`) | Optical fingerprint sensor |

Iris capture is **simulated** in the frontend (no iris hardware).

---

## 2. Design rule: the sensor stores nothing

Templates live **only in the local Postgres** (`fingerprint_templates`,
encrypted with pgcrypto `pgp_sym_encrypt_bytea`, key =
`TEMPLATE_ENCRYPTION_KEY` in `backend/.env`). The sensor is a stateless
matcher:

- **Enroll:** 3 scans into temporary slot **199** → `GetTemplate(199)` →
  delete slot 199 → 498-byte template goes to the browser (held in the
  registration draft).
- **Commit:** browser posts all 10 templates in one transaction to
  `POST /api/patients/<id>/templates`.
- **Verify:** device fetches the patient's templates, loads them into
  temporary slots 199, 198, … (`SetTemplate`), captures one live finger,
  runs `Verify1_1` against each slot, wipes the slots.
- `device_slot` column is legacy and always `NULL`.

---

## 3. Hardware setup (verified working)

| GT-521Fx2 pin | ESP32 pin |
|---|---|
| TX | GPIO16 (RX2) |
| RX | GPIO17 (TX2) |
| GND | GND |
| VCC | **3V3** (VIN/5V did NOT work reliably on this board) |

- UART2, **115200 baud**. Never use GPIO1/GPIO3 (UART0 = USB console).
- ESP32 serial port: `/dev/ttyUSB0` (CP2102). MicroPython 1.29.
- WiFi: SSID lives in `firmware/config.py` (watch for **trailing spaces** in the SSID — they are significant). ESP32 gets
  `10.20.221.227`; backend PC is `10.20.221.243:3000` (changes per network →
  update `BACKEND_BASE_URL` in `firmware/config.py` and reflash).
- `DEVICE_ID = "dev-esp32-1"` must match `FP_DEVICE_ID` in `index.html`.
- The sensor's own small 4-pin JST cable is fragile; pin order at the
  sensor is **TX, RX, GND, VCC**.

### Sensor light = operator cue
- **Steady glow** → place finger flat, hold still.
- **Rapid flashing** → lift finger completely.
- Each finger is placed **3 times**, same spot and angle.

---

## 4. Running things

```bash
# Backend
cd backend && source .venv/bin/activate && python app.py

# Local DB schema (idempotent, safe to re-run)
PGPASSWORD=… psql -U lifelink -h localhost -d lifelink -f fingerprint-device/db/schema.sql

# Flash firmware (interrupt main.py first if the board is busy)
cd fingerprint-device
mpremote connect /dev/ttyUSB0 fs cp fplibMicro.py :fplibMicro.py + \
  fs cp firmware/device_api.py :device_api.py + fs cp firmware/modes.py :modes.py + \
  fs cp firmware/config.py :config.py + fs cp firmware/main.py :main.py

# Watch the device log
.venv/bin/python serial_watch.py /dev/ttyUSB0 115200 60
```

If `mpremote` says **"could not enter raw repl"**, `main.py` is busy: toggle
RTS to reset the board and spam Ctrl-C (`\x03`) for ~4 s during boot, then
retry.

Healthy boot log:
```
>> WiFi connected: ('10.20.221.227', …)
>> sensor connected, firmware 20171018
>> ready -- polling http://10.20.221.243:3000 for commands as dev-esp32-1
```

---

## 5. API (fingerprint part)

| Method | Path | Used by | Purpose |
|---|---|---|---|
| POST | `/api/devices/<dev>/commands` | browser | Queue `{mode: enroll, exclude_templates}` or `{mode: verify, patient_id}` |
| GET | `/api/devices/<dev>/commands/pending` | device | Atomically claim oldest command (204 if none); includes `params` |
| GET | `/api/devices/<dev>/commands/<id>` | browser | Poll status/result (never returns `params`) |
| POST | `/api/devices/<dev>/commands/<id>/progress` | device | Live step `{stage: place/lift/retry, scan, of}` |
| POST | `/api/devices/<dev>/commands/<id>/result` | device | `done`/`error` + result |
| POST | `/api/patients/<id>/templates` | browser | Store all templates, **one transaction** |
| GET | `/api/patients/<id>/templates` | device | Decrypted templates for verify |
| POST | `/api/enroll` | standalone/button | Store a single template |
| POST | `/api/verify` | device | Record a verify outcome |

Templates must decode to exactly **498 bytes**, otherwise the API returns 400.

Device error codes → UI text (`describeFingerprintError` in `index.html`):
`duplicate_finger` (+ `duplicate_of`), `finger_timeout`, `finger_not_lifted`,
`enroll_failed`/`bad_finger` (poor quality), `sensor_error`.

---

## 6. GT-521Fx2 quirks (learned the hard way)

1. **IsPressFinger with the backlight OFF wedges the sensor** until it is
   power-cycled. Always `CmosLed(1)` first (the driver enforces this).
2. **IsPressFinger right after the backlight turns on can misread** "no
   finger". Wait ~300 ms after LED on, and require 3 clear readings in a row
   before treating the finger as lifted.
3. **`EnrollStart(-1)` ("enroll without saving") always fails at Enroll3**
   on this firmware. Enroll into a real scratch slot, read it back, delete it.
4. **Enroll3 NACKs `enroll_failed` if the finger already exists** in the
   sensor database (duplicate check). It is indistinguishable from a bad-quality failure.
5. **`Identify1_N` gives false matches.** It matched a right index to a right
   thumb 3/3 times while `Verify1_1` correctly rejected it. Use `Verify1_1`
   per slot for all matching (verify and duplicate check).
6. **`SetTemplate` wipes the captured image buffer.** Load templates before
   `CaptureFinger`, never after.
7. The sensor keeps its baud rate until power loss; after power-up it is at
   9600. `init()` tries 115200, then falls back and sends `ChangeBaudrate`.
8. Thumbs are the hardest finger; the merge often fails if placement angle
   changes between the 3 scans (firmware retries automatically once).

---

## 7. Bug log — everything found and fixed (2026-09-26)

### Hardware / wiring
| Symptom | Root cause | Fix |
|---|---|---|
| Sensor answered, then went silent; RX line full of junk bytes | Sensor losing power / TX wire contact (GPIO16 floating, picking up crosstalk from TX) | Reseated wires; moved VCC to **3V3**. Firmware detects a floating RX line and prints `GPIO16 is floating…` |
| ESP32 vanished from USB mid-test | Board unplugged / possible VIN–GND short while rewiring | Check wiring with USB unplugged |

### Driver (`fplibMicro.py`, fully rewritten)
| Bug | Effect | Fix |
|---|---|---|
| UART RX buffer left at default 256 bytes | Every template truncated (**312 of 498 bytes** stored); `SetTemplate` never acked | `rxbuf=2048` |
| `_read_packet(wait=True)` looped forever | Firmware froze, Ctrl-C couldn't reach REPL | All reads deadline-bounded |
| Unbounded `_flush()` / `read()` | Froze forever on a noisy/floating line | Bounded flush, sized reads |
| No checksum verification, NACK codes ignored | Garbage accepted silently, no error info | Checksums verified; `last_error` from NACK codes |
| `init()` returned True even with no sensor reply | "Sensor OK" when it wasn't | True only if `Open` is ACKed |
| No wait-for-finger before `CaptureFinger` | Enroll gave up after ~10 s → `capture_failed` | `wait_for_finger` + settle delay + capture |
| Captured on first contact | Partial prints, poor matches | 400 ms settle before capture |
| Finger sliding off during capture aborted the scan | `finger_is_not_pressed` errors | Go back to waiting for the finger until timeout |
| No "lift finger" step between scans | 3 scans of one placement → Enroll3 rejects | Flash backlight until finger removed; required between scans |
| Lift detected from one false reading | Previous finger (thumb) scanned as the next one | 3 consecutive clear readings, LED warm-up |
| Enroll slot chosen via `GetEnrollCount` + `DeleteID` | Could delete another patient's print | Replaced by scratch-slot design |
| `set_led(False)` in cleanup reset `last_error` | Real error lost (reported as `None`) | Error saved before cleanup |

### Firmware (`firmware/`)
| Bug | Fix |
|---|---|
| Verify loaded templates **after** capture (SetTemplate wiped image) → same finger rejected | Load all templates first, then capture, then `Verify1_1` each |
| Duplicate check used `Identify1_N` → false "already captured" | `Verify1_1` per loaded slot |
| Enroll error overwritten by slot wipe → blank error | Read `last_error`/`duplicate_slot` before wiping |
| HTTP calls had no timeout | `timeout=10` on all requests |
| Loop crashed on any exception; halted if sensor missing | Catch-all loop, WiFi reconnect, sensor re-init on `SensorError` |
| Verify could scan a finger left on from before | Wait for lift before capture |
| Standalone button mode used `int(patient_id)` | Uses lifelink ids (`LL-xxxx-X`) |

### Backend (`backend/app.py`)
| Bug | Fix |
|---|---|
| Accepted truncated templates | Rejects anything that isn't 498 bytes (`decode_template`) |
| `device_slot` required | Optional / `NULL` |
| Templates linked one by one after patient creation (partial failures) | `POST /api/patients/<id>/templates`, single transaction |
| Browser poll could receive template params | `GET` command returns explicit columns, no `params` |

### Frontend (`index.html`)
| Bug | Fix |
|---|---|
| No per-step guidance during a scan | **Operator Prompt** panel driven by device progress |
| Raw error codes shown | `describeFingerprintError` plain-language messages |
| Enroll poll timed out at 45 s | 180 s enroll (covers auto-retry), 60 s verify |
| Commit said success even when templates failed | Requires 10/10, atomic store, retry, honest alert |
| Demo prefill faked 10 fingerprints with no templates | Prefill no longer touches fingerprints |
| Platen viewport collapsed (no fingerprint graphic) | Inlined Tailwind lacked `w-28 h-32 text-[6px] min-h-[130px] p-1.5 inset-2 …`; added a supplemental `<style>` block |
| "WAIT — SENSOR PREPARING" made users place the next finger too early | "KEEP ALL FINGERS OFF THE SENSOR — PLACE … ONLY WHEN THE LIGHT IS STEADY" |
| Identify page didn't say which finger matched | Shows e.g. "RIGHT THUMB (R1)" |

### Data cleanup done
- 9 legacy prints wiped from the sensor (slots 0–8, old design).
- 4 corrupt 312-byte template rows deleted from Postgres.
- Test rows removed after each test run.

---

## 8. Verified end-to-end results (real hardware, 2026-09-26)

- Enroll → 498-byte template, byte-identical after the Postgres round trip.
- Same finger verify: matched 4/4. Different finger: rejected 2/2.
- Duplicate finger during registration: correctly refused as `R1`.
- Full 10-finger registration via the web app → patient **LL-6658-M**
  (Kingsley Judewin, demo demographics) with R1–R5, L1–L5 stored;
  Identify page confirmed the patient by fingerprint.

---

## 9. Known open items

- `device_commands.result` keeps an **unencrypted copy** of each captured
  template. Scrub it once the browser has read it, or purge old rows.
- Iris capture is simulated.
- Test patient LL-6658-M uses demo demographics and a generated photo.
- The inlined Tailwind bundle can silently miss classes. When adding new
  utility classes, check they exist or add them to the supplemental
  `<style>` block.
- No cross-patient duplicate check (same person registered twice).
