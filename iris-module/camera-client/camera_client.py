#!/usr/bin/env python3
"""
LifeLink iris camera client — Raspberry Pi Zero autonomous camera unit.

Started at boot by lifelink-iris-camera.service; no manual steps. State
machine (see iris-module/README.md):

  CONNECTING  find the Pi 5 (main-pi.local, then 192.168.50.1) via GET /health
  READY       wait until /health says ready (the operator asked for an iris
              scan or a patient photo in the LifeLink GUI)
  SCANNING    iris: capture frames, pick the best eye crop (Stage A:
              sharpness, exposure, glare), stop the camera as soon as one is
              good enough. photo: let exposure settle, send the sharpest
              square face frame
  SENDING     POST /capture {capture_id, device_id, timestamp, image_hash, image_data}
  WAITING     SUCCESS -> shut down (or back to READY with --no-shutdown)
              RESCAN  -> SCANNING (new frame)
              RETRY / network error -> SENDING (same frame, same capture_id)

Frames are held in memory only; nothing is written to disk. No torch here:
the Pi 5 runs segmentation and recognition.

Dev on a PC:
  python camera_client.py --host localhost --source 0 --no-shutdown
"""

import argparse
import base64
import hashlib
import json
import logging
import secrets
import subprocess
import sys
import threading
import time
from datetime import datetime, timezone
from pathlib import Path
from urllib import error as urlerror, parse as urlparse, request as urlrequest

import cv2

PROJECT_ROOT = Path(__file__).resolve().parent
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))

from src.camera.camera_manager import RaspberryPiCamera  # noqa: E402
from src.quality.frame_selector import FrameSelector  # noqa: E402

logger = logging.getLogger("IrisCamera")

DEFAULT_HOSTS = ["main-pi.local", "192.168.50.1"]
NETWORK_ERRORS = (urlerror.URLError, TimeoutError, ConnectionError, OSError)


