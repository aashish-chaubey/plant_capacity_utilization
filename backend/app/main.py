from __future__ import annotations

import json
from io import BytesIO
from pathlib import Path
from typing import Any

import pandas as pd
from fastapi import FastAPI, File, UploadFile
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import FileResponse, JSONResponse
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel

from app.services.capacity import build_capacity_response
from app.services.intelligence import build_capacity_intelligence, build_chat_response

_FRONTEND_DIST = Path(__file__).resolve().parents[2] / "frontend" / "dist"

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


class ChatRequest(BaseModel):
    message: str
    intelligence: dict[str, Any] = {}
    tower_details: list[dict[str, Any]] = []
    history: list[dict[str, str]] = []


def _json_default(obj: Any) -> Any:
    """Convert numpy/pandas scalar types that json.dumps can't handle natively."""
    # pd.NA, pd.NaT, numpy.nan → null
    try:
        if pd.isna(obj):
            return None
    except (TypeError, ValueError):
        pass
    # numpy scalars (int64, float64, bool_) → Python native via .item()
    if hasattr(obj, "item"):
        return obj.item()
    # numpy arrays, pandas Series → list
    if hasattr(obj, "tolist"):
        return obj.tolist()
    raise TypeError(f"Type {type(obj).__name__} is not JSON serializable")


def _safe_json(data: Any) -> Any:
    """Round-trip through json.dumps/loads to strip all non-serializable types."""
    return json.loads(json.dumps(data, default=_json_default))


@app.get("/api/health")
def health_check() -> dict[str, str]:
    return {"status": "ok"}


@app.post("/api/upload")
async def upload_production_report(file: UploadFile = File(...)) -> JSONResponse:
    try:
        contents = await file.read()
        workbook = pd.ExcelFile(BytesIO(contents), engine="openpyxl")
    except Exception as exc:
        return JSONResponse({
            "valid": False,
            "summary": None,
            "daily": [],
            "details": [],
            "errors": [f"Unable to read workbook: {exc}"],
        })

    try:
        result = build_capacity_response(workbook)
        return JSONResponse(_safe_json(result))
    except Exception as exc:
        return JSONResponse({
            "valid": False,
            "summary": None,
            "daily": [],
            "details": [],
            "errors": [f"Processing failed: {exc}"],
        })


@app.post("/api/intelligence")
def capacity_intelligence(request: IntelligenceRequest) -> JSONResponse:
    try:
        result = build_capacity_intelligence(request.model_dump())
        return JSONResponse(_safe_json({"valid": True, "intelligence": result, "errors": []}))
    except Exception as exc:
        return JSONResponse({
            "valid": False,
            "intelligence": None,
            "errors": [f"Intelligence generation failed: {exc}"],
        })


@app.post("/api/chat")
def capacity_chat(request: ChatRequest) -> JSONResponse:
    try:
        result = build_chat_response(
            message=request.message,
            intelligence=request.intelligence,
            tower_details=request.tower_details,
            history=request.history,
        )
        return JSONResponse({
            "valid": True,
            "answer": result.get("answer", ""),
            "status": result.get("status", "ok"),
        })
    except Exception as exc:
        return JSONResponse({
            "valid": False,
            "answer": "Unable to process your question.",
            "status": "error",
            "error": str(exc),
        })


# Serve built frontend in production (skipped silently in dev when dist/ doesn't exist)
if _FRONTEND_DIST.exists():
    _assets_dir = _FRONTEND_DIST / "assets"
    if _assets_dir.exists():
        app.mount("/assets", StaticFiles(directory=str(_assets_dir)), name="assets")

    @app.get("/{full_path:path}", include_in_schema=False)
    def serve_spa() -> FileResponse:
        return FileResponse(str(_FRONTEND_DIST / "index.html"))
