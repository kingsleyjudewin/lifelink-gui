"""
Iris Template Matcher Module.
Performs 1:1 verification and 1:N identification against enrolled iris templates.
"""

from dataclasses import dataclass
from typing import List, Optional
from src.recognition.template import IrisTemplate
from src.models.irisformer_wrapper import VISIrisFormerRecognizer


@dataclass
class MatchResult:
    match: bool
    similarity: float
    threshold: float
    subject_id: str
    eye_side: str


class IrisMatcher:
    """Matcher for 1:1 verification and 1:N identification."""

    def __init__(
        self,
        recognizer: Optional[VISIrisFormerRecognizer] = None,
        default_threshold: float = 0.90,
    ):
        self.recognizer = recognizer
        self.default_threshold = default_threshold

    def _compare(
        self, template_a: IrisTemplate, template_b: IrisTemplate
    ) -> float:
        """Computes similarity score between two IrisTemplate objects."""
        if self.recognizer is not None:
            return self.recognizer.compare_templates(
                template_a.embedding, template_b.embedding
            )

        # Fallback cosine similarity if recognizer is not supplied directly
        import torch
        import torch.nn.functional as F

        t_a = torch.from_numpy(template_a.embedding).float()
        t_b = torch.from_numpy(template_b.embedding).float()

        if t_a.ndim == 2:
            t_a = t_a.unsqueeze(0)
        if t_b.ndim == 2:
            t_b = t_b.unsqueeze(0)

        cos_sim = F.cosine_similarity(t_a, t_b, dim=-1)
        return float(torch.mean(cos_sim, dim=-1).item())

    def verify(
        self,
        probe: IrisTemplate,
        enrolled: IrisTemplate,
        threshold: Optional[float] = None,
    ) -> MatchResult:
        """Performs 1:1 verification between probe and enrolled template.

        Args:
            probe: Probe IrisTemplate.
            enrolled: Enrolled IrisTemplate.
            threshold: Custom threshold override.

        Returns:
            MatchResult object.
        """
        thr = threshold if threshold is not None else self.default_threshold
        similarity = self._compare(probe, enrolled)
        is_match = bool(similarity >= thr)

        return MatchResult(
            match=is_match,
            similarity=similarity,
            threshold=thr,
            subject_id=enrolled.subject_id,
            eye_side=enrolled.eye_side,
        )

    def identify(
        self,
        probe: IrisTemplate,
        gallery: List[IrisTemplate],
        threshold: Optional[float] = None,
    ) -> List[MatchResult]:
        """Performs 1:N identification against a gallery of enrolled templates.

        Args:
            probe: Probe IrisTemplate.
            gallery: List of enrolled IrisTemplates.
            threshold: Custom threshold override.

        Returns:
            List of MatchResult sorted by similarity descending.
        """
        thr = threshold if threshold is not None else self.default_threshold
        results = [self.verify(probe, enrolled, threshold=thr) for enrolled in gallery]
        results.sort(key=lambda r: r.similarity, reverse=True)
        return results
