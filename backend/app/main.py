from __future__ import annotations

from io import BytesIO

import pandas as pd
from fastapi import FastAPI, File, UploadFile
from fastapi.middleware.cors import CORSMiddleware

from app.services.capacity import build_capacity_response, validate_workbook


app = FastAPI(title="Plant Capacity Utilization API", version="0.1.0")

app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],
    allow_credentials=False,
    allow_methods=["*"],
    allow_headers=["*"],
)


@app.get("/api/health")
def health_check() -> dict[str, str]:
    return {"status": "ok"}


@app.post("/api/upload")
async def upload_production_report(file: UploadFile = File(...)) -> dict:
    if not file.filename or not file.filename.lower().endswith(".xlsx"):
        return {
            "valid": False,
            "summary": None,
            "daily": [],
            "details": [],
            "errors": ["Only .xlsx production reports are supported."],
        }

    try:
        contents = await file.read()
        workbook = pd.ExcelFile(BytesIO(contents), engine="openpyxl")
    except Exception:
        return {
            "valid": False,
            "summary": None,
            "daily": [],
            "details": [],
            "errors": ["Unable to read workbook. Upload a valid .xlsx file."],
        }

    errors = validate_workbook(workbook)
    if errors:
        return {
            "valid": False,
            "summary": None,
            "daily": [],
            "details": [],
            "errors": errors,
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
