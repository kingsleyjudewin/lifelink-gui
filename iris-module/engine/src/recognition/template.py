"""
Iris Biometric Template Representation Data Module.
Encapsulates patch-wise embeddings and metadata for enrolment and verification.
"""

from dataclasses import dataclass, field, asdict
from datetime import datetime
from typing import Tuple, Dict, Any
import numpy as np


@dataclass
class IrisTemplate:
    subject_id: str
    eye_side: str  # 'left' or 'right'
    embedding: np.ndarray
    model_name: str = "VIS-IrisFormer"
    model_version: str = "1.0.0"
    normalization_size: Tuple[int, int] = (64, 512)
    template_type: str = "patch_embedding"
    created_at: str = field(default_factory=lambda: datetime.utcnow().isoformat())

    def to_dict(self) -> Dict[str, Any]:
        """Converts template metadata to dictionary (excluding raw embedding array)."""
        d = asdict(self)
        del d["embedding"]
        return d

    @classmethod
    def from_dict(cls, data: Dict[str, Any], embedding: np.ndarray) -> "IrisTemplate":
        """Instantiates IrisTemplate from metadata dictionary and embedding array."""
        return cls(
            subject_id=data["subject_id"],
            eye_side=data.get("eye_side", "right"),
            embedding=embedding,
            model_name=data.get("model_name", "VIS-IrisFormer"),
            model_version=data.get("model_version", "1.0.0"),
            normalization_size=tuple(data.get("normalization_size", (64, 512))),
            template_type=data.get("template_type", "patch_embedding"),
            created_at=data.get("created_at", datetime.utcnow().isoformat()),
        )
