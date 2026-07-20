from __future__ import annotations

import json
import os
import threading
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime, timezone
from pathlib import Path
from typing import Any
from zoneinfo import ZoneInfo


_BACKEND_DIR = Path(__file__).resolve().parents[2]
_DEFAULT_LOG_PATH = _BACKEND_DIR / "logs" / "chat_eval.jsonl"
_IST = ZoneInfo("Asia/Kolkata")
_WRITE_LOCK = threading.Lock()
_EXECUTOR = ThreadPoolExecutor(
    max_workers=max(1, int(os.getenv("CAPACITY_CHAT_EVAL_WORKERS", "1") or "1"))
)


def log_chat_eval_async(
    *,
    chat_id: str,
    query: str,
    response: str,
    system_prompt: str,
    retrieval_context: list[str],
    metadata: dict[str, Any] | None = None,
) -> None:
    """Evaluate and persist one chat turn without blocking the chat API response.

    DeepEval performs LLM-as-judge calls, so it can be slow or unavailable depending on
    API-key setup. Running it in a background worker keeps the product path reliable.
    """
    if _env_flag("CAPACITY_CHAT_EVAL_ENABLED", default=True) is False:
        return

    _EXECUTOR.submit(
        _evaluate_and_log,
        chat_id=chat_id,
        query=query,
        response=response,
        system_prompt=system_prompt,
        retrieval_context=retrieval_context,
        metadata=metadata or {},
    )


def _evaluate_and_log(
    *,
    chat_id: str,
    query: str,
    response: str,
    system_prompt: str,
    retrieval_context: list[str],
    metadata: dict[str, Any],
) -> None:
    """Write the chat record immediately, then append a second record with eval results."""
    record = {
        "chat_id": chat_id,
        "event": "chat_logged",
        "created_at": datetime.now(timezone.utc).isoformat(),
        "query": query,
        "system_prompt": system_prompt,
        "retrieval_context": retrieval_context,
        "response": response,
        "metadata": metadata,
        "evaluation": {"status": "queued"},
    }
    _append_jsonl(_log_path(), record)

    try:
        evaluation = _run_deepeval_metrics(
            query=query,
            response=response,
            retrieval_context=retrieval_context,
        )
    except Exception as exc:
        evaluation = {
            "status": "failed",
            "error": str(exc),
            "metrics": {},
        }

    evaluation_record = {
        "chat_id": chat_id,
        "event": "evaluation_completed",
        "created_at": datetime.now(timezone.utc).isoformat(),
        "evaluation": evaluation,
    }
    _append_jsonl(_log_path(), evaluation_record)


def _run_deepeval_metrics(
    *,
    query: str,
    response: str,
    retrieval_context: list[str],
) -> dict[str, Any]:
    """Run the three DeepEval checks and return serializable score details."""
    if not _env_flag("CAPACITY_CHAT_EVAL_DEEPEVAL_ENABLED", default=False):
        return {
            "status": "skipped",
            "error": "DeepEval LLM-as-judge is disabled. Set CAPACITY_CHAT_EVAL_DEEPEVAL_ENABLED=true.",
            "metrics": {},
        }

    try:
        from deepeval.models import AzureOpenAIModel, GPTModel
        from deepeval.metrics import (
            AnswerRelevancyMetric,
            ContextualRelevancyMetric,
            FaithfulnessMetric,
        )
        from deepeval.test_case import LLMTestCase
    except Exception as exc:
        return {
            "status": "skipped",
            "error": f"DeepEval is not available: {exc}",
            "metrics": {},
        }

    test_case = LLMTestCase(
        input=query,
        actual_output=response,
        retrieval_context=retrieval_context,
    )
    threshold = float(_get_env("CAPACITY_CHAT_EVAL_THRESHOLD", "0.7") or "0.7")
    try:
        model, model_label = _build_deepeval_judge_model(AzureOpenAIModel=AzureOpenAIModel, GPTModel=GPTModel)
    except Exception as exc:
        return {
            "status": "skipped",
            "error": f"DeepEval judge model is not configured: {exc}",
            "metrics": {},
        }
    metric_kwargs = {"threshold": threshold, "include_reason": True}
    if model:
        metric_kwargs["model"] = model

    try:
        metrics = {
            "faithfulness": FaithfulnessMetric(**metric_kwargs),
            "answer_relevance": AnswerRelevancyMetric(**metric_kwargs),
            "context_relevance": ContextualRelevancyMetric(**metric_kwargs),
        }
    except Exception as exc:
        return {
            "status": "skipped",
            "error": f"DeepEval metric initialization failed: {exc}",
            "metrics": {},
        }

    results: dict[str, Any] = {}
    for name, metric in metrics.items():
        try:
            metric.measure(test_case)
            results[name] = {
                "score": _clean_score(getattr(metric, "score", None)),
                "passed": bool(getattr(metric, "success", False)),
                "reason": getattr(metric, "reason", "") or "",
            }
        except Exception as exc:
            results[name] = {
                "score": None,
                "passed": False,
                "reason": "",
                "error": str(exc),
            }

    return {"status": "complete", "threshold": threshold, "model": model_label, "metrics": results}


