from __future__ import annotations

from io import BytesIO
from typing import Any

import pandas as pd
from fastapi import FastAPI, File, UploadFile
from fastapi.middleware.cors import CORSMiddleware
from pydantic import BaseModel

from app.services.capacity import build_capacity_response
from app.services.intelligence import build_capacity_intelligence


app = FastAPI(title="Plant Capacity Utilization API", version="0.1.0")

app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],
    allow_credentials=False,
    allow_methods=["*"],
    allow_headers=["*"],
)


class IntelligenceRequest(BaseModel):
    summary: dict[str, Any] = {}
    daily: list[dict[str, Any]] = []
    details: list[dict[str, Any]] = []
    tower_details: list[dict[str, Any]] = []
    scope_label: str = ""


@app.get("/api/health")
def health_check() -> dict[str, str]:
    return {"status": "ok"}


@app.post("/api/upload")
async def upload_production_report(file: UploadFile = File(...)) -> dict:
    try:
        contents = await file.read()
        workbook = pd.ExcelFile(BytesIO(contents), engine="openpyxl")
    except Exception:
        return {
            "valid": False,
            "summary": None,
            "daily": [],
            "details": [],
            "errors": ["Unable to read workbook. Upload a readable Excel workbook."],
        }

    try:
        return build_capacity_response(workbook)
    except Exception as exc:
        return {
            "valid": False,
            "summary": None,
            "daily": [],
            "details": [],
            "errors": [f"Processing failed: {exc}"],
        }


@app.post("/api/intelligence")
def capacity_intelligence(request: IntelligenceRequest) -> dict[str, Any]:
    try:
        return {
            "valid": True,
            "intelligence": build_capacity_intelligence(request.model_dump()),
            "errors": [],
        }
    except Exception as exc:
        return {
            "valid": False,
            "intelligence": None,
            "errors": [f"Intelligence generation failed: {exc}"],
        }
