"""
Segmentation Mask Validation Module.
Checks geometric sanity and boundary validity of segmented iris and pupil masks
before feeding them to the normalization and feature extraction pipeline.
"""

from dataclasses import dataclass
import numpy as np


@dataclass
class SegmentationValidation:
    valid: bool
    iris_area_ratio: float
    pupil_area_ratio: float
    pupil_inside_ratio: float
    boundary_quality: float
    reason: str


class SegmentationValidator:
    """Validates iris and pupil segmentation masks for biometric quality."""

    def __init__(
        self,
        min_iris_area_ratio: float = 0.02,
        min_pupil_area_ratio: float = 0.003,
        min_pupil_inside_ratio: float = 0.85,
        min_pupil_to_iris_ratio: float = 0.05,
        max_pupil_to_iris_ratio: float = 0.80,
    ):
        self.min_iris_area_ratio = min_iris_area_ratio
        self.min_pupil_area_ratio = min_pupil_area_ratio
        self.min_pupil_inside_ratio = min_pupil_inside_ratio
        self.min_pupil_to_iris_ratio = min_pupil_to_iris_ratio
        self.max_pupil_to_iris_ratio = max_pupil_to_iris_ratio

    def validate(
        self, iris_mask: np.ndarray, pupil_mask: np.ndarray, diagnostics: dict = None
    ) -> SegmentationValidation:
        """Validates segmentation masks.

        Args:
            iris_mask: binary uint8 mask (H, W)
            pupil_mask: binary uint8 mask (H, W)
            diagnostics: optional diagnostic dictionary from LightIrisNetSegmenter

        Returns:
            SegmentationValidation dataclass instance.
        """
        H, W = iris_mask.shape
        img_area = float(H * W)

        iris_area = float((iris_mask > 0).sum())
        pupil_area = float((pupil_mask > 0).sum())

        iris_ratio = iris_area / img_area
        pupil_ratio = pupil_area / img_area

        if iris_ratio < self.min_iris_area_ratio:
            return SegmentationValidation(
                valid=False,
                iris_area_ratio=iris_ratio,
                pupil_area_ratio=pupil_ratio,
                pupil_inside_ratio=0.0,
                boundary_quality=0.0,
                reason=f"Iris area too small ({iris_ratio:.4f} < {self.min_iris_area_ratio})",
            )

        if pupil_ratio < self.min_pupil_area_ratio:
            return SegmentationValidation(
                valid=False,
                iris_area_ratio=iris_ratio,
                pupil_area_ratio=pupil_ratio,
                pupil_inside_ratio=0.0,
                boundary_quality=0.0,
                reason=f"Pupil area too small ({pupil_ratio:.4f} < {self.min_pupil_area_ratio})",
            )

        pupil_to_iris = pupil_area / max(1.0, iris_area)
        if (
            pupil_to_iris < self.min_pupil_to_iris_ratio
            or pupil_to_iris > self.max_pupil_to_iris_ratio
        ):
            return SegmentationValidation(
                valid=False,
                iris_area_ratio=iris_ratio,
                pupil_area_ratio=pupil_ratio,
                pupil_inside_ratio=0.0,
                boundary_quality=0.0,
                reason=f"Implausible pupil/iris area ratio ({pupil_to_iris:.3f})",
            )

        # Check pupil containment inside iris
        inside_area = float(((pupil_mask > 0) & (iris_mask > 0)).sum())
        pupil_inside_ratio = inside_area / max(1.0, pupil_area)

        if pupil_inside_ratio < self.min_pupil_inside_ratio:
            return SegmentationValidation(
                valid=False,
                iris_area_ratio=iris_ratio,
                pupil_area_ratio=pupil_ratio,
                pupil_inside_ratio=pupil_inside_ratio,
                boundary_quality=0.0,
                reason=f"Pupil not sufficiently inside iris ({pupil_inside_ratio:.3f} < {self.min_pupil_inside_ratio})",
            )

        # Border touch check (iris truncated at border)
        border_pixels = (
            iris_mask[0, :].sum()
            + iris_mask[-1, :].sum()
            + iris_mask[:, 0].sum()
            + iris_mask[:, -1].sum()
        )
        boundary_quality = 1.0 - float(border_pixels > 0) * 0.3

        return SegmentationValidation(
            valid=True,
            iris_area_ratio=iris_ratio,
            pupil_area_ratio=pupil_ratio,
            pupil_inside_ratio=pupil_inside_ratio,
            boundary_quality=boundary_quality,
            reason="Segmentation masks passed all validation checks.",
        )
