# LifeLink — Project Memory

A reference for anyone (human or AI) picking this project up later: how it is
built, how the fingerprint and iris hardware really behave, and every bug
that has been found and fixed. Last updated 2026-09-30.

---

## 1. What the project is

LifeLink is an emergency medical identity system. A clinician registers a
patient (10 fingerprints, both irises, photo, demographics, medical
history). In an emergency, the patient is confirmed by fingerprint or iris
and their critical medical record is shown.

| Part | Location | Notes |
|---|---|---|
| Frontend | `index.html` | Single page app, Tailwind CSS **inlined** (pre-built, not CDN) |
| Backend | `backend/app.py` | Flask, port **3000**, serves `index.html`, `/api/*`, and the iris camera's `/health` + `/capture` |
| Patient data | Supabase | Patients, records, photos (`backend/schema.sql`) |
| Biometric data | **Local Postgres** (`lifelink` DB) | Encrypted fingerprint + iris templates, device command queue (`fingerprint-device/db/schema.sql`, then `iris-module/db/schema.sql`) |
| Fingerprint firmware | `fingerprint-device/firmware/` + `fingerprint-device/fplibMicro.py` | MicroPython on ESP32 |
| Fingerprint sensor | GT-521Fx2 (firmware `20171018`) | Optical fingerprint sensor |
| Iris engine | `iris-module/engine/iris_service.py` | FastAPI on **127.0.0.1:8010**, stateless: LightIrisNet segmentation + VIS-IrisFormer |
| Iris camera | `iris-module/camera-client/camera_client.py` | Pi Zero + CSI camera, autostarts, device id `iris-cam-1`. Also takes the **patient photo** (there is no webcam) |
| Target hardware | Raspberry Pi 5 (main system) + Pi Zero (camera) | Pi 5 runs backend, engine, Postgres, GUI |

