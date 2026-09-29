"""
Iris Image Quality Evaluation Module.
Computes local ISO/IEC 29794-6 inspired quality metrics for eye frames.
"""

from dataclasses import dataclass
import numpy as np
import cv2


@dataclass
class IrisQualityMetrics:
    sharpness: float
    brightness_mean: float
    contrast: float
    highlight_ratio: float
    shadow_ratio: float
    quality_score: float
    valid: bool
    reason: str


class IrisQualityEvaluator:
    """Evaluates image quality of close-up eye crops."""

    def __init__(
        self,
        min_sharpness: float = 95.0,
        min_brightness: float = 30.0,
        max_brightness: float = 220.0,
        max_highlight_ratio: float = 0.08,
        max_shadow_ratio: float = 0.35,
    ):
        self.min_sharpness = min_sharpness
        self.min_brightness = min_brightness
        self.max_brightness = max_brightness
        self.max_highlight_ratio = max_highlight_ratio
        self.max_shadow_ratio = max_shadow_ratio

    def evaluate(self, eye_image: np.ndarray) -> IrisQualityMetrics:
        """Evaluates image quality metrics for an eye crop.

        Args:
            eye_image: (H, W, 3) or (H, W) image.

        Returns:
            IrisQualityMetrics object.
        """
        if eye_image is None or eye_image.size == 0:
            return IrisQualityMetrics(
                sharpness=0.0,
                brightness_mean=0.0,
                contrast=0.0,
                highlight_ratio=1.0,
                shadow_ratio=1.0,
                quality_score=0.0,
                valid=False,
                reason="Empty image frame provided.",
            )

        if eye_image.ndim == 3:
            gray = cv2.cvtColor(eye_image, cv2.COLOR_RGB2GRAY)
        else:
            gray = eye_image.copy()

        if gray.dtype != np.uint8:
            gray = (np.clip(gray, 0.0, 1.0) * 255).astype(np.uint8)

        # 1. Sharpness: Variance of Laplacian
        laplacian_var = float(cv2.Laplacian(gray, cv2.CV_64F).var())

        # 2. Brightness mean & contrast (std)
        b_mean = float(gray.mean())
        contrast = float(gray.std())

        # 3. Saturation / specular highlights (pixels > 250)
        num_pixels = float(gray.size)
        highlight_ratio = float((gray > 250).sum()) / num_pixels

        # 4. Shadow clipping (pixels < 10)
        shadow_ratio = float((gray < 10).sum()) / num_pixels

        # Rejection rules
        valid = True
        reasons = []

        if laplacian_var < self.min_sharpness:
            valid = False
            reasons.append(f"Blurry image (sharpness {laplacian_var:.1f} < {self.min_sharpness})")

        if b_mean < self.min_brightness or b_mean > self.max_brightness:
            valid = False
            reasons.append(f"Bad exposure (mean brightness {b_mean:.1f})")

        if highlight_ratio > self.max_highlight_ratio:
            valid = False
            reasons.append(f"Excessive specular glare ({highlight_ratio:.3f} > {self.max_highlight_ratio})")

        if shadow_ratio > self.max_shadow_ratio:
            valid = False
            reasons.append(f"Excessive shadow ({shadow_ratio:.3f} > {self.max_shadow_ratio})")

        # Composite quality score (0.0 to 100.0)
        norm_sharpness = min(1.0, laplacian_var / 300.0)
        norm_brightness = 1.0 - abs(b_mean - 128.0) / 128.0
        norm_contrast = min(1.0, contrast / 64.0)

        quality_score = float(
            (0.50 * norm_sharpness + 0.30 * norm_brightness + 0.20 * norm_contrast) * 100.0
        )

        reason_str = "; ".join(reasons) if reasons else "Passed quality evaluation."

        return IrisQualityMetrics(
            sharpness=laplacian_var,
            brightness_mean=b_mean,
            contrast=contrast,
            highlight_ratio=highlight_ratio,
            shadow_ratio=shadow_ratio,
            quality_score=quality_score,
            valid=valid,
            reason=reason_str,
        )
