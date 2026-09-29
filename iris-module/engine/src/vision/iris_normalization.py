"""
Daugman's Rubber-Sheet Iris Normalization Module.
Unwraps the annular iris region bounded by pupil boundary and iris boundary
into a fixed polar coordinate grid of shape 64 x 512.
"""

from typing import Tuple, Dict, Any, Optional
import numpy as np
import cv2


class IrisNormalizer:
    """Rubber-Sheet Polar Iris Normalizer."""

    def __init__(self, target_height: int = 64, target_width: int = 512):
        self.target_height = target_height
        self.target_width = target_width

    def _extract_geometry(
        self,
        mask: np.ndarray,
        ellipse: Optional[Tuple[Tuple[float, float], Tuple[float, float], float]] = None,
    ) -> Tuple[float, float, float]:
        """Extracts center (cx, cy) and effective radius from binary mask or ellipse."""
        if ellipse is not None and ellipse[0][0] > 0 and ellipse[0][1] > 0:
            (cx, cy), (ma, mi), angle = ellipse
            r = (ma + mi) / 4.0
            return float(cx), float(cy), float(r)

        cnts, _ = cv2.findContours(
            mask.astype(np.uint8), cv2.RETR_EXTERNAL, cv2.CHAIN_APPROX_NONE
        )
        if cnts:
            cnt = max(cnts, key=cv2.contourArea)
            (cx, cy), radius = cv2.minEnclosingCircle(cnt)
            if radius > 0:
                return float(cx), float(cy), float(radius)

        # Fallback to image center and proportion if mask is empty
        H, W = mask.shape[:2]
        return float(W / 2.0), float(H / 2.0), float(min(H, W) / 6.0)

    def normalize(
        self,
        eye_image: np.ndarray,
        iris_mask: np.ndarray,
        pupil_mask: np.ndarray,
        iris_ellipse: Optional[Tuple] = None,
        pupil_ellipse: Optional[Tuple] = None,
    ) -> Dict[str, Any]:
        """Normalizes iris image to a 64 x 512 rectangular polar representation.

        Args:
            eye_image: (H, W, 3) RGB or (H, W) grayscale image.
            iris_mask: (H, W) binary mask for iris.
            pupil_mask: (H, W) binary mask for pupil.
            iris_ellipse: optional cv2 fitEllipse tuple for iris.
            pupil_ellipse: optional cv2 fitEllipse tuple for pupil.

        Returns:
            dict containing:
                - 'normalized_iris': (64, 512) grayscale image (uint8, 0-255)
                - 'normalization_mask': (64, 512) binary mask (1 = valid iris, 0 = noise/occlusion)
                - 'geometry': dictionary of extracted centers and radii
        """
        if eye_image.ndim == 3:
            gray = cv2.cvtColor(eye_image, cv2.COLOR_RGB2GRAY)
        else:
            gray = eye_image.copy()

        if gray.dtype != np.uint8:
            gray = (np.clip(gray, 0.0, 1.0) * 255).astype(np.uint8)

        cx_p, cy_p, r_p = self._extract_geometry(pupil_mask, pupil_ellipse)
        cx_i, cy_i, r_i = self._extract_geometry(iris_mask, iris_ellipse)

        if r_i <= r_p:
            r_i = r_p + 10.0  # fallback to prevent division by zero or negative radius

        # Create polar coordinate sampling mesh
        angles = np.linspace(0, 2 * np.pi, self.target_width, endpoint=False)
        rad_samples = np.linspace(0, 1, self.target_height)

        theta_grid, r_grid = np.meshgrid(angles, rad_samples)

        # Non-concentric mapping equations:
        # Pupil boundary points
        xp = cx_p + r_p * np.cos(theta_grid)
        yp = cy_p + r_p * np.sin(theta_grid)

        # Iris boundary points
        xi = cx_i + r_i * np.cos(theta_grid)
        yi = cy_i + r_i * np.sin(theta_grid)

        # Linear interpolation between pupil boundary (r_grid=0) and iris boundary (r_grid=1)
        map_x = ((1 - r_grid) * xp + r_grid * xi).astype(np.float32)
        map_y = ((1 - r_grid) * yp + r_grid * yi).astype(np.float32)

        # Resample image and masks using bilinear interpolation
        normalized_iris = cv2.remap(
            gray, map_x, map_y, cv2.INTER_LINEAR, borderMode=cv2.BORDER_CONSTANT, borderValue=0
        )

        norm_iris_mask = cv2.remap(
            iris_mask.astype(np.uint8),
            map_x,
            map_y,
            cv2.INTER_NEAREST,
            borderMode=cv2.BORDER_CONSTANT,
            borderValue=0,
        )

        norm_pupil_mask = cv2.remap(
            pupil_mask.astype(np.uint8),
            map_x,
            map_y,
            cv2.INTER_NEAREST,
            borderMode=cv2.BORDER_CONSTANT,
            borderValue=0,
        )

        # Valid iris pixel mask: inside iris mask and outside pupil mask
        normalization_mask = (norm_iris_mask > 0) & (norm_pupil_mask == 0)

        geometry = {
            "pupil_center": (cx_p, cy_p),
            "pupil_radius": r_p,
            "iris_center": (cx_i, cy_i),
            "iris_radius": r_i,
        }

        return {
            "normalized_iris": normalized_iris,
            "normalization_mask": normalization_mask.astype(np.uint8),
            "geometry": geometry,
        }
