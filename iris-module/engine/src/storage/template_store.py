"""
Privacy-Aware Iris Template Storage System.
Persists enrolled iris templates and metadata under structured, secure local paths.
"""

import json
import shutil
from pathlib import Path
from typing import List, Optional
import numpy as np

from src.recognition.template import IrisTemplate


class TemplateStore:
    """Manages enrolment storage of iris templates."""

    def __init__(self, base_dir: str = "templates"):
        self.base_dir = Path(base_dir)
        self.base_dir.mkdir(parents=True, exist_ok=True)

    def _get_subject_dir(self, subject_id: str) -> Path:
        subj_dir = self.base_dir / subject_id
        subj_dir.mkdir(parents=True, exist_ok=True)
        return subj_dir

    def save_template(self, template: IrisTemplate) -> Path:
        """Saves an enrolled IrisTemplate to local storage.

        Args:
            template: IrisTemplate object.

        Returns:
            Path to saved numpy embedding file.
        """
        subj_dir = self._get_subject_dir(template.subject_id)
        prefix = template.eye_side.lower()

        npy_path = subj_dir / f"{prefix}_template.npy"
        json_path = subj_dir / f"{prefix}_metadata.json"

        np.save(str(npy_path), template.embedding)

        meta = template.to_dict()
        with open(json_path, "w", encoding="utf-8") as f:
            json.dump(meta, f, indent=2)

        return npy_path

    def load_template(
        self, subject_id: str, eye_side: str = "right"
    ) -> Optional[IrisTemplate]:
        """Loads an enrolled IrisTemplate for a subject and eye.

        Args:
            subject_id: Unique subject identifier.
            eye_side: 'left' or 'right'.

        Returns:
            IrisTemplate object or None if not found.
        """
        subj_dir = self.base_dir / subject_id
        prefix = eye_side.lower()

        npy_path = subj_dir / f"{prefix}_template.npy"
        json_path = subj_dir / f"{prefix}_metadata.json"

        if not npy_path.exists() or not json_path.exists():
            return None

        embedding = np.load(str(npy_path))
        with open(json_path, "r", encoding="utf-8") as f:
            meta = json.load(f)

        return IrisTemplate.from_dict(meta, embedding)

    def list_subjects(self) -> List[str]:
        """Lists all unique enrolled subject IDs."""
        if not self.base_dir.exists():
            return []
        subjects = [
            subj_dir.name
            for subj_dir in self.base_dir.iterdir()
            if subj_dir.is_dir()
        ]
        return sorted(subjects)

    def list_templates(self) -> List[IrisTemplate]:
        """Lists all enrolled templates across all subjects in store."""
        templates = []
        for subj_dir in self.base_dir.iterdir():
            if subj_dir.is_dir():
                for json_path in subj_dir.glob("*_metadata.json"):
                    prefix = json_path.name.replace("_metadata.json", "")
                    npy_path = subj_dir / f"{prefix}_template.npy"
                    if npy_path.exists():
                        embedding = np.load(str(npy_path))
                        with open(json_path, "r", encoding="utf-8") as f:
                            meta = json.load(f)
                        templates.append(IrisTemplate.from_dict(meta, embedding))
        return templates

    def delete_subject(self, subject_id: str) -> bool:
        """Deletes all enrolled templates and metadata for a subject."""
        subj_dir = self.base_dir / subject_id
        if subj_dir.exists() and subj_dir.is_dir():
            shutil.rmtree(subj_dir)
            return True
        return False

    def delete_template(self, subject_id: str, eye_side: str = "right") -> bool:
        """Deletes a specific eye template for a subject. Removes subject folder if no templates remain."""
        subj_dir = self.base_dir / subject_id
        prefix = eye_side.lower()
        npy_path = subj_dir / f"{prefix}_template.npy"
        json_path = subj_dir / f"{prefix}_metadata.json"

        deleted = False
        if npy_path.exists():
            npy_path.unlink()
            deleted = True
        if json_path.exists():
            json_path.unlink()
            deleted = True

        if subj_dir.exists() and not list(subj_dir.glob("*.npy")):
            shutil.rmtree(subj_dir)

        return deleted
