#!/usr/bin/env python3
"""
LifeLink iris engine — stateless HTTP service around the LightIrisNet +
VIS-IrisFormer pipeline.

Replaces the original module's server_pi5.py. It stores nothing: no
templates, no captured frames. The LifeLink backend (backend/app.py) owns
storage (encrypted in the local Postgres) and calls this service only to
turn an eye image into a template, or to compare a live eye image against
templates it already holds.

  GET  /health   -> {status, model, threshold}
  POST /extract  {image_data}             -> {template_data, quality, segmentation}
  POST /verify   {image_data, templates}  -> {match, similarity, eye_side, threshold, scores}

image_data is a base64 JPEG/PNG eye image. template_data is a base64 .npy
float32 array of patch embeddings. A frame whose iris/pupil segmentation
fails validation is answered with 422 {error: "segmentation_invalid"} so the
camera can take a new frame.

Binds to 127.0.0.1 by default: only the LifeLink backend should reach it.
Run:  .venv/bin/python iris_service.py [--host 127.0.0.1] [--port 8010]
"""

import argparse
import base64
import binascii
import io
import json
import logging
import sys
from contextlib import asynccontextmanager
from pathlib import Path

import cv2
import numpy as np
import uvicorn
from fastapi import FastAPI, HTTPException
from pydantic import BaseModel

ENGINE_DIR = Path(__file__).resolve().parent
if str(ENGINE_DIR) not in sys.path:
    sys.path.insert(0, str(ENGINE_DIR))

from src.pipeline.iris_pipeline import IrisRecognitionPipeline  # noqa: E402
from src.quality.iris_quality import IrisQualityEvaluator  # noqa: E402
from src.vision.segmentation_validator import SegmentationValidator  # noqa: E402

logger = logging.getLogger("IrisService")

MAX_IMAGE_BYTES = 5 * 1024 * 1024
MAX_TEMPLATE_BYTES = 2 * 1024 * 1024
MODEL_NAME = "VIS-IrisFormer"

THRESHOLD = json.loads((ENGINE_DIR / "config" / "matching_threshold.json").read_text())["threshold"]

PIPELINE_CONFIG = {
    "segmentation": {"checkpoint_path": str(ENGINE_DIR / "models" / "best_mobilenetv3.pt")},
    "recognition": {"checkpoint_path": str(ENGINE_DIR / "models" / "sota.pth")},
    "matching": {"threshold": THRESHOLD},
    # The pipeline's TemplateStore is never used here, but it creates its
    # directory on init; keep that out of the engine folder.
    "storage": {"templates_dir": str(ENGINE_DIR / ".unused_template_store")},
}

pipeline = None
validator = SegmentationValidator()
quality_evaluator = IrisQualityEvaluator()


def get_pipeline():
    global pipeline
    if pipeline is None:
        logger.info("Loading LightIrisNet + VIS-IrisFormer...")
        pipeline = IrisRecognitionPipeline(config=PIPELINE_CONFIG)
    return pipeline


class ExtractRequest(BaseModel):
    image_data: str


class StoredTemplate(BaseModel):
    eye_side: str
    template_data: str


class VerifyRequest(BaseModel):
    image_data: str
    templates: list[StoredTemplate]


def decode_image(image_b64):
    try:
        raw = base64.b64decode(image_b64, validate=True)
    except (binascii.Error, ValueError):
        raise HTTPException(400, detail={"error": "invalid_image", "reason": "image_data is not valid base64"})
    if len(raw) > MAX_IMAGE_BYTES:
        raise HTTPException(413, detail={"error": "image_too_large"})
    bgr = cv2.imdecode(np.frombuffer(raw, np.uint8), cv2.IMREAD_COLOR)
    if bgr is None:
        raise HTTPException(400, detail={"error": "invalid_image", "reason": "could not decode image"})
    return cv2.cvtColor(bgr, cv2.COLOR_BGR2RGB)


