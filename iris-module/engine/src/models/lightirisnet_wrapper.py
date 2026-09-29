"""
LightIrisNet segmentation model wrapper.
Provides high-level interface for loading MobileNetV3 LightIrisNet weights
and performing iris + pupil segmentation on close-up eye images.
"""

import sys
from pathlib import Path
import numpy as np
import torch
import torch.nn.functional as F
import cv2

# Add LightIrisNet source to path if needed
LIGHTIRISNET_DIR = Path(__file__).resolve().parents[2] / "LightIrisNet" / "src"
if str(LIGHTIRISNET_DIR) not in sys.path and LIGHTIRISNET_DIR.exists():
    sys.path.append(str(LIGHTIRISNET_DIR))

from irisseg._train_impl import IrisNetDeepLab  # type: ignore
from irisseg._test_impl import largest_cc, refit_ellipse, apply_containment  # type: ignore


class LightIrisNetSegmenter:
    """Wrapper for LightIrisNet iris & pupil segmentation model."""

    def __init__(
        self,
        checkpoint_path: str = None,
        device: str = None,
        thr_iris: float = 0.5,
        thr_pupil: float = 0.5,
        containment_mode: str = "soft",
        inside_thresh: float = 0.85,
        keep_area_frac: float = 0.90,
    ):
        if device is None:
            self.device = torch.device("cuda" if torch.cuda.is_available() else "cpu")
        else:
            self.device = torch.device(device)

        if checkpoint_path is None:
            default_ckpt = (
                Path(__file__).resolve().parents[2]
                / "LightIrisNet"
                / "model"
                / "best_mobilenetv3.pt"
            )
            checkpoint_path = str(default_ckpt)

        self.checkpoint_path = checkpoint_path
        self.thr_iris = thr_iris
        self.thr_pupil = thr_pupil
        self.containment_mode = containment_mode
        self.inside_thresh = inside_thresh
        self.keep_area_frac = keep_area_frac

        self.model = IrisNetDeepLab(
            backbone="mobilenetv3",
            use_ellipse=True,
            extra_decoder_conv=True,
            pupil_refine_depth=1,
            pretrained_encoder=False,
        )

        self._load_weights()
        self.model.to(self.device)
        self.model.eval()

    def _load_weights(self):
        if not Path(self.checkpoint_path).exists():
            raise FileNotFoundError(
                f"LightIrisNet checkpoint not found at: {self.checkpoint_path}"
            )

        ckpt = torch.load(self.checkpoint_path, map_location="cpu")
        state_dict = ckpt.get("model_state", ckpt.get("state_dict", ckpt.get("model", ckpt)))
        self.model.load_state_dict(state_dict, strict=False)

    def preprocess(self, eye_image: np.ndarray):
        """Preprocesses RGB uint8/float32 image to normalized PyTorch tensor.

        Args:
            eye_image: (H, W, 3) image in RGB format (0-255 or 0.0-1.0).

        Returns:
            tensor: (1, 3, H, W) normalized image tensor.
            orig_size: (H, W) tuple.
        """
        if eye_image is None or eye_image.size == 0:
            raise ValueError("Invalid input eye image provided.")

        img = eye_image.astype(np.float32)
        if img.max() > 1.0:
            img /= 255.0

        if img.ndim == 2:
            img = np.stack([img, img, img], axis=-1)
        elif img.shape[2] == 4:
            img = img[:, :, :3]

        H, W = img.shape[:2]

        # Pad height and width to multiples of 32 for ASPP compatibility
        pad_h = (32 - (H % 32)) % 32
        pad_w = (32 - (W % 32)) % 32
        if pad_h > 0 or pad_w > 0:
            img_padded = np.pad(img, ((0, pad_h), (0, pad_w), (0, 0)), mode="edge")
        else:
            img_padded = img

        tensor = torch.from_numpy(img_padded.transpose(2, 0, 1)).unsqueeze(0).float()
        mean = torch.tensor([0.485, 0.456, 0.406]).view(1, 3, 1, 1)
        std = torch.tensor([0.229, 0.224, 0.225]).view(1, 3, 1, 1)
        tensor = (tensor - mean) / std

        return tensor.to(self.device), (H, W)

    @torch.no_grad()
    def segment(self, eye_image: np.ndarray):
        """Segments iris and pupil in an eye ROI image.

        Args:
            eye_image: (H, W, 3) RGB image.

        Returns:
            dict containing:
                - 'iris_mask': binary uint8 mask (H, W)
                - 'pupil_mask': binary uint8 mask (H, W)
                - 'iris_ellipse': fitted iris ellipse ((cx, cy), (ma, mi), angle) or None
                - 'pupil_ellipse': fitted pupil ellipse ((cx, cy), (ma, mi), angle) or None
                - 'diagnostics': dictionary with containment and area stats
        """
        tensor, (H, W) = self.preprocess(eye_image)
        outputs = self.model(tensor)

        iris_prob = torch.sigmoid(outputs["iris"])[0, 0, :H, :W].cpu().numpy()
        pupil_prob = torch.sigmoid(outputs["pupil"])[0, 0, :H, :W].cpu().numpy()

        iris_bin = (iris_prob >= self.thr_iris).astype(np.uint8)
        pupil_bin = (pupil_prob >= self.thr_pupil).astype(np.uint8)

        # Containment check: ensure pupil is inside iris
        pupil_adj, contain_diag = apply_containment(
            pupil_bin,
            iris_bin,
            mode=self.containment_mode,
            inside_thresh=self.inside_thresh,
            keep_area_frac=self.keep_area_frac,
        )

        iris_post = largest_cc(iris_bin)
        pupil_post = largest_cc(pupil_adj)

        # Refit ellipses for boundary localization & normalization geometry
        iris_ellipse, iris_refit_mask = refit_ellipse(iris_post)
        pupil_ellipse, pupil_refit_mask = refit_ellipse(pupil_post)

        if iris_refit_mask is not None:
            iris_final = iris_refit_mask
        else:
            iris_final = iris_post

        if pupil_refit_mask is not None:
            pupil_final = pupil_refit_mask
        else:
            pupil_final = pupil_post

        diagnostics = {
            "containment": contain_diag,
            "iris_area": int(iris_final.sum()),
            "pupil_area": int(pupil_final.sum()),
            "iris_prob_mean": float(iris_prob[iris_final > 0].mean()) if iris_final.sum() > 0 else 0.0,
            "pupil_prob_mean": float(pupil_prob[pupil_final > 0].mean()) if pupil_final.sum() > 0 else 0.0,
        }

        return {
            "iris_mask": iris_final.astype(np.uint8),
            "pupil_mask": pupil_final.astype(np.uint8),
            "iris_ellipse": iris_ellipse,
            "pupil_ellipse": pupil_ellipse,
            "diagnostics": diagnostics,
        }


def pupil_refinement_fallback(refit_mask, post_mask):
    return refit_mask if refit_mask is not None else post_mask
