"""
Eye ROI (Region of Interest) Crop Module.
Extracts the target close-up eye region from full camera frames.
"""

from typing import Tuple, Optional
import numpy as np


class EyeROICropper:
    """Extracts eye ROI from scanner frame."""

    def __init__(
        self,
        x: Optional[int] = None,
        y: Optional[int] = None,
        width: Optional[int] = None,
        height: Optional[int] = None,
        rel_x: float = 0.15,
        rel_y: float = 0.15,
        rel_width: float = 0.70,
        rel_height: float = 0.70,
    ):
        self.x = x
        self.y = y
        self.width = width
        self.height = height
        self.rel_x = rel_x
        self.rel_y = rel_y
        self.rel_width = rel_width
        self.rel_height = rel_height

    def get_roi_box(self, frame_shape: Tuple[int, int]) -> Tuple[int, int, int, int]:
        """Calculates absolute (x, y, w, h) ROI bounding box for given frame size."""
        H, W = frame_shape[:2]

        if (
            self.x is not None
            and self.y is not None
            and self.width is not None
            and self.height is not None
        ):
            x1 = max(0, min(W - 1, self.x))
            y1 = max(0, min(H - 1, self.y))
            w = max(1, min(W - x1, self.width))
            h = max(1, min(H - y1, self.height))
            return x1, y1, w, h

        x1 = int(round(W * self.rel_x))
        y1 = int(round(H * self.rel_y))
        w = int(round(W * self.rel_width))
        h = int(round(H * self.rel_height))

        x1 = max(0, min(W - 1, x1))
        y1 = max(0, min(H - 1, y1))
        w = max(1, min(W - x1, w))
        h = max(1, min(H - y1, h))

        return x1, y1, w, h

    def crop(self, frame: np.ndarray) -> Tuple[np.ndarray, Tuple[int, int, int, int]]:
        """Crops eye ROI from frame.

        Args:
            frame: (H, W, 3) or (H, W) frame image.

        Returns:
            cropped_eye: ROI sub-image.
            bbox: (x, y, w, h) absolute coordinates in original frame.
        """
        if frame is None or frame.size == 0:
            raise ValueError("Empty or invalid frame passed to EyeROICropper.")

        x, y, w, h = self.get_roi_box(frame.shape)
        cropped = frame[y : y + h, x : x + w].copy()
        return cropped, (x, y, w, h)
