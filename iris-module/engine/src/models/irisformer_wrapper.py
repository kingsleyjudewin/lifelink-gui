"""
VIS-IrisFormer model wrapper.
Provides high-level interface for loading the Vision Transformer (MAE + RoPE2D)
iris recognition encoder, extracting patch-wise iris embeddings, and comparing templates.
"""

import sys
from pathlib import Path
from typing import Union, Tuple
import numpy as np
import torch
import torch.nn.functional as F

# Add Vis-IrisFormer to path if needed
VIS_IRISFORMER_DIR = Path(__file__).resolve().parents[2] / "Vis-IrisFormer"
if str(VIS_IRISFORMER_DIR) not in sys.path and VIS_IRISFORMER_DIR.exists():
    sys.path.append(str(VIS_IRISFORMER_DIR))

from model.Transformers.VIT.mae import MAEVisionTransformers as MAE  # type: ignore


class VISIrisFormerRecognizer:
    """Wrapper for VIS-IrisFormer Transformer iris feature extractor and matcher."""

    def __init__(
        self,
        checkpoint_path: str = None,
        device: str = None,
        ft_pool: str = "map",
        pos_embed: str = "rope2d",
    ):
        if device is None:
            self.device = torch.device("cuda" if torch.cuda.is_available() else "cpu")
        else:
            self.device = torch.device(device)

        if checkpoint_path is None:
            default_ckpt = (
                Path(__file__).resolve().parents[2]
                / "models"
                / "vis_irisformer"
                / "sota.pth"
            )
            checkpoint_path = str(default_ckpt)

        self.checkpoint_path = checkpoint_path
        self.ft_pool = ft_pool
        self.pos_embed = pos_embed

        self.model = MAE(
            img_size=(64, 512),
            patch_size=(16, 16),
            encoder_dim=768,
            encoder_depth=12,
            encoder_heads=12,
            decoder_dim=512,
            decoder_depth=8,
            decoder_heads=16,
            mask_ratio=0.75,
            num_classes=1,
            ft_pool=self.ft_pool,
            pos_embed=self.pos_embed,
            bottleneck=False,
            bottleneck_dim=512,
        )

        self._load_weights()
        self.model.to(self.device)
        self.model.eval()

    def _load_weights(self):
        if not Path(self.checkpoint_path).exists():
            raise FileNotFoundError(
                f"VIS-IrisFormer checkpoint not found at: {self.checkpoint_path}"
            )

        ckpt = torch.load(self.checkpoint_path, map_location="cpu", weights_only=False)
        if isinstance(ckpt, dict) and "mae_model" in ckpt:
            ckpt_dict = ckpt["mae_model"]
        elif isinstance(ckpt, dict) and "model" in ckpt:
            ckpt_dict = ckpt["model"]
        else:
            ckpt_dict = ckpt

        ckpt_dict_filtered = {
            k: v for k, v in ckpt_dict.items() if "ft" not in k
        }
        msg = self.model.load_state_dict(ckpt_dict_filtered, strict=False)

    def preprocess(self, normalized_iris: np.ndarray) -> torch.Tensor:
        """Preprocesses normalized 64x512 iris image to model input tensor.

        Args:
            normalized_iris: (64, 512) grayscale image (uint8 or float32).

        Returns:
            tensor: (1, 1, 64, 512) float tensor on target device.
        """
        if normalized_iris is None or normalized_iris.size == 0:
            raise ValueError("Invalid normalized iris image provided.")

        img = normalized_iris.astype(np.float32)
        if img.max() > 1.0:
            img /= 255.0

        if img.ndim == 2:
            img = np.stack([img, img, img], axis=-1)

        tensor = torch.from_numpy(img.transpose(2, 0, 1)).unsqueeze(0).float()
        return tensor.to(self.device)

    @torch.no_grad()
    def extract_template(self, normalized_iris: np.ndarray) -> np.ndarray:
        """Extracts patch-wise embedding representation from normalized iris.

        Args:
            normalized_iris: (64, 512) grayscale numpy array.

        Returns:
            embedding: numpy array of shape (N_patches, D) or pooled vector.
        """
        tensor = self.preprocess(normalized_iris)
        norm_embedding, _, _, _, _ = self.model.Encoder.autoencoder(
            tensor, train=False, mask_index=None
        )

        if self.ft_pool == "mean":
            feature = norm_embedding.mean(dim=1)
        elif self.ft_pool == "cls":
            feature = norm_embedding[:, 0]
        else:  # 'map'
            feature = norm_embedding[0]

        return feature.cpu().numpy()

    def compare_templates(
        self,
        template_a: Union[np.ndarray, torch.Tensor],
        template_b: Union[np.ndarray, torch.Tensor],
    ) -> float:
        """Computes patch-wise cosine similarity between two iris templates.

        Args:
            template_a: (N_patches, D) or (1, N_patches, D) embedding.
            template_b: (N_patches, D) or (1, N_patches, D) embedding.

        Returns:
            similarity: scalar cosine similarity score in [-1.0, 1.0].
        """
        if isinstance(template_a, np.ndarray):
            t_a = torch.from_numpy(template_a).float()
        else:
            t_a = template_a.float()

        if isinstance(template_b, np.ndarray):
            t_b = torch.from_numpy(template_b).float()
        else:
            t_b = template_b.float()

        if t_a.ndim == 2:
            t_a = t_a.unsqueeze(0)
        if t_b.ndim == 2:
            t_b = t_b.unsqueeze(0)

        # Patch-wise cosine similarity across feature dimension (dim=-1)
        cos_sim = F.cosine_similarity(t_a, t_b, dim=-1)
        score = torch.mean(cos_sim, dim=-1).item()
        return float(score)
