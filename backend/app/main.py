from __future__ import annotations

import json
import time
import uuid
from concurrent.futures import ThreadPoolExecutor
from io import BytesIO
from pathlib import Path
from threading import Lock
from typing import Any

import pandas as pd
from fastapi import FastAPI, File, Query, UploadFile
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import FileResponse, JSONResponse
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel

from app.services.capacity import build_capacity_response, build_capacity_response_staged, scope_capacity_result
from app.services.eval_n_log import chat_eval_log_stats, log_chat_eval_async, read_chat_eval_logs
from app.services.intelligence import build_capacity_intelligence, build_chat_response

_FRONTEND_DIST = Path(__file__).resolve().parents[2] / "frontend" / "dist"

app = FastAPI(title="Plant Capacity Utilization API", version="0.1.0")
_UPLOAD_JOBS: dict[str, dict[str, Any]] = {}
# Holds the raw DataFrames/records behind a completed job (folder_day_df, tower_day_df,
# book_details, downtime_reasons) so /api/chat can re-scope by plant/folder/date without
# recomputing from the workbook. Never passed through _safe_json or returned to the client.
_UPLOAD_JOB_FRAMES: dict[str, dict[str, Any]] = {}
_UPLOAD_JOBS_LOCK = Lock()
_UPLOAD_EXECUTOR = ThreadPoolExecutor(max_workers=2)
# Generous TTL: a completed job must survive a full interactive chat session, not just
# the upload's own polling window. Actively used jobs get their TTL refreshed via
# _touch_upload_job on every status check and chat call.
_UPLOAD_JOB_TTL_SECONDS = 4 * 60 * 60

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


class ChatTimeframe(BaseModel):
    start: str = ""
    end: str = ""


class ChatRequest(BaseModel):
    job_id: str
    message: str
    selected_plant: str = ""
    selected_folders: list[str] = []
    timeframe: ChatTimeframe | None = None
    history: list[dict[str, Any]] = []
    force_full_llm: bool = False


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


def _set_upload_job(job_id: str, **updates: Any) -> None:
    with _UPLOAD_JOBS_LOCK:
        current = _UPLOAD_JOBS.setdefault(job_id, {})
        current.update(updates)
        current["updated_at"] = time.time()


def _get_upload_job(job_id: str) -> dict[str, Any] | None:
    with _UPLOAD_JOBS_LOCK:
        job = _UPLOAD_JOBS.get(job_id)
        return dict(job) if job else None


def _touch_upload_job(job_id: str) -> None:
    """Refresh a job's updated_at without altering its status/result, extending its TTL."""
    with _UPLOAD_JOBS_LOCK:
        job = _UPLOAD_JOBS.get(job_id)
        if job is not None:
            job["updated_at"] = time.time()


def _set_upload_job_frames(job_id: str, **frames: Any) -> None:
    with _UPLOAD_JOBS_LOCK:
        _UPLOAD_JOB_FRAMES[job_id] = frames


def _get_upload_job_frames(job_id: str) -> dict[str, Any] | None:
    with _UPLOAD_JOBS_LOCK:
        return _UPLOAD_JOB_FRAMES.get(job_id)


def _cleanup_upload_jobs() -> None:
    cutoff = time.time() - _UPLOAD_JOB_TTL_SECONDS
    with _UPLOAD_JOBS_LOCK:
        stale_ids = [
            job_id for job_id, job in _UPLOAD_JOBS.items()
            if job.get("updated_at", 0) < cutoff
        ]
        for job_id in stale_ids:
            _UPLOAD_JOBS.pop(job_id, None)
            _UPLOAD_JOB_FRAMES.pop(job_id, None)


def _run_staged_upload_job(job_id: str, contents: bytes) -> None:
    def publish(stage_update: dict[str, Any]) -> None:
        result = stage_update.get("result")
        _set_upload_job(
            job_id,
            status="processing",
            stage=stage_update.get("stage", "processing"),
            message=stage_update.get("message", "Processing workbook"),
            progress=stage_update.get("progress", 0),
            result=_safe_json(result) if result else None,
        )

    try:
        _set_upload_job(
            job_id,
            status="processing",
            stage="reading",
            message="Reading uploaded workbook",
            progress=5,
        )
        workbook = pd.ExcelFile(BytesIO(contents), engine="openpyxl")
        staged = build_capacity_response_staged(workbook, on_stage=publish)
        _set_upload_job_frames(
            job_id,
            folder_day_df=staged.folder_day_df,
            tower_day_df=staged.tower_day_df,
            book_details=staged.book_details,
            downtime_reasons=staged.downtime_reasons,
        )
        _set_upload_job(
            job_id,
            status="complete",
            stage="complete",
            message="Dashboard data is ready",
            progress=100,
            result=_safe_json(staged.payload),
        )
    except Exception as exc:
        _set_upload_job(
            job_id,
            status="error",
            stage="error",
            message=f"Processing failed: {exc}",
            progress=100,
            result={
                "valid": False,
                "summary": None,
                "daily": [],
                "details": [],
                "tower_details": [],
                "downtime_reasons": [],
                "errors": [f"Processing failed: {exc}"],
            },
        )


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


