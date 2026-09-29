"""
Multi-Frame Quality Selection Engine.
Filters streaming video frames into top candidates using lightweight metrics,
then evaluates segmentation quality to select the best frame for iris recognition.
"""

from dataclasses import dataclass
from typing import List, Optional, Tuple
import numpy as np

from src.quality.iris_quality import IrisQualityEvaluator, IrisQualityMetrics
from src.vision.eye_roi import EyeROICropper
from src.models.lightirisnet_wrapper import LightIrisNetSegmenter
from src.vision.segmentation_validator import SegmentationValidator, SegmentationValidation


@dataclass
class FrameCandidate:
    frame_index: int
    frame: np.ndarray
    eye_crop: np.ndarray
    roi_bbox: Tuple[int, int, int, int]
    quality_metrics: IrisQualityMetrics
    segmentation_result: Optional[dict] = None
    validation: Optional[SegmentationValidation] = None
    final_score: float = 0.0


class FrameSelector:
    """Selects the highest quality eye frame from a video sequence."""

    def __init__(
        self,
        cropper: Optional[EyeROICropper] = None,
        quality_evaluator: Optional[IrisQualityEvaluator] = None,
        segmenter: Optional[LightIrisNetSegmenter] = None,
        validator: Optional[SegmentationValidator] = None,
        top_k_stage_a: int = 5,
    ):
        self.cropper = cropper or EyeROICropper()
        self.quality_evaluator = quality_evaluator or IrisQualityEvaluator()
        self.segmenter = segmenter
        self.validator = validator or SegmentationValidator()
        self.top_k_stage_a = top_k_stage_a

    def process_frame(
        self, frame: np.ndarray, frame_index: int = 0
    ) -> FrameCandidate:
        """Processes a single frame through Stage A quality filtering."""
        eye_crop, bbox = self.cropper.crop(frame)
        metrics = self.quality_evaluator.evaluate(eye_crop)

        candidate = FrameCandidate(
            frame_index=frame_index,
            frame=frame,
            eye_crop=eye_crop,
            roi_bbox=bbox,
            quality_metrics=metrics,
            final_score=metrics.quality_score if metrics.valid else 0.0,
        )
        return candidate

    def select_best_frame(
        self, frames: List[np.ndarray], segmenter_override: Optional[LightIrisNetSegmenter] = None
    ) -> FrameCandidate:
        """Selects the best frame from a list of video frames using two-stage filtering.

        Args:
            frames: List of (H, W, 3) RGB frame arrays.
            segmenter_override: Optional segmenter instance.

        Returns:
            Best FrameCandidate object.
        """
        if not frames:
            raise ValueError("Empty frame list provided to FrameSelector.")

        segmenter = segmenter_override or self.segmenter

        # Stage A: Fast quality scoring on all frames
        candidates: List[FrameCandidate] = []
        for idx, frame in enumerate(frames):
            cand = self.process_frame(frame, frame_index=idx)
            candidates.append(cand)

        # Filter valid frames and sort by quality score
        valid_stage_a = [c for c in candidates if c.quality_metrics.valid]
        if not valid_stage_a:
            # Fallback to candidate with highest score if none passed hard thresholds
            candidates.sort(key=lambda c: c.quality_metrics.quality_score, reverse=True)
            top_candidates = candidates[: self.top_k_stage_a]
        else:
            valid_stage_a.sort(key=lambda c: c.quality_metrics.quality_score, reverse=True)
            top_candidates = valid_stage_a[: self.top_k_stage_a]

        # Stage B: High-fidelity segmentation validation (if segmenter available)
        if segmenter is not None:
            best_candidate = None
            best_score = -1.0

            for cand in top_candidates:
                seg_res = segmenter.segment(cand.eye_crop)
                val_res = self.validator.validate(
                    seg_res["iris_mask"], seg_res["pupil_mask"], seg_res["diagnostics"]
                )

                cand.segmentation_result = seg_res
                cand.validation = val_res

                if val_res.valid:
                    comb_score = (
                        0.50 * cand.quality_metrics.quality_score
                        + 0.30 * (val_res.iris_area_ratio * 100.0)
                        + 0.20 * (val_res.pupil_inside_ratio * 100.0)
                    )
                else:
                    comb_score = cand.quality_metrics.quality_score * 0.20

                cand.final_score = float(comb_score)

                if comb_score > best_score:
                    best_score = comb_score
                    best_candidate = cand

            return best_candidate or top_candidates[0]

        return top_candidates[0]