Detailed iris docs: `iris-module/README.md`. Iris code was imported from
[LifeLink_Iris_module](https://github.com/Mervin0266/LifeLink_Iris_module)
(teammate's standalone engine) and adapted.

Secrets live only in `backend/.env` (gitignored; `backend/.env.example`
lists the variables) and `fingerprint-device/firmware/config.py`
(gitignored). The GitHub repo `kingsleyjudewin/lifelink-gui` is
**public** — never commit keys, WiFi passwords, patient photos
(`backend/uploads/`) or the 453 MB model file.

---

## 2. Design rule: devices store nothing

All biometric templates live **only in the local Postgres**, encrypted with
pgcrypto `pgp_sym_encrypt_bytea` (key = `TEMPLATE_ENCRYPTION_KEY` in
`backend/.env`). Neither the fingerprint sensor nor the iris camera keeps anything.

### Fingerprint
- **Enroll:** 3 scans into temporary slot **199** → `GetTemplate(199)` →
  delete slot 199 → 498-byte template goes to the browser (held in the
  registration draft).
- **Commit:** the browser sends the record, photo, all 10 templates and the
  iris command ids in **one** `POST /api/patients`; the backend removes the
  Supabase row + photo again if storing the biometrics fails (all or
  nothing; the GUI keeps its draft and COMMIT can be retried).
- **Verify (1:1, patient chosen):** device fetches the patient's templates,
  loads them into temporary slots 199, 198, … (`SetTemplate`), captures one
  live finger, runs `Verify1_1` against each slot, wipes the slots.
- **Identify (1:N, no patient id, since 2026-09-30):** one live capture →
  `MakeTemplate` (498 B probe) → `SetTemplate(199, probe)` → the device
  pages through **every** stored template (`GET /api/fingerprint/candidates`,
  20 per page, the chosen finger across all patients first) and sends each
  with `Verify1_1_Template` (0x52) against slot 199 until one matches;
  slot 199 is wiped after. Measured **~75 ms per comparison** on the real
  sensor (20 templates in 1.5 s), so ~7.5 s for one finger across 100
  patients. `Identify1_N` is still not used (false matches).
- `device_slot` column is legacy and always `NULL`.

### Iris
- **Enroll (one request per eye):** Pi Zero sends its best eye frame → the
  engine returns a template (128 patch embeddings × 768 float32, ~393 KB)
  → stored **encrypted and unlinked** (`iris_templates.patient_id NULL`,
  `command_id` set). The browser only keeps the command ids
  (`draft.iris.commandIdOD/OS`) — template bytes never reach the browser.
- **Commit:** `POST /api/patients/<id>/iris-templates {command_ids: [od, os]}`
  links both eyes in one transaction (replaces any old ones). Unlinked rows
  are purged after 1 day.
- **Verify:** 1:1 against the patient chosen on the Identify page; the
  engine compares the live eye with both stored eyes, match if best cosine
  similarity ≥ **0.84** (`engine/config/matching_threshold.json`).
- Camera frames are never written to disk (engine or backend).

---

## 3. Hardware setup

### Fingerprint (verified working)

| GT-521Fx2 pin | ESP32 pin |
|---|---|
| TX | GPIO16 (RX2) |
| RX | GPIO17 (TX2) |
| GND | GND |
| VCC | **3V3** (VIN/5V did NOT work reliably on this board) |

- UART2, **115200 baud**. Never use GPIO1/GPIO3 (UART0 = USB console).
- ESP32 serial port: `/dev/ttyUSB0` (CP2102). MicroPython 1.29.
- WiFi: SSID lives in `firmware/config.py` (watch for **trailing spaces** in
  the SSID — they are significant). On the dev network the ESP32 got
  `10.20.221.227` and the laptop was `10.20.221.243:3000` (changes per
  network → update `BACKEND_BASE_URL` in `firmware/config.py` and reflash).
  On the Pi 5 network it becomes a fixed `http://192.168.50.1:3000`.
- `DEVICE_ID = "dev-esp32-1"` must match `FP_DEVICE_ID` in `index.html`.
- The sensor's own small 4-pin JST cable is fragile; pin order at the
  sensor is **TX, RX, GND, VCC**.

Sensor light = operator cue: **steady glow** → place finger flat, hold
still; **rapid flashing** → lift finger completely. Each finger is placed
**3 times**, same spot and angle.

### Deployment hardware (user, 2026-09-29)
Pi 5 **16 GB** · Pi Zero **W** (original ARMv6, not Zero 2 W) · **Camera Module 3
Wide NoIR** (IMX708, ~102° H FOV, AF, no IR-cut filter) · **7" 800×480** display.
Both iris models are **visible-light** (LightIrisNet VIS; VIS-IrisFormer trained
on UBIRIS.v2 — the ND0405/Thousand/Mobile NIR configs in `Vis-IrisFormer/` are
leftovers from upstream IrisFormer). Use white light, not 850 nm IR.

### Iris — Pi 5 ↔ Pi Zero (team architecture; NOT yet run on real Pis)

```
Internet ── Ethernet ── Pi 5 (main-pi.local, 192.168.50.1)
                          │  Wi-Fi AP + DHCP (NetworkManager "shared") + mDNS (avahi)
                          ├── Pi Zero + CSI camera (192.168.50.x)
                          └── ESP32 fingerprint (192.168.50.x)
```

- Camera → CSI → Pi Zero → Wi-Fi → Pi 5. The Pi Zero needs no internet.
- `iris-module/pi5-setup/setup_network.sh <ssid> <password>` creates the AP
  (password is an argument — never commit it; the architecture doc's example
  SSID was `ProjectNet`).
- `iris-module/pi5-setup/install_services.sh` → systemd units
  `lifelink-iris-engine` and `lifelink-backend` on the Pi 5.
- `iris-module/camera-client/install_pizero.sh` → apt `python3-opencv
  python3-picamera2`, systemd unit `lifelink-iris-camera`, sudoers rule
  allowing only `shutdown -h now`.
- Model is **visible-light** (VIS-IrisFormer), not NIR — an ordinary
  camera with good even lighting. The GUI still says "850NM NIR" / "Dual-NIR"
  in places (cosmetic, see open items).

### Iris operator flow
1. GUI: *CAPTURE IRIS* (right eye) → GUI says *POWER ON THE IRIS CAMERA*.
2. Power on the Pi Zero; the patient looks into it. On SUCCESS the Pi Zero
   powers itself off and the GUI advances to the left eye → repeat.
3. Identify: select patient → *SCAN IRIS* → power on the Pi Zero.

So enrollment = 2 power-ons, each identify = 1. `--no-shutdown` keeps the
client running (development).

### Patient photo (same Pi Zero camera, since 2026-09-29)
There is no webcam on the Pi 5, so the photo step uses the iris camera:
*CAPTURE PHOTO* → GUI queues a `photo_capture` request → power on the Pi
Zero, patient faces it ~50 cm away (live preview on the GUI) → the client
skips 15 frames for exposure to settle, sends the sharpest (Laplacian
variance) centre-square 720×720 JPEG of the next 15 → SUCCESS → shutdown.
The backend keeps the JPEG **in memory only** until the GUI collects it once
(`GET …/commands/<id>/photo`, max 10 min); the GUI stages it as a data URL
and it is written to `backend/uploads/` only on commit, as before. Leaving
the photo page cancels the request. Registration = 3 Pi Zero power-ons.

---

## 4. Running things

```bash
# Backend (dev laptop)
cd backend && .venv/bin/python app.py            # FLASK_DEBUG=1 to enable the debugger

# Iris engine (~6 s to load models; ~3–4 s per extract/verify on CPU)
cd iris-module/engine && .venv/bin/python iris_service.py      # 127.0.0.1:8010

# Local DB schema (both idempotent, safe to re-run; fingerprint first)
PGPASSWORD=… psql -U lifelink -h localhost -d lifelink -f fingerprint-device/db/schema.sql
PGPASSWORD=… psql -U lifelink -h localhost -d lifelink -f iris-module/db/schema.sql

# Iris camera without a Pi (webcam / image / video; no shutdown)
cd iris-module/camera-client
../engine/.venv/bin/python camera_client.py --host localhost --source 0 --no-shutdown

# Flash fingerprint firmware (interrupt main.py first if the board is busy)
cd fingerprint-device
mpremote connect /dev/ttyUSB0 fs cp fplibMicro.py :fplibMicro.py + \
  fs cp firmware/device_api.py :device_api.py + fs cp firmware/modes.py :modes.py + \
  fs cp firmware/config.py :config.py + fs cp firmware/main.py :main.py

# Watch the ESP32 log
.venv/bin/python serial_watch.py /dev/ttyUSB0 115200 60

# Watch the Pi Zero camera log (on the Pi Zero)
journalctl -u lifelink-iris-camera -f
```

- Iris engine venv: `python3 -m venv .venv`, then CPU torch
  (`pip install torch torchvision --index-url https://download.pytorch.org/whl/cpu`),
  then `pip install -r requirements.txt`, then `./download_models.sh`
  (fetches `sota.pth` from the upstream repo's Git LFS, SHA-256 checked).
- **Port 8000 on the dev laptop is taken** by an unrelated deepfake-detection
  API → the engine uses 8010 (`IRIS_ENGINE_URL` in `backend/.env`).
- If `mpremote` says **"could not enter raw repl"**, `main.py` is busy:
  toggle RTS to reset the board and spam Ctrl-C (`\x03`) for ~4 s during
  boot, then retry.

Healthy ESP32 boot log:
```
>> WiFi connected: ('10.20.221.227', …)
>> sensor connected, firmware 20171018
>> ready -- polling http://10.20.221.243:3000 for commands as dev-esp32-1
```

Webcam iris tips: only the **central 70%** of each frame is used (eye in
the middle, close to the lens), bright even light, no lamp reflection in
the pupil, eye wide open. Laptop webcams can't focus very close (try
10–20 cm). The GUI photo page and the camera client can't share the webcam.

---

## 5. API

### Device command queue (browser ↔ backend ↔ ESP32 / iris)

| Method | Path | Used by | Purpose |
|---|---|---|---|
| POST | `/api/devices/<dev>/commands` | browser | Queue `{mode: enroll, exclude_templates}`, `{mode: verify, patient_id}`, `{mode: identify, finger?}`, `{mode: iris_enroll, eye_side: right/left}`, `{mode: iris_verify, patient_id}`, `{mode: photo_capture}`. Fingerprint modes: 503 `FINGERPRINT_DEVICE_OFFLINE` if the reader hasn't called in for 15 s; older open fingerprint requests become `superseded` |
| GET | `/api/devices/<dev>/commands/pending` | device (key) | Atomically claim oldest command (204 if none); includes `params`. Expired ones are closed first |
| GET | `/api/devices/<dev>/commands/<id>` | browser | Poll status/result (never returns `params`); an enroll `template_data` is returned once, then removed |
| POST | `/api/devices/<dev>/commands/<id>/cancel` | browser | End an open request (timeout, left the page, skip) |
| POST | `/api/devices/<dev>/commands/<id>/progress` | device (key) | Live step `{stage: place/lift/retry, scan, of}`; identify: `{stage: searching, checked, total}` |
| POST | `/api/devices/<dev>/commands/<id>/result` | device (key) | `done`/`error` + result; **409** if the request is already closed |
| GET | `/api/devices/status` | browser | `{device_id: {online, last_seen_seconds}}` |
| GET | `/api/fingerprint/candidates?finger=&offset=&limit=` | ESP32 (key) | `{total, items: [{patient_id, finger_position, template_data}]}` for the 1:N search |

Access (`require_caller`): device routes need `X-API-Key` = that device's
entry in `DEVICE_API_KEYS` (backend/.env); all other routes only answer
`GUI_ALLOWED_NETS` (default 127.0.0.0/8). Other routes: `/api/audit`
(GET/POST), `/api/draft` (GET/PUT/DELETE), `/api/system/status`,
`/api/system/integrity`, `/fonts/<file>`.

### Fingerprint templates

| Method | Path | Used by | Purpose |
|---|---|---|---|
| POST | `/api/patients/<id>/templates` | browser | Store all templates, **one transaction** |
| GET | `/api/patients/<id>/templates` | ESP32 | Decrypted templates for verify |
| POST | `/api/enroll` | standalone/button | Store a single template |
| POST | `/api/verify` | ESP32 | Record a verify outcome |

Templates must decode to exactly **498 bytes**, otherwise the API returns 400.

### Iris

| Method | Path | Used by | Purpose |
|---|---|---|---|
| GET | `/health?device_id=iris-cam-1` | Pi Zero | `{status: ok, system, ready, request?}` — `ready` only while the GUI has an active camera command; `request` = `iris` or `photo` says what to capture |
| POST | `/capture` | Pi Zero | `{capture_id, device_id, timestamp, image_hash, image_data}` → `{capture_id, status: SUCCESS\|RESCAN\|RETRY, reason?}` |
| GET | `/api/devices/<dev>/commands/<id>/photo` | browser | The `photo_capture` JPEG, **once** (then dropped from memory); 404 if gone |
| POST | `/api/patients/<id>/iris-templates` | browser | Link `{command_ids: [od, os]}` to the patient (409 if not exactly one unlinked left + right) |
| POST | engine `/extract` | backend | `{image_data}` → `{template_data (b64 .npy), quality_score, segmentation}` or 422 `segmentation_invalid` |
| POST | engine `/verify` | backend | `{image_data, templates:[{eye_side, template_data}]}` → `{match, similarity, eye_side, threshold, scores}` |

Iris protocol rules:
- **SUCCESS** = processed, or nothing to do (`reason: no_active_request` /
  `no_iris_templates`) → Pi Zero shuts down.
- **RESCAN** = engine rejected the frame → Pi Zero scans again.
- **RETRY** = SHA-256 mismatch or engine unreachable → Pi Zero resends the
  **same** frame + capture_id. RETRY is not recorded; SUCCESS/RESCAN are
  recorded per `(device_id, capture_id)` in `iris_captures`, so a resend
  gets the recorded answer and is never reprocessed.
- Capture ids: `CAPTURE-<6-hex boot token>-0001…` (unique across reboots).
- Iris commands expire after **5 min** (`IRIS_COMMAND_TTL`; GUI polls 5
  min). A new GUI iris request marks older active ones `superseded`.
- GUI progress stages (backend writes them, camera doesn't):
  `processing`, `rescan` (+reason), `engine_unavailable`.

Device error codes → UI text in `index.html`:
- Fingerprint (`describeFingerprintError`): `duplicate_finger`
  (+ `duplicate_of`), `finger_timeout`, `finger_not_lifted`,
  `enroll_failed`/`bad_finger` (poor quality), `sensor_error`.
- Iris (`describeIrisError` / `describeIrisProgress`): `expired`,
  `superseded`, `no_iris_templates`.

---

## 6. Hardware / model quirks (learned the hard way)

### GT-521Fx2
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

### Iris engine / camera
1. **The normalizer silently falls back to a default geometry** when
   segmentation finds no iris, so *any* image (even random noise) yields a
   template. `iris_service.py` runs `SegmentationValidator` first and
   answers 422 → RESCAN. Keep that gate if the pipeline changes.
2. **Stage A quality (on the Pi Zero) is weak**: random noise scored 95/100.
   It only measures sharpness/exposure/glare; the engine's segmentation
   validation is the real gate.
3. If a full frame fails segmentation and is > 400×400, the engine retries
   on the central ROI crop (`EyeROICropper`, central 70%).
4. **Picamera2 `RGB888` arrays are B,G,R per pixel** — convert to RGB.
5. **Picamera2 must be `close()`d** after each scan or the next
   `Picamera2()` fails (camera busy).
6. Model load ~6 s; each `/extract` or `/verify` ~3–4 s on laptop CPU
   (Pi 5 will be slower — untested).
7. Similarity reference (test photos): same eye 1.00; same eye shifted +
   brightness changed 0.983; different people 0.69–0.77. Threshold 0.84.

---

## 7. Bug log — everything found and fixed

### 2026-09-26 — fingerprint

#### Hardware / wiring
| Symptom | Root cause | Fix |
|---|---|---|
| Sensor answered, then went silent; RX line full of junk bytes | Sensor losing power / TX wire contact (GPIO16 floating, picking up crosstalk from TX) | Reseated wires; moved VCC to **3V3**. Firmware detects a floating RX line and prints `GPIO16 is floating…` |
| ESP32 vanished from USB mid-test | Board unplugged / possible VIN–GND short while rewiring | Check wiring with USB unplugged |

#### Driver (`fplibMicro.py`, fully rewritten)
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

#### Firmware (`firmware/`)
| Bug | Fix |
|---|---|
| Verify loaded templates **after** capture (SetTemplate wiped image) → same finger rejected | Load all templates first, then capture, then `Verify1_1` each |
| Duplicate check used `Identify1_N` → false "already captured" | `Verify1_1` per loaded slot |
| Enroll error overwritten by slot wipe → blank error | Read `last_error`/`duplicate_slot` before wiping |
| HTTP calls had no timeout | `timeout=10` on all requests |
| Loop crashed on any exception; halted if sensor missing | Catch-all loop, WiFi reconnect, sensor re-init on `SensorError` |
| Verify could scan a finger left on from before | Wait for lift before capture |
| Standalone button mode used `int(patient_id)` | Uses lifelink ids (`LL-xxxx-X`) |

#### Backend (`backend/app.py`)
| Bug | Fix |
|---|---|
| Accepted truncated templates | Rejects anything that isn't 498 bytes (`decode_template`) |
| `device_slot` required | Optional / `NULL` |
| Templates linked one by one after patient creation (partial failures) | `POST /api/patients/<id>/templates`, single transaction |
| Browser poll could receive template params | `GET` command returns explicit columns, no `params` |

#### Frontend (`index.html`)
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

### 2026-09-27 — repo safety (before first push of backend/device code)
| Issue | Fix |
|---|---|
| `app.run(debug=True)` on `0.0.0.0` → Werkzeug debugger = remote code execution on the LAN | Debug only with `FLASK_DEBUG=1` |
| No record of required env vars | `backend/.env.example` (no values) |
| `__pycache__`/venvs only ignored under `backend/` | Global `.gitignore` rules; `.env.*` ignored except `.env.example` |
| Home WiFi SSID written in this file (public repo) | Removed |

### 2026-09-27 — iris (imported module → LifeLink)

#### Original iris module (`LifeLink_Iris_module`) problems fixed during integration
| Problem | Fix |
|---|---|
| Templates saved as **unencrypted `.npy` files** on disk | Engine is stateless; backend stores pgcrypto-encrypted bytes in Postgres |
| Every received frame saved to `incoming_captures/` (raw eye images) | Frames never written to disk |
| First capture was **auto-enrolled** as a new subject; only anonymous 1:N identification, no link to a patient | Explicit enroll per eye + link at commit; 1:1 verify against the chosen patient |
| Two thresholds (identify 0.80, config 0.84) | One threshold, 0.84 from `matching_threshold.json` |
| Normalizer fallback → template from any image | `SegmentationValidator` gate → 422 / RESCAN |
| Pi Zero client imported the torch segmenter (README said no torch needed) | Client uses Stage A only; `frame_selector.py` imports the segmenter for type hints only |
| `Picamera2Backend` imported `picam2` (module is `picamera2`) → failed on every Pi | Fixed import |
| Picamera2 frames were BGR, pipeline expects RGB | `frame[:, :, ::-1]` |
| Picamera2 never closed → second scan (after RESCAN) can't open the camera | `close()` in `stop()` |
| `config.yaml` checkpoint paths didn't match where the files are | Paths → `models/…` |
| `sota.pth` in Git LFS (git-lfs not installed; 453 MB > GitHub limit) | Gitignored; `download_models.sh` with SHA-256 check |

#### Integration issues hit
| Issue | Fix |
|---|---|
| Engine failed to start: port 8000 already used by another service on the laptop | Engine default port 8010, `IRIS_ENGINE_URL` |
| FastAPI `on_event` deprecation warning | Lifespan handler |
| First version polled the command queue and never shut down — didn't match the team's Pi 5/Pi Zero architecture | Reworked to `/health` + `/capture`, SUCCESS→shutdown, RESCAN loop, RETRY resend, capture-id dedupe |
| Iris commands left open forever if the GUI gave up | 5-min expiry + newer request supersedes |
| Demo prefill faked both irises as captured | Prefill no longer touches iris |
| Identify "SCAN IRIS" was `simulateAuthenticate` (fake 99.8% match) | Real `verifyIrisReal` |

### 2026-09-29 — deployment review for the Pi 5 + photo via Pi Zero
| Issue | Fix / status |
|---|---|
| No webcam available for the photo step (it used browser `getUserMedia`) | New `photo_capture` camera mode: backend (`CAMERA_MODES`, `/health` `request`, `/capture` photo branch, in-memory photo store), camera client `scan_photo()`, GUI photo page rewritten, DB mode constraint updated (**re-run `iris-module/db/schema.sql`**) |
| `install_services.sh` / `install_pizero.sh` write `ExecStart=$REPO/...` unquoted; repo path `life link gui` has spaces → systemd runs `.../life` (verified with `systemd-analyze verify`) | **Open** — quote the paths, or clone into a folder without spaces |

### 2026-09-30 — regional languages (i18next)
- Language switcher (top-left header button, `translate` icon): English,
  Hindi, Tamil, Malayalam, Telugu, Kannada, Assamese. Choice persists in
  `localStorage['lifelink_language']`.
- `i18n/i18next.min.js` (v23, vendored for offline use), `i18n/translations.js`
  (one row per English string: `[en, hi, ta, ml, te, kn, as]`, matched
  case-insensitively; `{{name}}` rows are patterns for runtime text such as
  fingerprint scan prompts), `i18n/lifelink-i18n.js` (translates DOM text
  nodes + `placeholder`/`title` in place, MutationObserver for later renders).
  Flask serves them at `/i18n/<file>`.
- Deliberately left in English: patient data, audit-log entries, technical
  specs (ISO numbers, serials, CPU/RAM), `alert()` dialogs, header breadcrumb.
  Adding a string = appending a row. Mark an element `data-no-i18n` to exclude it.
- i18next reserves `count` for plurals, so pattern placeholders use `{{n}}`.
- Indic scripts use Noto Sans (Google Fonts); letter-spacing is reset for them.
  On the offline Pi install `fonts-noto-core` so glyphs render without the CDN.

| Issue | Fix / status |
|---|---|
| Second `<style>` block in `<head>` (extra Tailwind utilities) was never closed → the design-tokens block's `<!-- -->`/`<style>` were parsed as CSS, swallowing the `:root` rule (all `var(--…)` empty) and invalidating the `@import`s (Inter + Material Symbols never loaded; icons showed as words like `person_add`) | Added the missing `</style>` |

### 2026-09-30 — visual refinement ("taste-skill", redesign-existing-projects)
- Canvas is pure `#FFFFFF`; one cool-grey family (`#F7F7F8` surfaces, `#E4E4E7`
  borders, `#6B6B72` secondary text), ink `#111113`. Status colours unchanged.
- Colour classes were remapped in the markup/JS templates (657 replacements)
  and Tailwind rebuilt (tools/build-tailwind.md); the hand-patched second
  utility `<style>` block was removed because the rebuild now contains it.
- Font Inter → Geist (still a single family). Radius scale 6px/12px.
- `.btn-metallic*` keep their class names but are now flat ink/white/red/green
  buttons with soft tinted shadows and press-scale feedback; inputs get a 3px
  focus ring; pages settle in with a 0.32s fade (off under reduced-motion).
- Skills installed to `.claude/skills/` by `npx skills add Leonxlnx/taste-skill`
  (+ `skills-lock.json`).
- Header (user request): breadcrumb + "HARDWARE REVISION" block and its status
  dot removed (router no longer writes `#global-breadcrumb`). Clock shows
  Indian Standard Time via `Intl` with `timeZone: 'Asia/Kolkata'` (label IST),
  independent of the Pi's system timezone; the date uses the UI language
  (`hi-IN`, `ta-IN`, ...). "LIFELINK" wordmark, tagline and `document.title`
  are translated.

### 2026-09-30 — fit-to-screen layout for the 800×480 display
- Before: at 800 px the `lg:` preview/controls split never kicked in, so
  controls stacked under the preview (fingerprint 878 px, iris 1043 px, photo
  765 px, identify 847 px tall); the iris preview and its button were never on
  screen together, and on Identify the patient list sat below the scan buttons.
- Now (`@media (max-height:560px) and (min-width:640px)` in index.html):
  fingerprint / iris / photo / identify are exactly one screen, no scrolling —
  preview left, controls right, Back/Continue pinned under the controls.
  Hook classes in the markup: `capture-page`, `capture-head`, `capture-grid`,
  `capture-preview`, `capture-side`, `capture-card`, `capture-actions`,
  `capture-nav`, `identify-*`; decorative telemetry is `compact-hide`.
  Identify: methods left, patient archive right (own scroll); while an iris
  scan runs (`#auth-iris-preview-wrap` shown) the fingerprint option folds away.
  Header is a fixed 52 px there; page height = `100dvh - 72px`.
- Long forms (patient info, medical info, summary, review) still scroll but
  their `.page-nav` Back/Continue bar is sticky at the bottom on compact panels.
- Verified at 800×480 in all 7 UI languages: no page or panel overflow;
  desktop (≥ 561 px tall) layout unchanged.
- Home: "Quick Reference Dossier" demo strip (hard-coded LL-8942-A shortcuts)
  removed at the user's request, with its only caller `goToPatientView` and
  its 3 translation rows.
- Fingerprint + iris pages decluttered (user request, all screen sizes): no
  "MODULE:" line, status/ISO badges, viewport header/footer readouts (POS,
  surface temp, DPI, FPS/gain, DIST/ILLUM), fingerprint grid background,
  anatomical ID, "Enrollment Matrix"/"Ocular selection"/"Lateral sector"
  labels, or bolt/skip icons on the secondary buttons. The platen graphic with
  its scan line and the iris reticle/live preview stay. Their JS updaters and
  translation rows were removed too.
- Fingerprint scanner visual replaced (user's reference image): circuit-ring
  emblem — three SVG ring layers (`.fp-ring--a/b/c`, rotating 70/100/140 s)
  around a raised disc with a symmetric loop-pattern print (`#fp-ridges`:
  22 evenly spaced hairpin ridges, mirrored breaks, no randomness; faded at
  the sides and base by two masks; shared by a base and an "ink" layer). `#fp-scanner[data-state]` =
  idle | scanning | lift | success | captured | error, set by
  `setScannerState()` in LifeLinkBiometrics (scan start/progress/success/
  failure, and `renderFingerprintView` when not scanning). Scanning = green
  beam sweep revealing the print + green nodes; success = check badge.
  `#scan-feedback-txt` is now the caption under the emblem.
- Names in regional scripts (user decision 2026-09-30): names are SAVED exactly as
  typed (English) and only DISPLAYED transliterated. `i18n/transliterate.js` is an
  offline rule-based English→Devanagari phonetic converter, shifted into the other
  Indic blocks with per-script fixes (Tamil folding + ந/ன + nasal classes,
  Assamese ৰ/ৱ/য়, Malayalam chillu + nasal clusters, final virama in ta/te/kn/ml).
  Elements marked `data-translit` are transliterated by lifelink-i18n.js (dictionary
  fallbacks like "UNNAMED PATIENT" still translate); `{{name}}` pattern values too.
  Inputs with `data-translit-input="<id>"` show a live "↳ script" preview line.
  Google Input Tools was rejected: it would send patient names off-device.
  Approximate for some spellings (Rahul → रहुल): English doesn't mark long vowels.

### 2026-09-30 — fingerprint device: no more reflashing per network
| Issue | Fix / status |
|---|---|
| Every new WiFi/hotspot needed a reflash: `config.py` had one SSID and the laptop's IP hard-coded in `BACKEND_BASE_URL` (DHCP gives a new IP per network) | `WIFI_NETWORKS` list (strongest known network in range wins); backend discovery: device broadcasts `LIFELINK_DISCOVER` on UDP 37020, `backend/app.py` `run_discovery_responder` replies `LIFELINK_BACKEND <port>`, device uses the reply's source IP; cached in `backend_url.txt` on the ESP32, re-discovered after every WiFi connect and after 3 failed requests. `BACKEND_BASE_URL` is now only a fallback |
| Verified on hardware: joined "Segue" (SSID is capitalised, case-sensitive) -52 dBm, discovered `http://60.70.3.137:3000`, sensor fw 20171018 up. ufw on the laptop lets the UDP reply through | Done |
| Two stale commands (#146 verify, #147 enroll from 11:51) were waiting in `device_commands` and ran first after boot | Both timed out (`finger_timeout`); queue empty. Scans pressed while the board is offline stay queued — watch for this |
| Adding a network later = one line in `WIFI_NETWORKS` + reflash of `config.py` only; a new backend IP needs nothing | — |

### Data cleanup done
- 9 legacy prints wiped from the sensor (slots 0–8, old design).
- 4 corrupt 312-byte template rows deleted from Postgres.
- Test rows removed after each test run (incl. iris E2E test patients
  LL-8735-M, LL-9833-M, LL-7183-M, LL-7585-M, deleted from Supabase + local tables).

---

## 8. Verified results

### Fingerprint — real hardware, 2026-09-26
- Enroll → 498-byte template, byte-identical after the Postgres round trip.
- Same finger verify: matched 4/4. Different finger: rejected 2/2.
- Duplicate finger during registration: correctly refused as `R1`.
- Full 10-finger registration via the web app → patient **LL-6658-M**
  (Kingsley Judewin, demo demographics) with R1–R5, L1–L5 stored;
  Identify page confirmed the patient by fingerprint.

### Iris — laptop, 2026-09-27 (Wikimedia eye photos as the camera source)
- Engine: extract 200 for eye photos (quality ~97), random noise → 422.
- Backend protocol test: **13/13** (health ready/not ready, no-request
  SUCCESS, supersede, SHA-256 mismatch → RETRY, bad frame → RESCAN, resent
  capture_id → recorded answer, good frame → SUCCESS, missing id → 400).
- GUI end-to-end in headless Chrome (puppeteer-core + system Chrome;
  the Claude-in-Chrome extension wasn't connected): capture both eyes via
  the buttons → commit (both eyes linked) → Identify *SCAN IRIS* same eye
  **CONFIRMED 100%** → dashboard; other person **NO MATCH 69.2%**; engine
  killed mid-scan → GUI shows "IRIS ENGINE OFFLINE", camera keeps
  resending, completes once the engine is back; camera exits 0 after
  calling `sudo -n shutdown -h now`. Stored bytes are PGP-encrypted.
- **Not yet done: real Pi Zero camera, real Pi 5, real eye.**

---

### 2026-09-30 — deep-audit fixes + fingerprint identify without an ID
All of these were verified: backend API test 54/54, headless-Chrome GUI test
24/24 at 800×480 (with a simulated reader), offline test with Supabase
unreachable, and the real ESP32 on the new firmware (identify → clean
`finger_timeout` with no finger; key auth accepted). Test scripts are not in
the repo (scratch only).

| # | Issue | Fix |
|---|---|---|
| — | No way to identify an unknown / unconscious patient (Identify was 1:1 only) | New `identify` command mode: **IDENTIFY BY FINGER** on the Identify page, no patient id. Firmware `modes.identify_mode`, driver `verify_template` (0x52), `GET /api/fingerprint/candidates`. Finger selector (right index default, "not sure" = all fingers). Iris stays 1:1 |
| 2 / 19 | Stale fingerprint requests took the next finger; a lost result left the request `in_progress` forever | GUI cancels on timeout and when leaving the page (`cancelScansNotOn`); a new fingerprint request supersedes older open ones; backend expires unclaimed ones after 3 min (`expired`) and silent claimed ones after 90 s (`device_lost`); device retries the result POST 6× with backoff, 409 = request closed, drop it |
| 20 | One failed poll ended a scan | `pollDeviceCommand` retries until the backend has been unreachable for 15 s |
| 3 | Enroll-page iris green box wider than the image (531 vs 348 px) | Preview frame is always exactly 16:9 (`.iris-live-frame`, container query units), image `object-fit: fill` → green box = central 70% of the frame on both pages. Measured in Chrome: box = 0.70 × image width, offset 0.15 |
| 4 / 43 | Plaintext templates in `device_commands` | Enroll template is handed to the GUI **once** then removed from the row (`template_collected: true`); `params` (duplicate-check templates) set NULL when a request ends; backend start scrubs old rows and deletes finished commands > 7 days (removed 40 copies here) |
| 5 | "30-35 CM" eye distance | "EYE ABOUT 8–10 CM FROM THE CAMERA" (estimate for the Wide lens; measure on the real camera). Photo hint 30–40 cm |
| 7 / 13 | Open API on every network | `require_caller` in app.py: device routes need the device's own `X-API-Key` (`DEVICE_API_KEYS` in `.env`; camera key can't use fingerprint routes, a key only acts for its own device id); everything else (GUI, photos) only from `GUI_ALLOWED_NETS` (default localhost). CORS removed. `setup_network.sh` adds an nftables rule: ports 3000/37020/8010 only on lo + wlan0 |
| 8 / 39 | Failed commit lost the fingerprints / half-created patients | One atomic `POST /api/patients` (see §2); draft kept on failure |
| 9 | "ALL SENSORS ONLINE" fixed text; ESP32 offline = 3 min wait | `device_last_seen` (updated on every authenticated device call), `GET /api/devices/status`; fingerprint request to an offline reader fails at once (`FINGERPRINT_DEVICE_OFFLINE`, 503); live dots on Identify + footer |
| 11 / 12 | Tapping a patient faked "99.8% CERTAINTY"; newest patient auto-selected | `simulateAuthenticate` and `ensureIdentifyDefaultPatient` removed. Tapping only selects (then CONFIRM by finger or iris); entering Identify clears any selection. There is no biometric-free way into a record now |
| 14 | Everything went through Supabase; 120 s timeouts | Supabase timeout 6 s (`SUPABASE_TIMEOUT_SECONDS`); fingerprint paths check the local tables only; `patient_cache` table: last copy of every viewed/created patient, served (marked `offline`) when Supabase is unreachable (list, search, record) |
| 15 | Fonts/icons from Google | Geist + Material Symbols in `fonts/` (served at `/fonts/`); Indic Noto still from Google or `fonts-noto-core` |
| 16 | Draft only in browser memory | Mirrored to backend **memory** (`/api/draft`, never disk, 2 h idle expiry), restored on page load |
| 18 | Fake Settings (calibrate, NIR strobe, key rotation, invented audit lines) | Settings rebuilt on `GET /api/system/status` + `GET /api/system/integrity` (decrypts every template, walks the audit chain, counts plaintext copies); fake actions removed; export renamed "patient list" |
| 21 | Sample data used an Unsplash URL | Generated placeholder portrait (data URL) |
| 22 | Names/errors inserted as raw HTML | `LifeLinkUtil.escapeHtml` / `esc()` on every data interpolation in `innerHTML` templates (104 lines). Tested with a `<img onerror>` name |
| 40 | `,` `(` `)` in search → 500 | `safe_search_term` |
| 41 | Audit log in localStorage | `audit_events` table (hash-chained), `POST/GET /api/audit` |
| 31 | Camera retried forever on a camera fault | Stops once `/health` says the request is gone |
| 35 | Unquoted systemd paths; `/etc/hosts` after hostname change | Quoted `ExecStart` (checked with `systemd-analyze verify`); hosts line fixed in `setup_network.sh`; camera key via `/etc/lifelink-camera.env` (asked by `install_pizero.sh`) |
| — | `translations.js` last row had no trailing comma, so the next appended row became `row[...]` (undefined) and crashed lifelink-i18n.js | Comma added |

Backend now applies `backend/local_schema.sql` on every start (mode
constraint incl. `identify`, `patient_cache`, `audit_events`).

## 9. Known open items

- **1:N fingerprint on real fingers is not yet confirmed**: the sensor
  commands work (template-vs-template 75 ms, identical → match, other
  fingers → no match), but a single-capture probe vs. a 3-scan enrolled
  template still has to be tried with a live finger.
- Iris is still 1:1 (needs a selected patient). A 1:N iris search could be
  done on the Pi 5 (engine compares embeddings), not built.
- Camera-side items from the deep audit are still open (need the real
  Pi Zero + CM3 Wide): autofocus (`AfMode`), aiming phase, always-on Pi Zero,
  6→2–3 buffers, engine double pass, threshold recalibration, head tilt.
- No biometric-free override into a record (removed with the fake auth).
  Decide if an audited "open without biometric" is needed for patients
  whose fingers/eyes can't be scanned.
- Data sent over the Pi 5 Wi-Fi is plain HTTP (templates to the ESP32).
- No backups (key, local DB, uploads).

- Deep audit 2026-09-29/30: items 1–4, 8, 9, 10 and the 2026-09-30 list
  are fixed (see the bug log above). Still open: camera distance/focus
  measurement on the real CM3 Wide, aiming phase.
- **Pi 5 deployment checklist** (review 2026-09-29): commit `iris-module/`
  first (untracked); fix the unquoted systemd paths (above); install
  Postgres + create the `lifelink` role/db by hand (not scripted), apply
  fingerprint then iris schema; copy `backend/.env` (same
  `TEMPLATE_ENCRYPTION_KEY` if migrating data with `pg_dump`); ESP32
  `config.py` → Pi 5 AP SSID + `BACKEND_BASE_URL=http://192.168.50.1:3000`,
  reflash; Pi 5 needs Ethernet internet (Supabase); kiosk must open
  `http://localhost:3000`; `setup_network.sh` doesn't update `/etc/hosts`
  after the hostname change; time one iris `/extract` on the Pi 5 (backend
  engine timeout is 30 s); check the Pi camera's focus for close iris
  shots (no `AfMode` set).

- **Iris on real hardware**: Pi 5 AP/DHCP/mDNS scripts, systemd units and
  the Picamera2 path have never run on a Pi. Next step: run the setup
  scripts, enroll + identify one real person, check Pi 5 timing.
- Iris page labels still say "850NM NIR ILLUMINATOR ARMED", "Dual-NIR",
  "2048 × 1536 NIR" — the model is visible-light. Cosmetic.
- No cross-patient duplicate check (same person registered twice).
- `README.md` is outdated (says Tailwind CDN, `server.py`, simulated iris);
  `fingerprint-device/README.md` still describes Fernet and per-template
  `/api/enroll` linking.
- Test patient LL-6658-M uses demo demographics and a generated photo.
- The inlined Tailwind bundle can silently miss classes. When adding new
  utility classes, check they exist or add them to the supplemental
  `<style>` block.
- `/favicon.ico` 404 on every page load (harmless).

---

## 10. Decisions

- **Biometrics stay in the local Postgres, not Supabase** (user decision,
  2026-09-27). A move to Supabase was built and tested, then reverted at the
  user's request because local is simpler and already working. Notes if it
  is ever revisited:
  - Don't use pgcrypto with the key inside the SQL on Supabase (the key would
    reach the provider) — encrypt in Python instead.
  - The Supabase project already has **old, unused** `fingerprint_templates`
    (uuid ids, `template_data_encrypted text`, empty) and `device_commands`
    (9 stale rows from 2026-09-26, one `result` holding an **unencrypted
    template copy**) from an earlier design. The backend never uses them.
    Candidates for deletion — not done, awaiting the user.
  - Supabase already has RLS enabled (no policies) on all tables, but
    `anon`/`authenticated` still hold full table grants.

## 11. Repo state

- `main` pushed to GitHub at `40a3e2e` (backend + fingerprint device).
- Iris integration (`iris-module/`, backend/frontend changes, this file)
  is **not committed yet** as of 2026-09-27.
