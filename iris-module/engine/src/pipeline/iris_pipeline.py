"""
Iris Recognition Pipeline Orchestrator.
Connects hardware capture, frame selection, LightIrisNet segmentation,
rubber-sheet normalization, VIS-IrisFormer extraction, and template matching.
"""

import json
import logging
import time
from pathlib import Path
from typing import Dict, Any, Optional, Tuple
import cv2
import numpy as np

from src.camera.camera_manager import RaspberryPiCamera
from src.quality.frame_selector import FrameSelector, FrameCandidate
from src.quality.iris_quality import IrisQualityEvaluator
from src.vision.eye_roi import EyeROICropper
from src.vision.segmentation_validator import SegmentationValidator
from src.models.lightirisnet_wrapper import LightIrisNetSegmenter
from src.vision.iris_normalization import IrisNormalizer
from src.models.irisformer_wrapper import VISIrisFormerRecognizer
from src.recognition.template import IrisTemplate
from src.recognition.matcher import IrisMatcher, MatchResult
from src.storage.template_store import TemplateStore

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(name)s: %(message)s",
)
logger = logging.getLogger("IrisPipeline")


class IrisRecognitionPipeline:
    """End-to-end pipeline orchestrator for Raspberry Pi Iris Recognition."""

    def __init__(
        self,
        config: Optional[Dict[str, Any]] = None,
        device: Optional[str] = None,
        lazy_init: bool = False,
    ):
        self.config = config or {}
        self.device = device

        self.segmenter: Optional[LightIrisNetSegmenter] = None
        self.normalizer: Optional[IrisNormalizer] = None
        self.recognizer: Optional[VISIrisFormerRecognizer] = None
        self.store: Optional[TemplateStore] = None
        self.matcher: Optional[IrisMatcher] = None

        if not lazy_init:
            self.initialize_models()

    def initialize_models(self):
        """Initializes PyTorch models and storage once."""
        t0 = time.time()
        logger.info("Initializing pipeline models and components...")

        seg_ckpt = self.config.get("segmentation", {}).get("checkpoint_path")
        rec_ckpt = self.config.get("recognition", {}).get("checkpoint_path")

        self.segmenter = LightIrisNetSegmenter(
            checkpoint_path=seg_ckpt, device=self.device
        )
        self.normalizer = IrisNormalizer(target_height=64, target_width=512)
        self.recognizer = VISIrisFormerRecognizer(
            checkpoint_path=rec_ckpt, device=self.device
        )
        self.cropper = EyeROICropper()
        self.store = TemplateStore(
            base_dir=self.config.get("storage", {}).get("templates_dir", "templates")
        )
        threshold = self.config.get("matching", {}).get("threshold", 0.84)
        self.matcher = IrisMatcher(
            recognizer=self.recognizer, default_threshold=threshold
        )

        logger.info(f"Pipeline models initialized in {time.time() - t0:.2f}s")

    def capture_best_eye(
        self, source: Optional[str] = None, num_frames: int = 15
    ) -> FrameCandidate:
        """Captures a sequence of frames and selects the highest-quality eye candidate."""
        cam_backend = self.config.get("camera", {}).get("backend", "opencv_video")
        cam_source = source or self.config.get("camera", {}).get("source", "0")
        width = self.config.get("camera", {}).get("width", 1280)
        height = self.config.get("camera", {}).get("height", 720)

        camera = RaspberryPiCamera(
            backend=cam_backend, source=cam_source, width=width, height=height
        )

        cropper = EyeROICropper()
        evaluator = IrisQualityEvaluator()
        validator = SegmentationValidator()
        selector = FrameSelector(
            cropper=cropper,
            quality_evaluator=evaluator,
            segmenter=self.segmenter,
            validator=validator,
        )

        camera.start()
        try:
            frames = list(camera.capture_sequence(num_frames=num_frames, delay_sec=0.02))
        finally:
            camera.stop()

        if not frames:
            raise RuntimeError("Failed to capture any frames from camera backend.")

        best_cand = selector.select_best_frame(frames, segmenter_override=self.segmenter)
        return best_cand

    def process_eye_image(
        self, eye_crop: np.ndarray, subject_id: str = "probe", eye_side: str = "right"
    ) -> Tuple[IrisTemplate, Dict[str, Any]]:
        """Processes an eye ROI crop through segmentation, normalization, and feature extraction.

        Args:
            eye_crop: (H, W, 3) eye crop.
            subject_id: ID string.
            eye_side: 'left' or 'right'.

        Returns:
            template: IrisTemplate object.
            diagnostics: Timing and quality dictionary.
        """
        if self.segmenter is None or self.recognizer is None:
            self.initialize_models()

        t0 = time.time()

        # 1. Segmentation
        t_seg_0 = time.time()
        seg_res = self.segmenter.segment(eye_crop)
        t_seg = (time.time() - t_seg_0) * 1000.0

        # 2. Normalization
        t_norm_0 = time.time()
        norm_res = self.normalizer.normalize(
            eye_crop,
            seg_res["iris_mask"],
            seg_res["pupil_mask"],
            seg_res["iris_ellipse"],
            seg_res["pupil_ellipse"],
        )
        t_norm = (time.time() - t_norm_0) * 1000.0

        # 3. VIS-IrisFormer Feature Extraction
        t_emb_0 = time.time()
        embedding = self.recognizer.extract_template(norm_res["normalized_iris"])
        t_emb = (time.time() - t_emb_0) * 1000.0

        total_ms = (time.time() - t0) * 1000.0

        template = IrisTemplate(
            subject_id=subject_id,
            eye_side=eye_side,
            embedding=embedding,
            normalization_size=(64, 512),
        )

        diagnostics = {
            "timing_ms": {
                "segmentation": t_seg,
                "normalization": t_norm,
                "template_generation": t_emb,
                "total": total_ms,
            },
            "segmentation": seg_res["diagnostics"],
            "normalization": norm_res["geometry"],
            "segmentation_result": seg_res,
            "normalization_result": norm_res,
        }

        return template, diagnostics

    def save_debug_artifacts(
        self,
        candidate: FrameCandidate,
        norm_res: Dict[str, Any],
        output_dir: str = "debug",
    ):
        """Saves debugging visualizations (overlay, masks, normalized iris strip)."""
        out_path = Path(output_dir)
        out_path.mkdir(parents=True, exist_ok=True)

        cv2.imwrite(str(out_path / "original_eye.png"), cv2.cvtColor(candidate.eye_crop, cv2.COLOR_RGB2BGR))
        cv2.imwrite(str(out_path / "normalized_iris.png"), norm_res["normalized_iris"])
        cv2.imwrite(str(out_path / "normalization_mask.png"), norm_res["normalization_mask"] * 255)

        seg_res = candidate.segmentation_result
        if seg_res:
            cv2.imwrite(str(out_path / "iris_mask.png"), seg_res["iris_mask"] * 255)
            cv2.imwrite(str(out_path / "pupil_mask.png"), seg_res["pupil_mask"] * 255)

            # Segmentation overlay on eye image
            overlay = candidate.eye_crop.copy()
            iris_c = (seg_res["iris_mask"] > 0)
            pupil_c = (seg_res["pupil_mask"] > 0)
            overlay[iris_c, 1] = np.clip(overlay[iris_c, 1].astype(int) + 100, 0, 255).astype(np.uint8)
            overlay[pupil_c, 0] = np.clip(overlay[pupil_c, 0].astype(int) + 100, 0, 255).astype(np.uint8)

            cv2.imwrite(str(out_path / "segmentation_overlay.png"), cv2.cvtColor(overlay, cv2.COLOR_RGB2BGR))

        report = {
            "quality": {
                "sharpness": candidate.quality_metrics.sharpness,
                "brightness": candidate.quality_metrics.brightness_mean,
                "score": candidate.quality_metrics.quality_score,
            },
            "valid": candidate.quality_metrics.valid,
        }
        with open(out_path / "quality_report.json", "w", encoding="utf-8") as f:
            json.dump(report, f, indent=2)

    def enroll(
        self, subject_id: str, eye_side: str = "right", source: Optional[str] = None
    ) -> Dict[str, Any]:
        """Runs the enrollment workflow: capture -> best frame -> segment -> normalize -> save template."""
        best_cand = self.capture_best_eye(source=source)
        template, diag = self.process_eye_image(
            best_cand.eye_crop, subject_id=subject_id, eye_side=eye_side
        )

        saved_path = self.store.save_template(template)
        logger.info(f"Successfully enrolled template for '{subject_id}' ({eye_side}) at {saved_path}")

        if self.config.get("privacy", {}).get("save_debug_images", False):
            self.save_debug_artifacts(best_cand, diag["normalization_result"])

        return {
            "status": "ENROLLED",
            "subject_id": subject_id,
            "eye_side": eye_side,
            "saved_path": str(saved_path),
            "quality_score": best_cand.quality_metrics.quality_score,
            "timing_ms": diag["timing_ms"],
            "best_eye_crop": best_cand.eye_crop,
            "diagnostics": diag,
        }

    def verify(
        self, subject_id: str, eye_side: str = "right", source: Optional[str] = None
    ) -> Dict[str, Any]:
        """Runs 1:1 verification against an enrolled subject template."""
        enrolled_template = self.store.load_template(subject_id, eye_side=eye_side)
        if enrolled_template is None:
            raise FileNotFoundError(
                f"No enrolled template found for subject '{subject_id}' ({eye_side} eye)."
            )

        best_cand = self.capture_best_eye(source=source)
        probe_template, diag = self.process_eye_image(
            best_cand.eye_crop, subject_id=subject_id, eye_side=eye_side
        )

        match_res: MatchResult = self.matcher.verify(probe_template, enrolled_template)

        logger.info(
            f"Verification for '{subject_id}': similarity={match_res.similarity:.4f}, "
            f"threshold={match_res.threshold:.4f}, decision={'MATCH' if match_res.match else 'NON-MATCH'}"
        )

        return {
            "subject_id": subject_id,
            "eye_side": eye_side,
            "similarity": match_res.similarity,
            "threshold": match_res.threshold,
            "match": match_res.match,
            "decision": "MATCH" if match_res.match else "NON-MATCH",
            "quality_score": best_cand.quality_metrics.quality_score,
            "timing_ms": diag["timing_ms"],
        }
