# LifeLink — Project Memory

A reference for anyone (human or AI) picking this project up later: how it is
built, how the fingerprint and iris hardware really behave, and every bug
that has been found and fixed. Last updated 2026-09-29.

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
- **Commit:** browser posts all 10 templates in one transaction to
  `POST /api/patients/<id>/templates`.
- **Verify:** device fetches the patient's templates, loads them into
  temporary slots 199, 198, … (`SetTemplate`), captures one live finger,
  runs `Verify1_1` against each slot, wipes the slots.
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
| POST | `/api/devices/<dev>/commands` | browser | Queue `{mode: enroll, exclude_templates}`, `{mode: verify, patient_id}`, `{mode: iris_enroll, eye_side: right/left}`, `{mode: iris_verify, patient_id}`, `{mode: photo_capture}` |
| GET | `/api/devices/<dev>/commands/pending` | ESP32 | Atomically claim oldest command (204 if none); includes `params` |
| GET | `/api/devices/<dev>/commands/<id>` | browser | Poll status/result (never returns `params`) |
| POST | `/api/devices/<dev>/commands/<id>/progress` | ESP32 | Live step `{stage: place/lift/retry, scan, of}` |
| POST | `/api/devices/<dev>/commands/<id>/result` | ESP32 | `done`/`error` + result |

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

## 9. Known open items

- **Deep audit 2026-09-29 (Pi 5 16 GB + Pi Zero camera + 5" 800×480 display), not yet fixed:**
  1. `index.html` line 13: supplemental `<style>` is never closed → the font
     `@import` and `:root` tokens are swallowed as invalid CSS; Material
     Symbols icons render as words ("arrow_back"), Inter never loads. Since `40a3e2e`.
  2. Fingerprint commands are never cancelled/expired: a GUI timeout leaves the
     command `pending`; the ESP32 later claims it first (oldest-first) and eats
     the operator's next placements. Iris/photo have TTL+cancel, fingerprint doesn't.
  3. Iris enroll page green box is 70% of the *wrapper*, but the image is
     `object-fit: contain` in a non-16:9 wrapper → at 800×480 box 531 px wide vs
     real engine crop 348 px (identify page is correct, it's 16:9).
  4. Plaintext templates in `device_commands`: 30 `result` rows + 168 copies in
     `params.exclude_templates` (measured on the laptop DB).
  5. Flask on 0.0.0.0 with no auth → on the Pi 5 the Ethernet (hospital) LAN can
     GET decrypted templates. Firewall port 3000 to lo + wlan0.
  6. Camera distance: validator needs iris ≥ 2% of the 896×504 crop (≈107 px
     diameter) → eye must be ≈10–12 cm from a CM3/CM2 at 1280×720; GUI says
     "32 CM". Needs focus set (CM3 AfMode) or higher capture resolution.
  7. Preview only exists during the ~1 s scan → no aiming time for the photo
     (face) or the eye; add an aiming phase.
  8. Commit: `resetDraft()` runs even when fingerprint storing failed → templates
     lost, patient exists without prints, no re-enroll path.
  9. No device-online signal: ESP32 offline → GUI waits 180 s; "ALL SENSORS
     ONLINE" is static text. Leaving the iris page doesn't cancel the scan.
  10. Pages overflow 800×480 (fingerprint 842 px, iris 1001 px tall).
  Verified OK: both model checkpoints load every key (LightIrisNet 420/420,
  IrisFormer 304/304; only the unused `ft_head` is skipped).

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
- Identify page "MANUAL QUERY & REGISTERED PATIENT ARCHIVE": clicking a
  patient calls `simulateAuthenticate` (fake 99.8% match, no biometric).
  Bypasses both fingerprint and iris — decide whether to keep as an
  emergency fallback.
- `device_commands.result` keeps an **unencrypted copy** of each captured
  fingerprint template. Scrub it once the browser has read it, or purge old
  rows. (Iris results hold no template bytes.)
- No authentication on any API (device, camera or browser). The original
  fingerprint module had API-key auth + a hash-chained audit log — not
  carried over.
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