def encode_template(embedding):
    buf = io.BytesIO()
    np.save(buf, np.ascontiguousarray(embedding, dtype=np.float32), allow_pickle=False)
    return base64.b64encode(buf.getvalue()).decode()


def decode_template(template_b64):
    try:
        raw = base64.b64decode(template_b64, validate=True)
    except (binascii.Error, ValueError):
        raise HTTPException(400, detail={"error": "invalid_template"})
    if len(raw) > MAX_TEMPLATE_BYTES:
        raise HTTPException(400, detail={"error": "invalid_template"})
    try:
        arr = np.load(io.BytesIO(raw), allow_pickle=False)
    except ValueError:
        raise HTTPException(400, detail={"error": "invalid_template"})
    if arr.dtype != np.float32 or arr.ndim != 2:
        raise HTTPException(400, detail={"error": "invalid_template"})
    return arr


def segment_and_validate(pipe, image_rgb):
    """Returns (template, validation) for the first input that segments
    cleanly: the image as sent, then (for a full camera frame) its central
    eye ROI. Raises 422 when neither does."""
    attempts = [image_rgb]
    if image_rgb.shape[0] > 400 and image_rgb.shape[1] > 400:
        attempts.append(pipe.cropper.crop(image_rgb)[0])

    last = None
    for img in attempts:
        template, diag = pipe.process_eye_image(img)
        seg = diag["segmentation_result"]
        last = validator.validate(seg["iris_mask"], seg["pupil_mask"], seg["diagnostics"])
        if last.valid:
            return template, last, img
    raise HTTPException(422, detail={"error": "segmentation_invalid", "reason": last.reason})


@asynccontextmanager
async def lifespan(_app):
    get_pipeline()
    yield


app = FastAPI(title="LifeLink Iris Engine", lifespan=lifespan)


@app.get("/health")
def health():
    return {"status": "ok", "model": MODEL_NAME, "threshold": THRESHOLD}


@app.post("/extract")
def extract(req: ExtractRequest):
    pipe = get_pipeline()
    template, validation, eye_img = segment_and_validate(pipe, decode_image(req.image_data))
    quality = quality_evaluator.evaluate(eye_img)
    return {
        "model_name": MODEL_NAME,
        "template_data": encode_template(template.embedding),
        "quality_score": round(float(quality.quality_score), 1),
        "segmentation": {
            "iris_area_ratio": round(validation.iris_area_ratio, 4),
            "pupil_inside_ratio": round(validation.pupil_inside_ratio, 4),
        },
    }


@app.post("/verify")
def verify(req: VerifyRequest):
    if not req.templates:
        raise HTTPException(400, detail={"error": "no_templates"})
    enrolled = [(t.eye_side, decode_template(t.template_data)) for t in req.templates]

    pipe = get_pipeline()
    probe, _, _ = segment_and_validate(pipe, decode_image(req.image_data))

    scores = []
    for eye_side, embedding in enrolled:
        if embedding.shape != probe.embedding.shape:
            raise HTTPException(400, detail={"error": "template_shape_mismatch"})
        similarity = pipe.recognizer.compare_templates(probe.embedding, embedding)
        scores.append({"eye_side": eye_side, "similarity": round(float(similarity), 4)})
    best = max(scores, key=lambda s: s["similarity"])
    return {
        "match": best["similarity"] >= THRESHOLD,
        "similarity": best["similarity"],
        "eye_side": best["eye_side"],
        "threshold": THRESHOLD,
        "scores": scores,
    }


def main():
    parser = argparse.ArgumentParser(description="LifeLink iris engine service")
    parser.add_argument("--host", default="127.0.0.1")
    parser.add_argument("--port", type=int, default=8010)
    args = parser.parse_args()
    logging.basicConfig(level=logging.INFO, format="%(asctime)s [%(levelname)s] %(name)s: %(message)s")
    uvicorn.run(app, host=args.host, port=args.port)


if __name__ == "__main__":
    main()
