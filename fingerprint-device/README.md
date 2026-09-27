# LifeLink fingerprint device

Real GT-521Fx2 fingerprint sensor support, imported from
[life-link-finger-print-](https://github.com/kingsleyjudewin/life-link-finger-print-)
and adapted to talk to this repo's own Flask + Supabase backend instead of
that project's separate FastAPI service.

- `fplibMicro.py` — MicroPython driver for the GT-521Fx2 sensor (enroll,
  verify, identify, template read/write over UART).
- `firmware/` — ESP32 firmware (MicroPython) that wires the sensor to
  `backend/app.py`'s `/api/devices/...`, `/api/enroll`, `/api/verify`, and
  `/api/patients/<id>/templates` endpoints.

The original repo also has a FastAPI backend, a Postgres schema, and a
React frontend — none of that is used here. This app's own
`backend/app.py` and `backend/schema.sql` implement the same contract
directly (see the "fingerprint" section at the bottom of `app.py`), so the
firmware needs no code changes, only a config update.

## How it fits together

1. Frontend "Start Enrollment" → `POST /api/devices/{id}/commands`
   `{mode: "enroll"}` → device polls, captures 3 scans, reports back
   `{template_data, device_slot}` → frontend holds that in the
   registration draft.
2. Once the patient is actually created (`POST /api/patients`), the
   frontend calls `POST /api/enroll` with the real `patient_id`
   (lifelink_id), linking the captured template to that patient. Template
   bytes are encrypted at rest with `TEMPLATE_ENCRYPTION_KEY`
   (`backend/.env`, Fernet) before being stored.
3. Identify page "SCAN FINGER" (after picking a candidate patient) →
   `POST /api/devices/{id}/commands` `{mode: "verify", patient_id}` →
   device fetches that patient's stored templates via
   `GET /api/patients/{id}/templates`, loads them into temporary scratch
   slots (`SetTemplate`), captures a live scan, runs the real 1:1
   comparison **on the sensor itself** (`Verify1_1`), wipes the scratch
   slots, and reports back `{match, confidence}`.

**The sensor stores nothing permanently.** The local Postgres
(`fingerprint_templates`, encrypted) is the only template store. Enroll
uses scratch slot `SCRATCH_SLOT` (199) just long enough to read the merged
498-byte template back, then deletes it.

Operator cues on the sensor backlight: **steady** = place finger,
**flashing** = lift finger. Enroll takes 3 separate placements.

## Setting up the ESP32

1. Copy `firmware/config.example.py` to `firmware/config.py` and fill in
   your WiFi credentials. **Never commit `config.py`** — it's gitignored.
2. Set `BACKEND_BASE_URL` to this machine's LAN address and port 3000 (not
   `localhost` — the ESP32 is a separate device on the network). Find the
   current address with:
   ```
   hostname -I
   ```
   e.g. `BACKEND_BASE_URL = "http://10.20.221.243:3000"`. This changes
   whenever you switch networks — update and reflash `config.py` if so.
3. Set `DEVICE_ID` — must match the `FP_DEVICE_ID` constant used in
   `index.html` (defaults to `dev-esp32-1`).
4. Wiring and flashing steps are unchanged from the original project — see
   the GT-521Fx2-to-ESP32 pinout and `esptool`/`mpremote` commands below.

### Wiring

| GT-521Fx2 pin | ESP32 pin |
|---|---|
| TX | GPIO16 (RX2) |
| RX | GPIO17 (TX2) |
| GND | GND |
| VCC | 3V3 (VIN/5V did not power it reliably on this board) |

Don't use GPIO1/GPIO3 (UART0) — that fights with the USB-serial console.

If the boot log says `GPIO16 is floating`, the sensor has no power/GND or
its TX wire is off. Nothing on the ESP32 can fix that; reseat the wires
(the sensor's own 4-pin JST cable is the usual culprit).
Push button between `config.BUTTON_PIN` (GPIO14) and GND.

### Flashing

```
esptool --port /dev/ttyUSB0 erase-flash
esptool --port /dev/ttyUSB0 --baud 460800 write-flash 0x1000 ESP32_GENERIC-*.bin
mpremote connect /dev/ttyUSB0 fs cp fplibMicro.py :fplibMicro.py
mpremote connect /dev/ttyUSB0 fs cp firmware/device_api.py :device_api.py
mpremote connect /dev/ttyUSB0 fs cp firmware/modes.py :modes.py
mpremote connect /dev/ttyUSB0 fs cp firmware/config.py :config.py
mpremote connect /dev/ttyUSB0 fs cp firmware/main.py :main.py
mpremote connect /dev/ttyUSB0 run main.py
```

`urequests` isn't vendored — see the original repo's firmware README if
`mpremote mip install urequests` fails with a TLS error on your network.

## Not carried over

- The original repo's tamper-evident `audit_log` (hash-chained action log)
  and API-key auth on device requests — out of scope for this pass. The
  original `db/schema.sql`/`backend/app/audit.py` show the design if you
  want it added later.
- `tools/usb_bridge.py` (a USB-tethered fallback) — this integration is
  WiFi-only, matching how the firmware already talks to the backend.
