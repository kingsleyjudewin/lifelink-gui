# LifeLink iris module

Visible-light iris recognition, imported from
[LifeLink_Iris_module](https://github.com/Mervin0266/LifeLink_Iris_module)
and wired into this repo's backend, GUI and local Postgres, following the
Raspberry Pi 5 ↔ Pi Zero architecture.

| Folder | Runs on | What |
|---|---|---|
| `engine/` | Pi 5 | LightIrisNet segmentation → Daugman 64×512 normalization → VIS-IrisFormer embeddings, cosine match at 0.84 (`config/matching_threshold.json`). `iris_service.py` is a **stateless** service on `127.0.0.1:8010`, called only by the backend. |
| `camera-client/` | Pi Zero | Autonomous camera unit: boots, finds the Pi 5, scans, sends the best frame, shuts down on SUCCESS. No torch. |
| `db/schema.sql` | Pi 5 | `iris_templates` + `iris_captures` in the local `lifelink` Postgres. |
| `pi5-setup/` | Pi 5 | Private Wi-Fi + DHCP + `main-pi.local`, and boot services. |

## Network

```
Internet ── Ethernet ── Pi 5 (main-pi.local, 192.168.50.1)
                          │  Wi-Fi access point + DHCP + mDNS
                          ├── Pi Zero + camera (192.168.50.x)
                          └── ESP32 fingerprint (192.168.50.x)
```

The Pi Zero never needs internet. The Pi 5 reaches Supabase over Ethernet.

## Protocol

The backend (`backend/app.py`, port 3000 — the same server as the GUI and
the ESP32) exposes two endpoints for the camera:

| | Request | Answer |
|---|---|---|
| `GET /health?device_id=iris-cam-1` | — | `{status: "ok", ready}` — `ready` is true only while the GUI has an active iris request for this camera |
| `POST /capture` | `{capture_id, device_id, timestamp, image_hash, image_data}` | `{capture_id, status, reason?}` |

- **SUCCESS** — processed (template stored / match decided), or there was
  nothing to do. The Pi Zero shuts down.
- **RESCAN** — the engine rejected the frame (segmentation failed). The Pi
  Zero scans again, indefinitely, until the request expires (5 min).
- **RETRY** — SHA-256 mismatch or engine unreachable. The Pi Zero resends
  the **same** frame and capture_id.
- Final answers are recorded per `capture_id` (`iris_captures`): a resent
  capture gets the same answer, never processed twice. Capture ids are
  `CAPTURE-<boot token>-0001…` so they stay unique across reboots.

Pi Zero state machine (`camera_client.py`):

```
BOOT → CONNECTING (main-pi.local, then 192.168.50.1: GET /health, retry every 3 s)
     → READY (poll /health until ready)
     → SCANNING (up to 30 frames; stop early at quality ≥ 90) → camera stopped
     → SENDING → WAITING ─ SUCCESS → shutdown
                         ├ RESCAN  → SCANNING
                         └ RETRY / network error → SENDING (same frame)
```

## Operator flow

1. **Enroll**: in the GUI, Iris Enrollment → *CAPTURE IRIS* (right eye). The
   GUI says *POWER ON THE IRIS CAMERA*. Power on the Pi Zero; the patient
   looks into it. When it succeeds the Pi Zero powers off and the GUI moves
   to the left eye — click *CAPTURE IRIS* and power the Pi Zero on again.
2. Templates are stored **encrypted** (pgcrypto, `TEMPLATE_ENCRYPTION_KEY`),
   unlinked; the GUI only keeps the request ids. Committing the patient links
   both eyes (`POST /api/patients/<id>/iris-templates`).
3. **Identify**: pick the claimed patient → *SCAN IRIS* → power on the Pi
   Zero. 1:1 against that patient's two stored eyes.

The GUI waits up to 5 minutes per scan (covers the Pi Zero booting). A new
request cancels an older one. Camera frames are never written to disk and
template bytes never reach the browser.

## Setup — Pi 5

```bash
# 1. Network: Wi-Fi AP 192.168.50.1 with DHCP, hostname main-pi.local
sudo iris-module/pi5-setup/setup_network.sh <ssid> <password>

# 2. Iris engine
cd iris-module/engine
python3 -m venv .venv
.venv/bin/pip install torch torchvision --index-url https://download.pytorch.org/whl/cpu
.venv/bin/pip install -r requirements.txt
./download_models.sh                       # 453 MB checkpoint, SHA-256 checked

# 3. Local database (after fingerprint-device/db/schema.sql)
PGPASSWORD=… psql -U lifelink -h localhost -d lifelink -f iris-module/db/schema.sql

# 4. Start engine + backend at every boot
sudo iris-module/pi5-setup/install_services.sh
```

Set the ESP32's `BACKEND_BASE_URL` to `http://192.168.50.1:3000` and its
WiFi to the same SSID — the address no longer changes between networks.

## Setup — Pi Zero

```bash
sudo nmcli dev wifi connect <ssid> password <password>   # the Pi 5's network
scp -r iris-module/camera-client <user>@<pi-zero>:~/        # from the Pi 5
cd ~/camera-client && sudo ./install_pizero.sh
journalctl -u lifelink-iris-camera -f                       # watch it
```

`install_pizero.sh` installs OpenCV/Picamera2 from apt, a systemd service
that starts the client at boot, and a sudoers rule allowing only
`shutdown -h now`.

## Testing without the Pi hardware

On any PC running the backend + engine, the client can use a webcam, video
or image and skip the shutdown:

```bash
python camera_client.py --host localhost --source 0 --no-shutdown
```

For a webcam test: put your eye in the **middle** of the frame (only the
central 70% is used), close to the lens, with even bright light and no
lamp reflection in the pupil.

## Changed from the original module

- `server_pi5.py` → `/health` + `/capture` on the LifeLink backend plus the
  stateless `engine/iris_service.py`: no `.npy` template files, no saved
  captures, no auto-enroll of the first capture, one threshold (0.84), and
  segmentation is validated before a template is produced (the normalizer
  otherwise falls back to a default geometry and yields a template from any
  image). Port 3000 (shared with the GUI/ESP32) instead of 8000.
- `client_pizero.py` → `camera_client.py`: waits for a GUI request before
  scanning, unique capture ids, no torch import on the Pi Zero (the original
  imported the segmenter), systemd + sudoers installer.
- `Picamera2Backend` fixes: imported `picam2` (module is `picamera2`),
  returned B,G,R frames as RGB, never released the camera (a RESCAN would
  fail to reopen it).