def _build_deepeval_judge_model(*, AzureOpenAIModel: Any, GPTModel: Any) -> tuple[Any, str]:
    """Build the LLM judge from existing Azure env vars used by this app."""
    explicit_model = _get_env("CAPACITY_CHAT_EVAL_MODEL") or _get_env("DEEPEVAL_MODEL")
    endpoint = _get_env("AZURE_OPENAI_ENDPOINT") or _get_env("AZURE_ENDPOINT")
    api_key = (
        _get_env("AZURE_OPENAI_API_KEY")
        or _get_env("API_KEY")
        or _get_env("AZURE_API_KEY")
        or _get_env("AZURE_INFERENCE_KEY")
    )
    deployment = _get_env("AZURE_DEPLOYMENT_NAME") or _get_env("AZURE_DEPLOYMENT")
    model_name = explicit_model or _get_env("AZURE_MODEL_NAME") or _get_env("AZURE_MODEL") or _get_env("AZURE_OPENAI_MODEL") or deployment
    api_version = _get_env("OPENAI_API_VERSION") or _get_env("AZURE_OPENAI_API_VERSION")

    if not endpoint or not api_key or not model_name:
        missing = [
            name
            for name, value in {
                "AZURE_ENDPOINT/AZURE_OPENAI_ENDPOINT": endpoint,
                "API_KEY/AZURE_OPENAI_API_KEY": api_key,
                "AZURE_DEPLOYMENT/AZURE_MODEL_NAME": model_name,
            }.items()
            if not value
        ]
        raise ValueError(f"missing {', '.join(missing)}")

    if "/openai/v1" in endpoint.rstrip("/").casefold():
        return (
            GPTModel(model=model_name, api_key=api_key, base_url=endpoint, **_judge_http_client_kwargs()),
            f"{model_name} via Azure OpenAI-compatible endpoint",
        )

    if not deployment:
        raise ValueError("missing AZURE_DEPLOYMENT/AZURE_DEPLOYMENT_NAME for Azure OpenAI endpoint")
    if not api_version:
        raise ValueError("missing OPENAI_API_VERSION/AZURE_OPENAI_API_VERSION for Azure OpenAI endpoint")

    return (
        AzureOpenAIModel(
            model=model_name,
            api_key=api_key,
            base_url=endpoint,
            deployment_name=deployment,
            api_version=api_version,
            **_judge_http_client_kwargs(),
        ),
        f"{model_name} via Azure OpenAI deployment {deployment}",
    )


def _judge_http_client_kwargs() -> dict[str, Any]:
    verify_ssl = _env_flag("CAPACITY_CHAT_EVAL_SSL_VERIFY", default=False)
    try:
        import httpx
    except Exception:
        return {}
    return {
        "http_client": httpx.Client(verify=verify_ssl),
        "async_http_client": httpx.AsyncClient(verify=verify_ssl),
    }

def _append_jsonl(path: Path, record: dict[str, Any]) -> None:
    """Append one JSON object as a single JSONL row, creating the log directory if needed."""
    path.parent.mkdir(parents=True, exist_ok=True)
    line = json.dumps(record, ensure_ascii=False, default=str)
    with _WRITE_LOCK:
        with path.open("a", encoding="utf-8") as handle:
            handle.write(line + "\n")


def _log_path() -> Path:
    """Resolve the active chat eval log path from env or the backend default."""
    configured = _get_env("CAPACITY_CHAT_EVAL_LOG_PATH", "").strip()
    return Path(configured) if configured else _DEFAULT_LOG_PATH


def read_chat_eval_logs(limit: int = 50) -> list[dict[str, Any]]:
    """Return compact, newest-first records for the eval/log viewer.

    The raw JSONL can contain very large prompts/contexts, so this helper strips
    heavy fields and keeps only the parts needed to interpret quality.
    """
    path = _log_path()
    if not path.exists():
        return []

    raw_rows: list[tuple[int, dict[str, Any]]] = []
    with path.open("r", encoding="utf-8") as handle:
        for line_number, line in enumerate(handle, start=1):
            try:
                raw_rows.append((line_number, json.loads(line)))
            except json.JSONDecodeError as exc:
                raw_rows.append((line_number, {
                    "line_number": line_number,
                    "event": "invalid_json",
                    "error": str(exc),
                }))

    rows = _merge_log_records(raw_rows)
    return rows[-max(1, min(limit, 500)):][::-1]


