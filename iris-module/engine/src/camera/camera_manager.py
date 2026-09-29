"""
Raspberry Pi Camera & Video Input Hardware Abstraction Layer.
Supports Picamera2/libcamera on physical Raspberry Pi hardware and
OpenCV video/image backends for PC development and testing.
"""

from abc import ABC, abstractmethod
from typing import Tuple, Generator, Optional
import os
import time
import numpy as np
import cv2


class BaseCameraBackend(ABC):
    """Abstract base camera backend."""

    @abstractmethod
    def start(self):
        pass

    @abstractmethod
    def read(self) -> Tuple[bool, Optional[np.ndarray]]:
        pass

    @abstractmethod
    def stop(self):
        pass


class Picamera2Backend(BaseCameraBackend):
    """Native Raspberry Pi Picamera2 / libcamera hardware backend."""

    def __init__(self, width: int = 1280, height: int = 720, fps: int = 30):
        self.width = width
        self.height = height
        self.fps = fps
        self.picam2 = None
        self.is_running = False

    def start(self):
        try:
            from picam2 import Picamera2  # type: ignore
        except ImportError:
            raise RuntimeError(
                "Picamera2 module is not installed. Ensure you are running on Raspberry Pi OS with picamera2."
            )

        self.picam2 = Picamera2()
        config = self.picam2.create_video_configuration(
            main={"size": (self.width, self.height), "format": "RGB888"}
        )
        self.picam2.configure(config)
        self.picam2.start()
        self.is_running = True
        time.sleep(0.5)

    def read(self) -> Tuple[bool, Optional[np.ndarray]]:
        if not self.is_running or self.picam2 is None:
            return False, None

        frame = self.picam2.capture_array()
        if frame is None or frame.size == 0:
            return False, None

        return True, frame

    def stop(self):
        if self.picam2 is not None and self.is_running:
            self.picam2.stop()
            self.is_running = False


class OpenCVVideoBackend(BaseCameraBackend):
    """Video file, image file, or USB webcam input backend."""

    def __init__(
        self,
        source: str = "0",
        width: int = 1280,
        height: int = 720,
        fps: int = 30,
        loop: bool = True,
    ):
        self.source = source
        self.width = width
        self.height = height
        self.fps = fps
        self.loop = loop
        self.cap = None
        self.is_net = False
        self.is_image = False
        self.image_frame = None

    def start(self):
        src_str = str(self.source)

        # Static image file check
        if src_str.lower().endswith((".jpg", ".jpeg", ".png", ".bmp")):
            img = cv2.imread(src_str)
            if img is None:
                raise FileNotFoundError(f"Could not load image file: {self.source}")
            img_rgb = cv2.cvtColor(img, cv2.COLOR_BGR2RGB)
            self.image_frame = img_rgb
            self.is_image = True
            return

        # Webcam or Video file or Network Stream URL
        if src_str.isdigit():
            cap_src = int(src_str)
            self.cap = cv2.VideoCapture(cap_src)
        else:
            # Handle VLC custom protocol strings like tsp:// -> rtsp:// or tcp/h264:// -> tcp://
            if src_str.startswith("tsp://"):
                cap_src = "rtsp://" + src_str[len("tsp://"):]
            elif src_str.startswith("tcp/h264://"):
                cap_src = "tcp://" + src_str[len("tcp/h264://"):]
            elif src_str.startswith("tcp/raw://"):
                cap_src = "tcp://" + src_str[len("tcp/raw://"):]
            else:
                cap_src = src_str

            self.is_net = any(cap_src.startswith(proto) for proto in ("rtsp://", "http://", "https://", "tcp://", "udp://"))
            if self.is_net:
                # Force RTSP over TCP, low-delay flags, and 5-second timeout in FFmpeg
                os.environ["OPENCV_FFMPEG_CAPTURE_OPTIONS"] = "rtsp_transport;tcp|timeout;5000000|fflags;nobuffer|flags;low_delay|framedrop;1"
                for attempt in range(2):
                    self.cap = cv2.VideoCapture(cap_src, cv2.CAP_FFMPEG)
                    if self.cap.isOpened():
                        break
                    time.sleep(0.3)
            else:
                self.cap = cv2.VideoCapture(cap_src)

        if not self.cap.isOpened():
            err_msg = (
                f"Failed to open video source: '{self.source}' (Attempted URL: '{cap_src}')\n"
                "Diagnostics for Network Stream Connection Failure:\n"
                "  1. Network Timeout (Error -138): No active server is listening at that IP/port.\n"
                "  2. Verify VLC Stream Setup on sender (10.199.95.248):\n"
                "     - RTSP mode (Recommended): Stream URL 'rtsp://10.199.95.248:8554/live'\n"
                "     - HTTP mode: Stream URL 'http://10.199.95.248:8080'\n"
                "  3. Firewall Check: Ensure port 8554/8080 is open in Windows Defender Firewall."
            )
            raise RuntimeError(err_msg)

        self.cap.set(cv2.CAP_PROP_FRAME_WIDTH, self.width)
        self.cap.set(cv2.CAP_PROP_FRAME_HEIGHT, self.height)
        if self.is_net:
            self.cap.set(cv2.CAP_PROP_BUFFERSIZE, 1)

    def read(self) -> Tuple[bool, Optional[np.ndarray]]:
        if self.is_image:
            return True, self.image_frame.copy()

        if self.cap is None or not self.cap.isOpened():
            return False, None

        # For network streams, flush queued stale frames to get latest real-time frame
        if self.is_net:
            for _ in range(2):
                if not self.cap.grab():
                    break

        ret, frame = self.cap.read()
        if not ret:
            if self.loop and not self.is_net:
                self.cap.set(cv2.CAP_PROP_POS_FRAMES, 0)
                ret, frame = self.cap.read()

        if not ret or frame is None:
            return False, None

        frame_rgb = cv2.cvtColor(frame, cv2.COLOR_BGR2RGB)
        return True, frame_rgb

    def stop(self):
        if self.cap is not None:
            self.cap.release()
            self.cap = None


class RaspberryPiCamera:
    """Unified camera manager handling hardware and software backends."""

    def __init__(
        self,
        backend: str = "opencv_video",
        source: str = "0",
        width: int = 1280,
        height: int = 720,
        fps: int = 30,
    ):
        self.backend_name = backend.lower()

        if self.backend_name == "picamera2":
            self.backend = Picamera2Backend(width=width, height=height, fps=fps)
        else:
            self.backend = OpenCVVideoBackend(
                source=source, width=width, height=height, fps=fps
            )

    def start(self):
        self.backend.start()

    def read(self) -> Tuple[bool, Optional[np.ndarray]]:
        return self.backend.read()

    def stop(self):
        self.backend.stop()

    def capture_sequence(
        self, num_frames: int = 15, delay_sec: float = 0.03
    ) -> Generator[np.ndarray, None, None]:
        """Yields a sequence of frames from camera stream."""
        captured = 0
        while captured < num_frames:
            ret, frame = self.read()
            if not ret:
                break
            yield frame
            captured += 1
            if delay_sec > 0:
                time.sleep(delay_sec)