@app.post("/api/upload/start")
async def start_upload_production_report(file: UploadFile = File(...)) -> JSONResponse:
    _cleanup_upload_jobs()
    try:
        contents = await file.read()
    except Exception as exc:
        return JSONResponse({
            "valid": False,
            "job_id": "",
            "status": "error",
            "message": f"Unable to read upload: {exc}",
            "errors": [f"Unable to read upload: {exc}"],
        })

    job_id = uuid.uuid4().hex
    _set_upload_job(
        job_id,
        status="queued",
        stage="queued",
        message="Upload received; queued for processing",
        progress=0,
        result=None,
        created_at=time.time(),
    )
    _UPLOAD_EXECUTOR.submit(_run_staged_upload_job, job_id, contents)
    return JSONResponse({
        "valid": True,
        "job_id": job_id,
        "status": "queued",
        "stage": "queued",
        "message": "Upload received; queued for processing",
        "progress": 0,
        "result": None,
    })


@app.get("/api/upload/status/{job_id}")
def upload_job_status(job_id: str) -> JSONResponse:
    job = _get_upload_job(job_id)
    if not job:
        return JSONResponse({
            "valid": False,
            "status": "not_found",
            "stage": "not_found",
            "message": "Upload job was not found or has expired.",
            "progress": 100,
            "result": None,
            "errors": ["Upload job was not found or has expired."],
        }, status_code=404)

    _touch_upload_job(job_id)
    return JSONResponse(_safe_json({
        "valid": job.get("status") != "error",
        "job_id": job_id,
        "status": job.get("status", "processing"),
        "stage": job.get("stage", "processing"),
        "message": job.get("message", "Processing workbook"),
        "progress": job.get("progress", 0),
        "result": job.get("result"),
        "errors": (job.get("result") or {}).get("errors", []),
    }))


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
    _cleanup_upload_jobs()
    job = _get_upload_job(request.job_id)
    frames = _get_upload_job_frames(request.job_id)
    if not job or job.get("status") != "complete" or frames is None:
        return JSONResponse({
            "valid": False,
            "answer": "This dashboard session is no longer available. Please re-upload the file.",
            "status": "not_found",
        }, status_code=404)

    _touch_upload_job(request.job_id)
    try:
        scoped = scope_capacity_result(
            folder_day_df=frames["folder_day_df"],
            tower_day_df=frames["tower_day_df"],
            book_details=frames["book_details"],
            downtime_reasons=frames["downtime_reasons"],
            selected_plant=request.selected_plant,
            selected_folders=request.selected_folders,
            timeframe_start=request.timeframe.start if request.timeframe else "",
            timeframe_end=request.timeframe.end if request.timeframe else "",
        )
        result = build_chat_response(
            message=request.message,
            summary=scoped["summary"],
            daily_rows=scoped["daily"],
            details=scoped["details"],
            tower_details=scoped["tower_details"],
            downtime_reasons=scoped["downtime_reasons"],
            book_details=scoped["book_details"],
            history=request.history,
            force_full_llm=request.force_full_llm,
        )
        context_json = json.dumps(_safe_json(scoped), separators=(",", ":"))
        log_chat_eval_async(
            chat_id=uuid.uuid4().hex,
            query=request.message,
            response=result.get("answer", ""),
            system_prompt="Answer using only the scoped dashboard context supplied in retrieval_context.",
            retrieval_context=[context_json],
            metadata={
                "job_id": request.job_id,
                "selected_plant": request.selected_plant,
                "selected_folders": request.selected_folders,
                "timeframe": request.timeframe.model_dump() if request.timeframe else None,
                "status": result.get("status", "ok"),
            },
        )
        return JSONResponse({
            "valid": True,
            "answer": result.get("answer", ""),
            "status": result.get("status", "ok"),
            "detail": result.get("detail", ""),
            "plan": result.get("plan") or None,
            "confidence": result.get("confidence"),
            "refined": result.get("refined", False),
            "chart": _safe_json(result.get("chart")) if result.get("chart") else None,
        })
    except Exception as exc:
        return JSONResponse({
            "valid": False,
            "answer": "Unable to process your question.",
            "status": "error",
            "error": str(exc),
        })


# Chat evaluation log viewer API.
# The frontend `/eval_n_log` page uses this endpoint to read compact records from
# backend/logs/chat_eval.jsonl without exposing the raw large JSONL file directly.
@app.get("/api/eval_n_log")
def eval_n_log_records(limit: int = Query(default=50, ge=1, le=500)) -> dict[str, Any]:
    return {
        "valid": True,
        "stats": chat_eval_log_stats(),
        "records": read_chat_eval_logs(limit=limit),
    }


# Serve built frontend in production (skipped silently in dev when dist/ doesn't exist)
if _FRONTEND_DIST.exists():
    _assets_dir = _FRONTEND_DIST / "assets"
    if _assets_dir.exists():
        app.mount("/assets", StaticFiles(directory=str(_assets_dir)), name="assets")

    @app.get("/{full_path:path}", include_in_schema=False)
    def serve_spa() -> FileResponse:
        return FileResponse(str(_FRONTEND_DIST / "index.html"))