def _merge_log_records(raw_rows: list[tuple[int, dict[str, Any]]]) -> list[dict[str, Any]]:
    merged: dict[str, dict[str, Any]] = {}
    order: list[str] = []
    for line_number, record in raw_rows:
        chat_id = record.get("chat_id") or f"line-{line_number}"
        if chat_id not in merged:
            merged[chat_id] = {}
            order.append(chat_id)
        if record.get("event") == "evaluation_completed":
            merged[chat_id]["evaluation"] = record.get("evaluation") or {}
            merged[chat_id]["evaluation_created_at"] = record.get("created_at", "")
            merged[chat_id]["evaluation_line_number"] = line_number
        else:
            merged[chat_id].update(record)
            merged[chat_id]["line_number"] = line_number
    return [_summarize_log_record(merged[chat_id], int(merged[chat_id].get("line_number") or 0)) for chat_id in order]


def chat_eval_log_stats() -> dict[str, Any]:
    """Return lightweight counts and file metadata for the eval/log page header."""
    path = _log_path()
    rows = read_chat_eval_logs(limit=500)
    return {
        "path": str(path),
        "exists": path.exists(),
        "size_bytes": path.stat().st_size if path.exists() else 0,
        "records": len(rows),
        "chat_logged": sum(1 for row in rows if row.get("event") == "chat_logged"),
        "evaluation_completed": sum(1 for row in rows if row.get("evaluation_status") == "complete"),
        "queued": sum(1 for row in rows if row.get("evaluation_status") == "queued"),
    }


def _summarize_log_record(record: dict[str, Any], line_number: int) -> dict[str, Any]:
    """Convert one raw JSONL row into the shape consumed by the frontend viewer."""
    evaluation = record.get("evaluation") or {}
    metrics = evaluation.get("metrics") or {}

    return {
        "line_number": line_number,
        "chat_id": record.get("chat_id", ""),
        "event": record.get("event", ""),
        "created_at": record.get("created_at", ""),
        "created_at_display": _format_ist(record.get("created_at", "")),
        "query": record.get("query", ""),
        "system_prompt": record.get("system_prompt", ""),
        "retrieval_context": record.get("retrieval_context") or [],
        "response": record.get("response", ""),
        "metadata": record.get("metadata") or {},
        "system_prompt_chars": len(record.get("system_prompt") or ""),
        "retrieval_context_chars": sum(len(item) for item in record.get("retrieval_context") or []),
        "evaluation_status": evaluation.get("status", ""),
        "evaluation_error": evaluation.get("error", ""),
        "faithfulness": _metric_summary(metrics.get("faithfulness")),
        "answer_relevance": _metric_summary(metrics.get("answer_relevance")),
        "context_relevance": _metric_summary(metrics.get("context_relevance")),
    }


def _metric_summary(metric: Any) -> dict[str, Any]:
    """Normalize one DeepEval metric result, including a shortened reason string."""
    if not isinstance(metric, dict):
        return {"score": None, "passed": None, "reason": "", "error": ""}
    return {
        "score": metric.get("score"),
        "passed": metric.get("passed"),
        "reason": _preview(metric.get("reason", ""), 500),
        "error": metric.get("error", ""),
    }


def _format_ist(value: Any) -> str:
    try:
        text = str(value)
        if not text:
            return ""
        parsed = datetime.fromisoformat(text.replace("Z", "+00:00"))
        if parsed.tzinfo is None:
            parsed = parsed.replace(tzinfo=timezone.utc)
        return parsed.astimezone(_IST).strftime("%Y-%m-%d %H:%M")
    except Exception:
        return str(value or "")


def _preview(value: Any, max_chars: int) -> str:
    """Return a string preview capped to the requested character count."""
    text = "" if value is None else str(value)
    return text if len(text) <= max_chars else text[:max_chars] + "..."


def _clean_score(value: Any) -> float | None:
    """Convert a metric score to a rounded float, or None when unavailable."""
    try:
        return round(float(value), 4)
    except (TypeError, ValueError):
        return None


def _env_flag(name: str, *, default: bool) -> bool:
    """Parse an environment variable as a boolean feature flag."""
    value = _get_env(name)
    if not value:
        return default
    return value.strip().casefold() not in {"0", "false", "no", "off"}


def _get_env(name: str, default: str = "") -> str:
    value = os.environ.get(name)
    if value:
        return value

    env_values = _read_local_env()
    return env_values.get(name, default)


def _read_local_env() -> dict[str, str]:
    candidates = [
        Path.cwd() / ".env",
        Path.cwd().parent / ".env",
        Path(__file__).resolve().parents[3] / ".env",
    ]
    values: dict[str, str] = {}

    for path in candidates:
        if not path.exists():
            continue
        for line in path.read_text(encoding="utf-8").splitlines():
            stripped = line.strip()
            if not stripped or stripped.startswith("#") or "=" not in stripped:
                continue
            key, raw_value = stripped.split("=", 1)
            values[key.strip()] = raw_value.strip().strip('"').strip("'")
        if values:
            break

    return values