class IrisCameraClient:
    def __init__(self, args):
        self.hosts = args.host or DEFAULT_HOSTS
        self.port = args.port
        self.device_id = args.device_id
        self.camera_backend = args.camera
        self.source = args.source
        self.max_frames = args.max_frames
        self.quality_threshold = args.quality_threshold
        self.shutdown_on_success = not args.no_shutdown
        self.base_url = None
        # Capture ids restart at 1 every boot; the boot token keeps them
        # unique for the Pi 5's duplicate-capture check.
        self.boot_token = secrets.token_hex(3)
        self.capture_counter = 0
        self.selector = FrameSelector()
        # Latest preview frame waiting to be posted (a newer one replaces
        # it), sent by a background thread so scanning never waits on it.
        self._preview_frame = None
        self._preview_ready = threading.Event()
        threading.Thread(target=self._preview_sender, daemon=True).start()

    # ------------------------------------------------------------ http --
    def _request(self, method, url, payload=None, timeout=10):
        data = json.dumps(payload).encode() if payload is not None else None
        req = urlrequest.Request(url, data=data, method=method,
                                 headers={"Content-Type": "application/json"})
        with urlrequest.urlopen(req, timeout=timeout) as resp:
            return json.loads(resp.read())

    def health(self, base_url, timeout=3):
        query = urlparse.urlencode({"device_id": self.device_id})
        return self._request("GET", f"{base_url}/health?{query}", timeout=timeout)

    def queue_preview(self, frame_rgb):
        self._preview_frame = frame_rgb
        self._preview_ready.set()

    def _preview_sender(self):
        """Posts low-res frames to /preview so the GUI can show the operator
        where the eye is. Best effort: failures are ignored."""
        while True:
            self._preview_ready.wait()
            self._preview_ready.clear()
            frame, self._preview_frame = self._preview_frame, None
            if frame is None or not self.base_url:
                continue
            h, w = frame.shape[:2]
            small = cv2.resize(frame, (480, int(h * 480 / w)))
            ok, jpeg = cv2.imencode(".jpg", cv2.cvtColor(small, cv2.COLOR_RGB2BGR),
                                    [int(cv2.IMWRITE_JPEG_QUALITY), 70])
            if not ok:
                continue
            try:
                self._request("POST", f"{self.base_url}/preview", {
                    "device_id": self.device_id,
                    "image_data": base64.b64encode(jpeg.tobytes()).decode(),
                }, timeout=2)
            except NETWORK_ERRORS + (ValueError,):
                pass

    # ---------------------------------------------------------- states --
    def connect(self):
        """CONNECTING: retry until some host answers /health."""
        while True:
            for host in self.hosts:
                url = f"http://{host}:{self.port}"
                try:
                    if self.health(url).get("status") == "ok":
                        logger.info("Pi 5 available at %s", url)
                        self.base_url = url
                        return
                except NETWORK_ERRORS + (ValueError,):
                    pass
            logger.info("Connecting to Pi 5...")
            time.sleep(3)

    def wait_until_ready(self):
        """READY: the Pi 5 says ready once the GUI has requested a scan.
        Returns what to capture: "iris" or "photo"."""
        last_log = 0.0
        while True:
            try:
                health = self.health(self.base_url)
                if health.get("ready"):
                    return health.get("request", "iris")
            except NETWORK_ERRORS + (ValueError,):
                logger.warning("Lost the Pi 5; reconnecting")
                self.connect()
                continue
            if time.time() - last_log > 30:
                logger.info("Pi 5 connected; waiting for an iris scan request from the GUI")
                last_log = time.time()
            time.sleep(1)

    def scan(self):
        """SCANNING: returns (jpeg_bytes, quality) of the best eye crop. The
        camera is stopped before returning."""
        camera = RaspberryPiCamera(backend=self.camera_backend, source=self.source)
        camera.start()
        best = None
        try:
            for index in range(self.max_frames):
                ok, frame = camera.read()
                if not ok:
                    break
                if index % 3 == 0:
                    self.queue_preview(frame)
                cand = self.selector.process_frame(frame, frame_index=index)
                key = (cand.quality_metrics.valid, cand.quality_metrics.quality_score)
                if best is None or key > (best.quality_metrics.valid, best.quality_metrics.quality_score):
                    best = cand
                if cand.quality_metrics.valid and cand.quality_metrics.quality_score >= self.quality_threshold:
                    break
        finally:
            camera.stop()
        if best is None:
            raise RuntimeError("camera returned no frames")

        ok, jpeg = cv2.imencode(".jpg", cv2.cvtColor(best.eye_crop, cv2.COLOR_RGB2BGR),
                                [int(cv2.IMWRITE_JPEG_QUALITY), 95])
        if not ok:
            raise RuntimeError("JPEG encode failed")
        return jpeg.tobytes(), best.quality_metrics.quality_score

    def scan_photo(self, settle_frames=15, candidate_frames=15):
        """SCANNING (patient photo): skips the first frames while exposure and
        white balance settle, then returns (jpeg_bytes, sharpness) of the
        sharpest centre-square frame. The camera is stopped before returning."""
        camera = RaspberryPiCamera(backend=self.camera_backend, source=self.source)
        camera.start()
        best, best_sharpness = None, -1.0
        try:
            for index in range(settle_frames + candidate_frames):
                ok, frame = camera.read()
                if not ok:
                    break
                if index % 3 == 0:
                    self.queue_preview(frame)
                if index < settle_frames:
                    continue
                h, w = frame.shape[:2]
                side = min(h, w)
                square = frame[(h - side) // 2:(h + side) // 2, (w - side) // 2:(w + side) // 2]
                gray = cv2.cvtColor(cv2.resize(square, (320, 320)), cv2.COLOR_RGB2GRAY)
                sharpness = float(cv2.Laplacian(gray, cv2.CV_64F).var())
                if sharpness > best_sharpness:
                    best, best_sharpness = square, sharpness
        finally:
            camera.stop()
        if best is None:
            raise RuntimeError("camera returned no frames")

        ok, jpeg = cv2.imencode(".jpg", cv2.cvtColor(best, cv2.COLOR_RGB2BGR),
                                [int(cv2.IMWRITE_JPEG_QUALITY), 90])
        if not ok:
            raise RuntimeError("JPEG encode failed")
        return jpeg.tobytes(), best_sharpness

    def send_until_answered(self, capture_id, jpeg):
        """SENDING / WAITING: resends the same frame on RETRY or network
        errors until the Pi 5 answers SUCCESS or RESCAN."""
        payload = {
            "capture_id": capture_id,
            "device_id": self.device_id,
            "timestamp": datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ"),
            "image_hash": hashlib.sha256(jpeg).hexdigest(),
            "image_data": base64.b64encode(jpeg).decode(),
        }
        delay = 2
        while True:
            try:
                # The Pi 5 runs segmentation + the transformer; allow it time.
                answer = self._request("POST", f"{self.base_url}/capture", payload, timeout=60)
                status = answer.get("status", "RETRY")
                if status in ("SUCCESS", "RESCAN"):
                    return status, answer.get("reason")
                logger.warning("RETRY (%s); resending %s in %d s", answer.get("reason"), capture_id, delay)
            except NETWORK_ERRORS + (ValueError,) as err:
                logger.warning("Send failed (%s); resending %s in %d s", err, capture_id, delay)
                time.sleep(delay)
                self.connect()
                delay = min(delay * 2, 10)
                continue
            time.sleep(delay)
            delay = min(delay * 2, 10)

    def shutdown(self):
        logger.info("SUCCESS — shutting down")
        subprocess.run(["sudo", "-n", "shutdown", "-h", "now"], check=False)
        sys.exit(0)

    def run(self):
        logger.info("Iris camera %s starting (boot %s)", self.device_id, self.boot_token)
        self.connect()
        while True:
            wanted = self.wait_until_ready()
            while True:
                try:
                    if wanted == "photo":
                        jpeg, sharpness = self.scan_photo()
                        detail = f"patient photo, sharpness {sharpness:.0f}"
                    else:
                        jpeg, quality = self.scan()
                        detail = f"best eye frame quality {quality:.1f}"
                except Exception as err:  # camera missing / unplugged
                    logger.error("Camera error: %s; retrying in 3 s", err)
                    time.sleep(3)
                    continue
                self.capture_counter += 1
                capture_id = f"CAPTURE-{self.boot_token}-{self.capture_counter:04d}"
                logger.info("%s: %s, sending", capture_id, detail)
                status, reason = self.send_until_answered(capture_id, jpeg)
                if status == "RESCAN":
                    logger.info("RESCAN (%s)", reason)
                    continue
                logger.info("SUCCESS%s", f" ({reason})" if reason else "")
                break
            if self.shutdown_on_success:
                self.shutdown()


def main():
    parser = argparse.ArgumentParser(description="LifeLink iris camera client (Pi Zero)")
    parser.add_argument("--host", action="append",
                        help="Pi 5 host to try (repeatable). Default: main-pi.local, then 192.168.50.1")
    parser.add_argument("--port", type=int, default=3000, help="LifeLink backend port on the Pi 5")
    parser.add_argument("--device-id", default="iris-cam-1", help="Must match IRIS_DEVICE_ID in index.html")
    parser.add_argument("--camera", default="opencv_video", choices=["opencv_video", "picamera2"])
    parser.add_argument("--source", default="0", help="Webcam index, video file or image (opencv_video only)")
    parser.add_argument("--max-frames", type=int, default=30, help="Frames examined per scan")
    parser.add_argument("--quality-threshold", type=float, default=90.0,
                        help="Stop scanning early at this Stage A quality score")
    parser.add_argument("--no-shutdown", action="store_true",
                        help="Stay running after SUCCESS (development)")
    args = parser.parse_args()

    logging.basicConfig(level=logging.INFO, format="%(asctime)s [%(levelname)s] %(name)s: %(message)s")
    IrisCameraClient(args).run()


if __name__ == "__main__":
    main()
