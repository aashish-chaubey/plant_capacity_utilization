from __future__ import annotations

from datetime import datetime, timezone
from math import isfinite
import json
import os
from pathlib import Path
from statistics import pstdev
from typing import Any
from urllib.error import HTTPError, URLError
from urllib.parse import parse_qsl, quote, urlencode, urlsplit, urlunsplit
from urllib.request import Request, urlopen


CAPACITY_MINUTES_PER_FOLDER_DAY = 240.0
REQUEST_TIMEOUT_SECONDS = 35

LOSS_COMPONENTS = [
    ("change_over_time", "Changeover time"),
    ("late_start_time", "LPR to print start"),
    ("reflong_related_downtime", "Reflong time"),
]

LOSS_DRIVER_INFERENCES = {
    "change_over_time": "changeover and sequencing gaps between editions",
    "late_start_time": "late start at the beginning of the 00:00-04:00 window",
    "reflong_related_downtime": "reflong-related interruption time",
}


def build_chat_response(
    message: str,
    intelligence: dict[str, Any],
    tower_details: list[dict[str, Any]],
    history: list[dict[str, str]],
) -> dict[str, Any]:
    endpoint = _get_env("AZURE_ENDPOINT")
    api_key = (
        _get_env("API_KEY")
        or _get_env("AZURE_API_KEY")
        or _get_env("AZURE_OPENAI_API_KEY")
        or _get_env("AZURE_INFERENCE_KEY")
    )

    if not endpoint or not api_key:
        return {"answer": "LLM is not configured.", "status": "unconfigured"}

    context = _build_chat_context(intelligence, tower_details)
    context_json = json.dumps(context, separators=(",", ":"), ensure_ascii=True)

    system_content = (
        "You are a concise analytics assistant for a print plant production dashboard. "
        "The production window is midnight to 4 AM (00:00–04:00). "
        "Answer ONLY from the JSON context supplied — never guess or invent values. "
        "Be brief and direct — no preamble, no filler. "
        "For ranked results use a short numbered list. "
        "Convert minutes to h:mm format when it aids readability. "
        "If the answer is genuinely absent from the data, say: Not available in the current data.\n\n"
        "Schema key:\n"
        "- resource: 'Machine / Folder' display name\n"
        "- utilization_pct / utilization_percentage: runtime ÷ total possible capacity (incl. unplanned nights)\n"
        "- active_day_utilization_pct: runtime ÷ capacity only on nights the folder was active\n"
        "- runtime_minutes / runtime_min: actual print runtime\n"
        "- lost_time_min / lost_time_minutes: Loss Time = changeover + late-start + reflong ONLY. "
        "WAITING TIME IS NOT INCLUDED IN LOSS TIME — it is a completely separate metric.\n"
        "- waiting_time_min / waiting_time_minutes: time waiting for editions; SEPARATE from loss_time, NEVER add it to loss_time\n"
        "- buffer_time_min / buffer_time_minutes: Spare Time — leftover capacity WITHIN an active night. "
        "This is the ONLY definition of spare time. NEVER add unplanned_time to spare time.\n"
        "- unplanned_time_min (folders): fully unscheduled nights where the folder had NO activity. "
        "This is NOT spare time — do not include it when reporting spare capacity.\n"
        "- loss_components: breakdown of loss_time into change_over_time, late_start_time, reflong_related_downtime\n"
        "- average_speed_cph: copies per hour\n"
        "- complex_runtime_share_percentage: share of runtime that was complex-category print\n"
        "- top_folders_by_loss: folders contributing most loss time on that day\n"
        "- unplanned_capacity_min (towers): capacity from nights the tower was not scheduled at all\n"
        "- spare_time_min (towers): leftover capacity within nights the tower was active\n"
        "- unplanned_nights (towers): number of production nights with no activity for that tower\n"
        "- uv_tower: true if the tower is a UV tower\n"
        "- uv_towers / non_uv_towers: pre-split lists by UV type\n"
        "- downtime_min (folders): mechanical machine stoppage time\n"
        "- load_share_pct: this folder's share of total plant runtime\n"
        "- variability_pct: std deviation of daily runtime % — high = inconsistent usage\n"
        "- unused_folders: list of folders with zero active nights in the period\n"
        "- complexity_vs_loss: per-complexity-category avg loss share and total lost/downtime\n"
        "- speed.by_category: speed and runtime share for each print category (SNP, GNP, SNP Complex, GNP Complex)\n"
        "- dominant_complexity (speed.by_folder): the category that ran most on that folder\n"
        "CRITICAL RULES: (1) spare_time = buffer_time ONLY. unplanned_time is NEVER spare. "
        "(2) waiting_time is NOT a loss time component — always report them separately. "
        "(3) Plant spare time = sum of buffer_time across folders, never include unplanned_time.\n\n"
        f"Dashboard context:\n{context_json}"
    )

    messages: list[dict[str, str]] = [{"role": "system", "content": system_content}]
    for turn in (history or [])[-8:]:
        role = _clean_text(turn.get("role", ""))
        content = _clean_text(turn.get("content", ""))
        if role in ("user", "assistant") and content:
            messages.append({"role": role, "content": content})
    messages.append({"role": "user", "content": _clean_text(message)})

    try:
        answer = _call_plain_chat_completion(endpoint, api_key, messages)
        return {"answer": answer.strip(), "status": "ok"}
    except Exception as exc:
        return {
            "answer": "Unable to generate a response right now.",
            "status": "error",
            "detail": _sanitize_error_message(exc, api_key),
        }


def _build_chat_context(
    intelligence: dict[str, Any],
    tower_details: list[dict[str, Any]],
) -> dict[str, Any]:
    sections = intelligence.get("sections") or {}
    folder_util = sections.get("folder_utilization") or {}
    loss_time_sec = sections.get("loss_time") or {}
    complexity = sections.get("complexity_speed") or {}

    # Aggregate tower metrics across all dates
    production_days = _number((intelligence.get("scope") or {}).get("production_days"))
    tower_buckets: dict[str, dict] = {}
    for row in tower_details:
        tower = _display_resource_name(row.get("tower") or "")
        if not tower:
            continue
        bucket = tower_buckets.setdefault(tower, {
            "runtime": 0.0, "available": 0.0, "buffer": 0.0,
            "downtime": 0.0, "waiting_time": 0.0,
            "change_over_time": 0.0, "late_start_time": 0.0,
            "reflong_related_downtime": 0.0, "dates": set(),
            "uv_tower": False,
        })
        bucket["runtime"] += _number(row.get("runtime"))
        bucket["available"] += _number(row.get("available_capacity"))
        bucket["buffer"] += _number(row.get("buffer_time"))
        bucket["downtime"] += _number(row.get("downtime"))
        bucket["waiting_time"] += _number(row.get("waiting_time"))
        bucket["change_over_time"] += _number(row.get("change_over_time"))
        bucket["late_start_time"] += _number(row.get("late_start_time"))
        bucket["reflong_related_downtime"] += _number(row.get("reflong_related_downtime"))
        if row.get("uv_tower"):
            bucket["uv_tower"] = True
        date = _clean_text(row.get("run_date"))
        if date:
            bucket["dates"].add(date)

    tower_rows = []
    for t, v in tower_buckets.items():
        active_nights = len(v["dates"])
        unplanned_nights = max(int(production_days) - active_nights, 0) if production_days > 0 else 0
        unplanned_capacity_min = unplanned_nights * CAPACITY_MINUTES_PER_FOLDER_DAY
        non_wait_lost_time = v["change_over_time"] + v["late_start_time"] + v["reflong_related_downtime"]
        tower_rows.append({
            "tower": t,
            "uv_tower": v["uv_tower"],
            "runtime_min": _clean_number(v["runtime"]),
            "downtime_min": _clean_number(v["downtime"]),
            "lost_time_min": _clean_number(non_wait_lost_time),
            "waiting_time_min": _clean_number(v["waiting_time"]),
            "change_over_time_min": _clean_number(v["change_over_time"]),
            "late_start_time_min": _clean_number(v["late_start_time"]),
            "reflong_downtime_min": _clean_number(v["reflong_related_downtime"]),
            "spare_time_min": _clean_number(v["buffer"]),
            "unplanned_capacity_min": _clean_number(unplanned_capacity_min),
            "active_nights": active_nights,
            "unplanned_nights": unplanned_nights,
            "utilization_pct": _percentage(v["runtime"], v["available"]),
        })
    tower_rows.sort(key=lambda r: -r["utilization_pct"])

    def _slim_day(row: dict[str, Any]) -> dict[str, Any]:
        return {
            "run_date": row.get("run_date"),
            "runtime_min": row.get("runtime_minutes"),
            "lost_time_min": row.get("lost_time_minutes"),
            "waiting_time_min": row.get("waiting_time_minutes"),
            "available_capacity_min": row.get("available_capacity_minutes"),
            "loss_pct": row.get("loss_percentage"),
            "dominant_driver": (row.get("dominant_driver") or {}).get("label"),
            "loss_components": {
                c.get("key"): c.get("minutes")
                for c in (row.get("components") or [])
                if c and c.get("minutes", 0) > 0
            },
            "top_folders_by_loss": [
                {"folder": f.get("resource"), "lost_min": f.get("lost_time_minutes")}
                for f in (row.get("top_folders") or [])
            ],
        }

    def _slim_folder(row: dict[str, Any]) -> dict[str, Any]:
        return {
            "resource": row.get("resource"),
            "utilization_pct": row.get("utilization_percentage"),
            "active_day_utilization_pct": row.get("active_day_utilization_percentage"),
            "runtime_min": row.get("runtime_minutes"),
            "lost_time_min": row.get("lost_time_minutes"),
            "waiting_time_min": row.get("waiting_time_minutes"),
            "downtime_min": row.get("downtime_minutes"),
            "buffer_time_min": row.get("buffer_time_minutes"),
            "unplanned_time_min": row.get("unplanned_time_minutes"),
            "active_days": row.get("active_days"),
            "unplanned_days": row.get("idle_days"),
            "loss_share_pct": row.get("loss_share_percentage"),
            "load_share_pct": row.get("load_share_percentage"),
            "variability_pct": row.get("runtime_variability_percentage_points"),
            "classification": row.get("classification"),
        }

    def _slim_speed(row: dict[str, Any]) -> dict[str, Any]:
        return {
            "resource": row.get("resource"),
            "avg_speed_cph": row.get("average_speed_cph"),
            "simple_speed_cph": row.get("simple_speed_cph"),
            "complex_speed_cph": row.get("complex_speed_cph"),
            "complex_share_pct": row.get("complex_runtime_share_percentage"),
            "dominant_complexity": row.get("dominant_complexity"),
            "runtime_min": row.get("runtime_minutes"),
        }

    all_days = [_slim_day(r) for r in (loss_time_sec.get("days") or [])]
    all_folders = [_slim_folder(r) for r in (folder_util.get("folders") or [])]
    speed_by_folder = [_slim_speed(r) for r in (complexity.get("by_folder") or [])]
    speed_by_machine = [_slim_speed(r) for r in (complexity.get("by_machine") or [])]

    # Unused folders: active_days == 0 across the entire period
    unused_folders = [f["resource"] for f in all_folders if not f.get("active_days")]

    # Complexity-vs-loss correlation: join speed dominant_complexity with folder loss share
    speed_lookup = {r.get("resource"): r for r in speed_by_folder if r.get("resource")}
    complexity_loss_groups: dict[str, list[dict[str, Any]]] = {}
    for f in all_folders:
        res = f.get("resource")
        speed_row = speed_lookup.get(res, {})
        cat = _clean_text(speed_row.get("dominant_complexity")) or "Unknown"
        if cat == "Unknown":
            continue
        complexity_loss_groups.setdefault(cat, []).append({
            "resource": res,
            "loss_share_pct": _number(f.get("loss_share_pct")),
            "lost_time_min": _number(f.get("lost_time_min")),
            "downtime_min": _number(f.get("downtime_min")),
            "runtime_min": _number(f.get("runtime_min")),
        })

    complexity_loss_summary = []
    for cat, entries in sorted(complexity_loss_groups.items()):
        loss_shares = [e["loss_share_pct"] for e in entries]
        complexity_loss_summary.append({
            "complexity": cat,
            "folder_count": len(entries),
            "avg_loss_share_pct": _clean_number(_average(loss_shares)),
            "total_lost_time_min": _clean_number(sum(e["lost_time_min"] for e in entries)),
            "total_downtime_min": _clean_number(sum(e["downtime_min"] for e in entries)),
            "total_runtime_min": _clean_number(sum(e["runtime_min"] for e in entries)),
        })

    # UV vs non-UV tower split
    uv_towers = [t for t in tower_rows if t.get("uv_tower")]
    non_uv_towers = [t for t in tower_rows if not t.get("uv_tower")]

    return {
        "scope": intelligence.get("scope") or {},
        "summary": intelligence.get("summary") or {},
        "folders": all_folders,
        "unused_folders": unused_folders,
        "speed": {
            "overall": complexity.get("overall") or {},
            "by_category": (complexity.get("by_category") or []),
            "by_folder": speed_by_folder,
            "by_machine": speed_by_machine,
            "fastest": (complexity.get("fastest_folders") or [])[:5],
            "slowest": (complexity.get("slowest_folders") or [])[:5],
            "highest_complexity_share": (complexity.get("highest_complexity_share_folders") or [])[:5],
        },
        "complexity_vs_loss": complexity_loss_summary,
        "loss_time": {
            "dominant_driver": loss_time_sec.get("dominant_driver"),
            "driver_totals": loss_time_sec.get("driver_totals"),
            "top_loss_days": (loss_time_sec.get("top_loss_days") or [])[:6],
            "low_loss_days": (loss_time_sec.get("low_loss_days") or [])[:4],
            "all_days": all_days,
        },
        "towers": tower_rows,
        "uv_towers": uv_towers,
        "non_uv_towers": non_uv_towers,
    }


def build_capacity_intelligence(payload: dict[str, Any]) -> dict[str, Any]:
    """Build intelligence from parsed dashboard payloads, not raw workbook rows."""
    deterministic = _build_deterministic_intelligence(
        summary=payload.get("summary") or {},
        daily_rows=payload.get("daily") or [],
        folder_rows=payload.get("details") or [],
        scope_label=_clean_text(payload.get("scope_label")),
    )

    llm_summary, llm_status = _build_llm_summary(deterministic)

    return {
        **deterministic,
        "llm": llm_status,
        "llm_summary": llm_summary,
        "generated_at": datetime.now(timezone.utc).isoformat(),
    }


def _build_deterministic_intelligence(
    summary: dict[str, Any],
    daily_rows: list[dict[str, Any]],
    folder_rows: list[dict[str, Any]],
    scope_label: str,
) -> dict[str, Any]:
    dates = _sorted_unique(
        [
            *[row.get("run_date") for row in daily_rows],
            *[row.get("run_date") for row in folder_rows],
        ]
    )
    folders = _sorted_unique(row.get("folder") for row in folder_rows)

    complexity_speed = _build_complexity_speed_analysis(folder_rows)
    folder_utilization = _build_folder_utilization_analysis(folder_rows, dates)
    loss_time = _build_loss_time_analysis(daily_rows, folder_rows)

    deterministic_notes = _build_deterministic_notes(
        complexity_speed=complexity_speed,
        folder_utilization=folder_utilization,
        loss_time=loss_time,
    )

    return {
        "status": "ready",
        "scope": {
            "label": scope_label or "Selected timeframe",
            "start_date": dates[0] if dates else "",
            "end_date": dates[-1] if dates else "",
            "production_days": len(dates),
            "folder_count": len(folders),
        },
        "summary": {
            "total_available_capacity": _clean_number(summary.get("total_available_capacity")),
            "total_runtime": _clean_number(summary.get("total_runtime")),
            "total_lost_time": _clean_number(summary.get("total_lost_time")),
            "total_spare_time": _clean_number(summary.get("total_buffer_time")),
            "total_unplanned_time": _clean_number(summary.get("total_idle_time")),
            "spare_capacity_percentage": _clean_number(summary.get("spare_capacity_percentage")),
            "unplanned_capacity_percentage": _clean_number(summary.get("idle_capacity_percentage")),
            "average_speed_cph": complexity_speed["overall"]["average_speed_cph"],
            "simple_speed_cph": complexity_speed["overall"]["simple_speed_cph"],
            "complex_speed_cph": complexity_speed["overall"]["complex_speed_cph"],
            "complexity_speed_gap_percentage": complexity_speed["overall"]["complexity_speed_gap_percentage"],
            "complex_runtime_share_percentage": complexity_speed["overall"]["complex_runtime_share_percentage"],
            "average_folder_utilization_percentage": folder_utilization["average_utilization_percentage"],
            "folder_utilization_range_percentage_points": folder_utilization["range_percentage_points"],
            "total_loss_time_minutes": loss_time["total_loss_time_minutes"],
            "loss_time_percentage": loss_time["loss_time_percentage"],
            "dominant_loss_driver": loss_time["dominant_driver"]["label"] if loss_time["dominant_driver"] else "",
            "peak_loss_day": loss_time["peak_day"]["run_date"] if loss_time["peak_day"] else "",
            "peak_loss_minutes": loss_time["peak_day"]["lost_time_minutes"] if loss_time["peak_day"] else 0,
        },
        "sections": {
            "complexity_speed": complexity_speed,
            "folder_utilization": folder_utilization,
            "loss_time": loss_time,
        },
        "deterministic_notes": deterministic_notes,
    }


def _build_complexity_speed_analysis(folder_rows: list[dict[str, Any]]) -> dict[str, Any]:
    overall = _new_speed_bucket()
    simple = _new_speed_bucket()
    complex_bucket = _new_speed_bucket()
    by_category: dict[str, dict[str, Any]] = {}
    by_folder: dict[str, dict[str, Any]] = {}
    by_machine: dict[str, dict[str, Any]] = {}

    for row in folder_rows:
        folder_key = _clean_text(row.get("folder"))
        if not folder_key:
            continue

        machine, folder_name = _split_machine_folder(folder_key)
        folder_bucket = by_folder.setdefault(
            folder_key,
            {
                "resource": _display_resource_name(folder_key),
                "machine": machine,
                "folder": folder_name,
                "overall": _new_speed_bucket(),
                "simple": _new_speed_bucket(),
                "complex": _new_speed_bucket(),
                "categories": {},
            },
        )
        machine_bucket = by_machine.setdefault(
            machine,
            {
                "machine": machine,
                "overall": _new_speed_bucket(),
                "simple": _new_speed_bucket(),
                "complex": _new_speed_bucket(),
            },
        )

        for segment in row.get("runtime_segments") or []:
            if not isinstance(segment, dict):
                continue

            minutes = _number(segment.get("minutes"))
            if minutes <= 0:
                continue

            label = _complexity_label(segment)
            is_complex = _is_complex_segment(segment)
            speed = _number(segment.get("effective_speed"))
            print_order = _number(segment.get("print_order"))

            _add_speed_bucket(overall, minutes, speed, print_order)
            _add_speed_bucket(folder_bucket["overall"], minutes, speed, print_order)
            _add_speed_bucket(machine_bucket["overall"], minutes, speed, print_order)

            category = by_category.setdefault(
                label,
                {
                    "label": label,
                    "is_complex": is_complex,
                    "bucket": _new_speed_bucket(),
                },
            )
            _add_speed_bucket(category["bucket"], minutes, speed, print_order)

            folder_category = folder_bucket["categories"].setdefault(label, _new_speed_bucket())
            _add_speed_bucket(folder_category, minutes, speed, print_order)

            if is_complex:
                _add_speed_bucket(complex_bucket, minutes, speed, print_order)
                _add_speed_bucket(folder_bucket["complex"], minutes, speed, print_order)
                _add_speed_bucket(machine_bucket["complex"], minutes, speed, print_order)
            else:
                _add_speed_bucket(simple, minutes, speed, print_order)
                _add_speed_bucket(folder_bucket["simple"], minutes, speed, print_order)
                _add_speed_bucket(machine_bucket["simple"], minutes, speed, print_order)

    overall_summary = _speed_summary(overall)
    simple_summary = _speed_summary(simple)
    complex_summary = _speed_summary(complex_bucket)
    overall_speed = overall_summary["average_speed_cph"]
    simple_speed = simple_summary["average_speed_cph"]
    complex_speed = complex_summary["average_speed_cph"]
    speed_gap = max(simple_speed - complex_speed, 0.0)

    category_rows = []
    for item in by_category.values():
        summary = _speed_summary(item["bucket"])
        category_rows.append(
            {
                "label": item["label"],
                "is_complex": item["is_complex"],
                **summary,
                "runtime_share_percentage": _percentage(summary["runtime_minutes"], overall_summary["runtime_minutes"]),
            }
        )
    category_rows.sort(key=lambda row: (-row["runtime_minutes"], row["label"]))

    folder_rows = []
    for folder_key, item in by_folder.items():
        row = _speed_entity_summary(
            resource=item["resource"],
            machine=item["machine"],
            folder=item["folder"],
            overall=item["overall"],
            simple=item["simple"],
            complex_bucket=item["complex"],
            categories=item["categories"],
        )
        folder_rows.append(row)
    folder_rows.sort(key=lambda row: (-row["runtime_minutes"], row["resource"]))

    machine_rows = []
    for item in by_machine.values():
        row = _speed_entity_summary(
            resource=item["machine"],
            machine=item["machine"],
            folder="",
            overall=item["overall"],
            simple=item["simple"],
            complex_bucket=item["complex"],
            categories={},
        )
        machine_rows.append(row)
    machine_rows.sort(key=lambda row: (-row["runtime_minutes"], row["resource"]))

    return {
        "overall": {
            **overall_summary,
            "simple_speed_cph": simple_speed,
            "complex_speed_cph": complex_speed,
            "complex_runtime_minutes": complex_summary["runtime_minutes"],
            "complex_runtime_share_percentage": _percentage(
                complex_summary["runtime_minutes"],
                overall_summary["runtime_minutes"],
            ),
            "complexity_speed_gap_cph": _clean_number(speed_gap),
            "complexity_speed_gap_percentage": _percentage(speed_gap, simple_speed),
        },
        "by_category": category_rows,
        "by_folder": folder_rows,
        "by_machine": machine_rows,
        "fastest_folders": sorted(
            [row for row in folder_rows if row["average_speed_cph"] > 0],
            key=lambda row: (-row["average_speed_cph"], row["resource"]),
        )[:5],
        "slowest_folders": sorted(
            [row for row in folder_rows if row["average_speed_cph"] > 0],
            key=lambda row: (row["average_speed_cph"], row["resource"]),
        )[:5],
        "highest_complexity_share_folders": sorted(
            folder_rows,
            key=lambda row: (-row["complex_runtime_share_percentage"], row["resource"]),
        )[:5],
    }


def _build_folder_utilization_analysis(
    folder_rows: list[dict[str, Any]],
    dates: list[str],
) -> dict[str, Any]:
    folders = _sorted_unique(row.get("folder") for row in folder_rows)
    production_days = len(dates)
    grouped: dict[str, list[dict[str, Any]]] = {folder: [] for folder in folders}

    for row in folder_rows:
        folder = _clean_text(row.get("folder"))
        if folder:
            grouped.setdefault(folder, []).append(row)

    total_runtime = sum(_number(row.get("runtime")) for row in folder_rows)
    folder_summaries = []

    for folder in folders:
        rows = grouped.get(folder, [])
        machine, folder_name = _split_machine_folder(folder)
        possible_capacity = production_days * CAPACITY_MINUTES_PER_FOLDER_DAY if production_days else sum(
            _available_minutes(row) for row in rows
        )
        active_capacity = sum(_available_minutes(row) for row in rows)
        runtime = sum(_number(row.get("runtime")) for row in rows)
        lost_time_raw = sum(_number(row.get("lost_time")) for row in rows)
        waiting_time = sum(_number(row.get("waiting_time")) for row in rows)
        non_wait_lost_time = max(lost_time_raw - waiting_time, 0)
        downtime = sum(_number(row.get("downtime")) for row in rows)
        buffer_time = sum(_number(row.get("buffer_time")) for row in rows)
        if buffer_time <= 0 and rows:
            buffer_time = max(active_capacity - runtime - lost_time_raw - downtime, 0)
        active_days = len({row.get("run_date") for row in rows if row.get("run_date")})
        unplanned_days = max(production_days - active_days, 0)
        unplanned_time = max(possible_capacity - active_capacity, 0)
        daily_runtime_percentages = _daily_folder_runtime_percentages(rows, dates)
        variability = pstdev(daily_runtime_percentages) if len(daily_runtime_percentages) > 1 else 0.0
        utilization = _percentage(runtime, possible_capacity)
        active_day_utilization = _percentage(runtime, active_capacity)
        loss_share = _percentage(non_wait_lost_time, runtime + non_wait_lost_time) if runtime + non_wait_lost_time > 0 else 0

        folder_summaries.append(
            {
                "resource": _display_resource_name(folder),
                "machine": machine,
                "folder": folder_name,
                "runtime_minutes": _clean_number(runtime),
                "lost_time_minutes": _clean_number(non_wait_lost_time),
                "waiting_time_minutes": _clean_number(waiting_time),
                "downtime_minutes": _clean_number(downtime),
                "buffer_time_minutes": _clean_number(buffer_time),
                "unplanned_time_minutes": _clean_number(unplanned_time),
                "possible_capacity_minutes": _clean_number(possible_capacity),
                "active_capacity_minutes": _clean_number(active_capacity),
                "utilization_percentage": utilization,
                "active_day_utilization_percentage": active_day_utilization,
                "loss_share_percentage": _clean_number(loss_share),
                "load_share_percentage": _percentage(runtime, total_runtime),
                "active_days": active_days,
                "idle_days": unplanned_days,
                "runtime_variability_percentage_points": _clean_number(variability),
                "classification": _folder_utilization_classification(
                    utilization=utilization,
                    active_day_utilization=active_day_utilization,
                    variability=variability,
                    idle_days=idle_days,
                    production_days=production_days,
                ),
            }
        )

    folder_summaries.sort(key=lambda row: (-row["runtime_minutes"], row["resource"]))
    utilization_values = [row["utilization_percentage"] for row in folder_summaries]

    return {
        "folders": folder_summaries,
        "highest_utilization": sorted(
            folder_summaries,
            key=lambda row: (-row["utilization_percentage"], row["resource"]),
        )[:5],
        "lowest_utilization": sorted(
            folder_summaries,
            key=lambda row: (row["utilization_percentage"], row["resource"]),
        )[:5],
        "highest_loss_share": sorted(
            folder_summaries,
            key=lambda row: (-row["loss_share_percentage"], row["resource"]),
        )[:5],
        "most_variable": sorted(
            folder_summaries,
            key=lambda row: (-row["runtime_variability_percentage_points"], row["resource"]),
        )[:5],
        "average_utilization_percentage": _clean_number(_average(utilization_values)),
        "range_percentage_points": _clean_number(max(utilization_values) - min(utilization_values) if utilization_values else 0),
        "production_days": production_days,
        "folder_count": len(folder_summaries),
    }


def _build_loss_time_analysis(
    daily_rows: list[dict[str, Any]],
    folder_rows: list[dict[str, Any]],
) -> dict[str, Any]:
    days = _sorted_unique(
        [
            *[row.get("run_date") for row in daily_rows],
            *[row.get("run_date") for row in folder_rows],
        ]
    )
    details_by_day: dict[str, list[dict[str, Any]]] = {day: [] for day in days}
    daily_by_day = {_clean_text(row.get("run_date")): row for row in daily_rows if row.get("run_date")}

    for row in folder_rows:
        run_date = _clean_text(row.get("run_date"))
        if run_date:
            details_by_day.setdefault(run_date, []).append(row)

    day_rows = []
    driver_totals = {key: 0.0 for key, _ in LOSS_COMPONENTS}

    for day in days:
        details = details_by_day.get(day, [])
        daily = daily_by_day.get(day, {})
        available = _number(daily.get("available_capacity")) or sum(_available_minutes(row) for row in details)
        runtime = _number(daily.get("runtime")) or sum(_number(row.get("runtime")) for row in details)
        lost_time_raw = _number(daily.get("lost_time")) or sum(_number(row.get("lost_time")) for row in details)
        waiting_time = sum(_number(row.get("waiting_time")) for row in details)
        lost_time = max(lost_time_raw - waiting_time, 0)
        component_values = {
            key: sum(_number(row.get(key)) for row in details)
            for key, _ in LOSS_COMPONENTS
        }

        for key, value in component_values.items():
            driver_totals[key] += value

        dominant_key, dominant_value = _dominant_component(component_values)
        top_folders = _top_loss_folders(details)

        day_rows.append(
            {
                "run_date": day,
                "runtime_minutes": _clean_number(runtime),
                "lost_time_minutes": _clean_number(lost_time),
                "waiting_time_minutes": _clean_number(waiting_time),
                "available_capacity_minutes": _clean_number(available),
                "loss_percentage": _percentage(lost_time, available),
                "loss_per_runtime_percentage": _percentage(lost_time, runtime),
                "dominant_driver": _driver_row(dominant_key, dominant_value),
                "components": [_driver_row(key, value) for key, value in component_values.items()],
                "top_folders": top_folders[:3],
            }
        )

    day_rows.sort(key=lambda row: row["run_date"])
    top_loss_days = sorted(day_rows, key=lambda row: (-row["lost_time_minutes"], row["run_date"]))[:6]
    low_loss_days = sorted(day_rows, key=lambda row: (row["lost_time_minutes"], row["run_date"]))[:6]
    total_lost_time = sum(_number(row["lost_time_minutes"]) for row in day_rows)
    total_available = sum(_number(row["available_capacity_minutes"]) for row in day_rows)
    dominant_key, dominant_value = _dominant_component(driver_totals)

    return {
        "total_loss_time_minutes": _clean_number(total_lost_time),
        "loss_time_percentage": _percentage(total_lost_time, total_available),
        "dominant_driver": _driver_row(dominant_key, dominant_value),
        "driver_totals": [
            _driver_row(key, value)
            for key, value in sorted(driver_totals.items(), key=lambda item: -item[1])
        ],
        "days": day_rows,
        "top_loss_days": top_loss_days,
        "low_loss_days": low_loss_days,
        "peak_day": top_loss_days[0] if top_loss_days else None,
        "inferred_factor": LOSS_DRIVER_INFERENCES.get(dominant_key, ""),
    }


def _build_deterministic_notes(
    complexity_speed: dict[str, Any],
    folder_utilization: dict[str, Any],
    loss_time: dict[str, Any],
) -> list[str]:
    notes = []
    overall = complexity_speed.get("overall") or {}
    if overall.get("complex_runtime_minutes", 0) > 0:
        notes.append(
            f"Complex work accounts for {overall.get('complex_runtime_share_percentage', 0)}% of runtime and runs {overall.get('complexity_speed_gap_percentage', 0)}% slower than simple work based on parsed speed segments."
        )
    else:
        notes.append("No complex runtime segments were found in the parsed speed data for this scope.")

    fastest = (complexity_speed.get("fastest_folders") or [{}])[0]
    slowest = (complexity_speed.get("slowest_folders") or [{}])[0]
    if fastest.get("resource") and slowest.get("resource"):
        notes.append(
            f"Fastest folder by average speed is {fastest['resource']} at {fastest.get('average_speed_cph', 0)} cph; slowest is {slowest['resource']} at {slowest.get('average_speed_cph', 0)} cph."
        )

    folder_rows = folder_utilization.get("folders") or []
    if folder_rows:
        notes.append(
            f"Average folder utilization is {folder_utilization.get('average_utilization_percentage', 0)}%, with a {folder_utilization.get('range_percentage_points', 0)} point spread across folders."
        )

    driver = loss_time.get("dominant_driver") or {}
    if driver.get("label"):
        notes.append(
            f"Loss time is led by {driver['label']} at {driver.get('minutes', 0)} minutes, suggesting {loss_time.get('inferred_factor', 'an operational timing contributor')}."
        )

    peak_day = loss_time.get("peak_day") or {}
    if peak_day.get("run_date"):
        notes.append(
            f"Peak loss day is {peak_day['run_date']} with {peak_day.get('lost_time_minutes', 0)} lost minutes."
        )

    return notes


def _build_llm_summary(intelligence: dict[str, Any]) -> tuple[dict[str, Any], dict[str, Any]]:
    if _get_env("CAPACITY_INTELLIGENCE_LLM_ENABLED", "true").strip().casefold() in {"0", "false", "no", "off"}:
        return _fallback_summary(intelligence), {
            "enabled": False,
            "used": False,
            "status": "disabled",
            "message": "LLM summary disabled by CAPACITY_INTELLIGENCE_LLM_ENABLED.",
        }

    endpoint = _get_env("AZURE_ENDPOINT")
    api_key = (
        _get_env("API_KEY")
        or _get_env("AZURE_API_KEY")
        or _get_env("AZURE_OPENAI_API_KEY")
        or _get_env("AZURE_INFERENCE_KEY")
    )

    if not endpoint or not api_key:
        return _fallback_summary(intelligence), {
            "enabled": False,
            "used": False,
            "status": "unconfigured",
            "message": "AZURE_ENDPOINT and API_KEY are required for LLM synthesis.",
        }

    compact_facts = _compact_intelligence_for_llm(intelligence)
    messages = [
        {
            "role": "system",
            "content": (
                "You are preparing executive-review capacity insights for a print plant. "
                "Use only the supplied JSON facts, which are derived from parsed dashboard data, not raw workbook rows. "
                "There are two base print categories: SNP and GNP. SNP Complex is the complex variant within SNP, "
                "and GNP Complex is the complex variant within GNP. Focus only on interesting, non-obvious executive insights: "
                "complexity impact on machine/folder speed, high-level folder utilization comparison, and meaningful loss-time drivers. "
                "Avoid redundant threshold-style statements. Return concise JSON with keys: headline, key_summary_points, recommended_actions. "
                "Treat spare time and unplanned time as entirely separate facts: spare (buffer_time) is leftover capacity on active nights; unplanned is capacity on nights with no scheduled activity. NEVER combine them. Waiting time is separate from loss time — do not include waiting in loss time figures. "
                "key_summary_points and recommended_actions must be arrays of short, concrete strings."
            ),
        },
        {
            "role": "user",
            "content": json.dumps(compact_facts, separators=(",", ":"), ensure_ascii=True),
        },
    ]

    try:
        text = _call_chat_completion(endpoint, api_key, messages)
        return _parse_llm_summary(text, intelligence), {
            "enabled": True,
            "used": True,
            "status": "ready",
            "message": "LLM synthesis completed.",
        }
    except Exception as exc:
        return _fallback_summary(intelligence), {
            "enabled": True,
            "used": False,
            "status": "error",
            "message": _sanitize_error_message(exc, api_key),
        }


def _fallback_summary(intelligence: dict[str, Any]) -> dict[str, Any]:
    summary = intelligence.get("summary") or {}
    sections = intelligence.get("sections") or {}
    headline = (
        f"Average speed is {summary.get('average_speed_cph', 0)} cph, "
        f"folder utilization averages {summary.get('average_folder_utilization_percentage', 0)}%, "
        f"and loss time is {summary.get('total_loss_time_minutes', 0)} minutes."
    )

    actions = [
        "Compare slow folders against their complexity mix before treating low speed as a machine issue.",
        "Review folders with high utilization and high loss share for sequencing or readiness gaps.",
        "Prioritize the dominant loss driver on the peak loss days before adjusting overall capacity.",
    ]

    notes = intelligence.get("deterministic_notes") or []
    if not notes and sections.get("loss_time", {}).get("inferred_factor"):
        notes = [sections["loss_time"]["inferred_factor"]]

    return {
        "headline": headline,
        "key_summary_points": notes,
        "observations": notes,
        "recommended_actions": actions,
    }


def _compact_intelligence_for_llm(intelligence: dict[str, Any]) -> dict[str, Any]:
    sections = intelligence.get("sections") or {}
    complexity = sections.get("complexity_speed") or {}
    folder_utilization = sections.get("folder_utilization") or {}
    loss_time = sections.get("loss_time") or {}

    return {
        "scope": intelligence.get("scope"),
        "summary": intelligence.get("summary"),
        "complexity_speed": {
            "overall": complexity.get("overall"),
            "by_category": (complexity.get("by_category") or [])[:6],
            "fastest_folders": (complexity.get("fastest_folders") or [])[:4],
            "slowest_folders": (complexity.get("slowest_folders") or [])[:4],
            "highest_complexity_share_folders": (complexity.get("highest_complexity_share_folders") or [])[:4],
            "by_machine": (complexity.get("by_machine") or [])[:6],
        },
        "folder_utilization": {
            "average_utilization_percentage": folder_utilization.get("average_utilization_percentage"),
            "range_percentage_points": folder_utilization.get("range_percentage_points"),
            "highest_utilization": (folder_utilization.get("highest_utilization") or [])[:4],
            "lowest_utilization": (folder_utilization.get("lowest_utilization") or [])[:4],
            "highest_loss_share": (folder_utilization.get("highest_loss_share") or [])[:4],
            "most_variable": (folder_utilization.get("most_variable") or [])[:4],
        },
        "loss_time": {
            "total_loss_time_minutes": loss_time.get("total_loss_time_minutes"),
            "loss_time_percentage": loss_time.get("loss_time_percentage"),
            "dominant_driver": loss_time.get("dominant_driver"),
            "driver_totals": loss_time.get("driver_totals"),
            "top_loss_days": (loss_time.get("top_loss_days") or [])[:5],
            "inferred_factor": loss_time.get("inferred_factor"),
        },
    }


def _new_speed_bucket() -> dict[str, float]:
    return {
        "runtime_minutes": 0.0,
        "print_order": 0.0,
        "weighted_speed_total": 0.0,
        "speed_weight_minutes": 0.0,
    }


def _add_speed_bucket(bucket: dict[str, float], minutes: float, speed: float, print_order: float) -> None:
    bucket["runtime_minutes"] += minutes
    bucket["print_order"] += print_order
    if speed > 0:
        bucket["weighted_speed_total"] += speed * minutes
        bucket["speed_weight_minutes"] += minutes


def _speed_summary(bucket: dict[str, float]) -> dict[str, Any]:
    runtime = bucket.get("runtime_minutes", 0.0)
    speed_weight = bucket.get("speed_weight_minutes", 0.0)
    weighted_speed = bucket.get("weighted_speed_total", 0.0)
    print_order = bucket.get("print_order", 0.0)
    average_speed = weighted_speed / speed_weight if speed_weight > 0 else _speed_from_print_order(print_order, runtime)

    return {
        "runtime_minutes": _clean_number(runtime),
        "print_order": _clean_number(print_order),
        "average_speed_cph": _clean_number(average_speed),
    }


def _speed_entity_summary(
    resource: str,
    machine: str,
    folder: str,
    overall: dict[str, float],
    simple: dict[str, float],
    complex_bucket: dict[str, float],
    categories: dict[str, dict[str, float]],
) -> dict[str, Any]:
    overall_summary = _speed_summary(overall)
    simple_summary = _speed_summary(simple)
    complex_summary = _speed_summary(complex_bucket)
    simple_speed = simple_summary["average_speed_cph"]
    complex_speed = complex_summary["average_speed_cph"]
    speed_gap = max(simple_speed - complex_speed, 0.0)
    dominant_category = ""
    if categories:
        dominant_category = max(categories.items(), key=lambda item: item[1]["runtime_minutes"])[0]

    return {
        "resource": resource,
        "machine": machine,
        "folder": folder,
        **overall_summary,
        "simple_speed_cph": simple_speed,
        "complex_speed_cph": complex_speed,
        "complex_runtime_minutes": complex_summary["runtime_minutes"],
        "complex_runtime_share_percentage": _percentage(
            complex_summary["runtime_minutes"],
            overall_summary["runtime_minutes"],
        ),
        "complexity_speed_gap_percentage": _percentage(speed_gap, simple_speed),
        "dominant_complexity": dominant_category,
    }


def _daily_folder_runtime_percentages(rows: list[dict[str, Any]], dates: list[str]) -> list[float]:
    rows_by_day = {
        _clean_text(row.get("run_date")): row
        for row in rows
        if row.get("run_date")
    }
    percentages = []
    for day in dates:
        row = rows_by_day.get(day, {})
        percentages.append(_percentage(_number(row.get("runtime")), CAPACITY_MINUTES_PER_FOLDER_DAY))
    return percentages


def _folder_utilization_classification(
    utilization: float,
    active_day_utilization: float,
    variability: float,
    idle_days: int,
    production_days: int,
) -> str:
    idle_share = idle_days / production_days if production_days else 0
    if utilization >= 70 and active_day_utilization >= 75:
        return "High sustained load"
    if idle_share >= 0.4 and active_day_utilization >= 60:
        return "Intermittent but intense"
    if utilization < 25 and idle_share >= 0.4:
        return "Under-scheduled"
    if variability >= 30:
        return "Variable load"
    return "Balanced"


def _top_loss_folders(details: list[dict[str, Any]]) -> list[dict[str, Any]]:
    grouped: dict[str, float] = {}
    for row in details:
        folder = _clean_text(row.get("folder"))
        if not folder:
            continue
        grouped[folder] = grouped.get(folder, 0.0) + _number(row.get("lost_time"))

    return [
        {
            "resource": _display_resource_name(folder),
            "lost_time_minutes": _clean_number(value),
        }
        for folder, value in sorted(grouped.items(), key=lambda item: (-item[1], item[0]))
        if value > 0
    ]


def _dominant_component(component_values: dict[str, float]) -> tuple[str, float]:
    if not component_values:
        return "", 0.0
    return max(component_values.items(), key=lambda item: item[1])


def _driver_row(key: str, value: float) -> dict[str, Any] | None:
    if not key:
        return None
    label = dict(LOSS_COMPONENTS).get(key, key)
    return {
        "key": key,
        "label": label,
        "minutes": _clean_number(value),
        "inference": LOSS_DRIVER_INFERENCES.get(key, ""),
    }


def _complexity_label(segment: dict[str, Any]) -> str:
    label = _clean_text(segment.get("label"))
    if label:
        return label

    segment_type = _clean_text(segment.get("type"))
    if segment_type:
        return f"{segment_type} Complex" if _is_complex_segment(segment) else segment_type

    key = _clean_text(segment.get("key"))
    return key.replace("_", " ").title() if key else "Unknown"


def _is_complex_segment(segment: dict[str, Any]) -> bool:
    if bool(segment.get("is_complex")):
        return True
    text = " ".join(
        [
            _clean_text(segment.get("key")),
            _clean_text(segment.get("label")),
            _clean_text(segment.get("type")),
        ]
    ).casefold()
    return "complex" in text


def _split_machine_folder(value: str) -> tuple[str, str]:
    parts = [_clean_text(part) for part in _clean_text(value).split("\n") if _clean_text(part)]
    if len(parts) >= 2:
        return parts[0], parts[1]
    text = _display_resource_name(value)
    return text, text


def _available_minutes(row: dict[str, Any]) -> float:
    available = _number(row.get("available_capacity"))
    return available if available > 0 else CAPACITY_MINUTES_PER_FOLDER_DAY


def _speed_from_print_order(print_order: float, runtime_minutes: float) -> float:
    if runtime_minutes <= 0:
        return 0.0
    return print_order / (runtime_minutes / 60.0)


def _percentage(numerator: float, denominator: float) -> float:
    if denominator <= 0:
        return 0.0
    return _clean_number(min(max((float(numerator) / float(denominator)) * 100, 0.0), 100.0))


def _number(value: Any) -> float:
    try:
        numeric = float(value)
    except (TypeError, ValueError):
        return 0.0
    return numeric if isfinite(numeric) else 0.0


def _clean_number(value: Any) -> int | float:
    numeric = _number(value)
    rounded = round(numeric, 2)
    if rounded.is_integer():
        return int(rounded)
    return rounded


def _average(values: list[float]) -> float:
    return sum(values) / len(values) if values else 0.0


def _sorted_unique(values: Any) -> list[str]:
    unique_values = {
        _clean_text(value)
        for value in values
        if _clean_text(value)
    }
    return sorted(unique_values, key=_resource_sort_key)


def _resource_sort_key(value: str) -> tuple[str, int, str]:
    text = _display_resource_name(value)
    digits = ""
    prefix = []
    for character in text:
        if character.isdigit():
            digits += character
        elif not digits:
            prefix.append(character)
    return ("".join(prefix).casefold(), int(digits or "999999"), text.casefold())


def _display_resource_name(value: Any) -> str:
    return _clean_text(value).replace("\n", " / ")


def _clean_text(value: Any) -> str:
    if value is None:
        return ""
    return str(value).strip()


def _string_list(value: Any) -> list[str]:
    if not isinstance(value, list):
        return []
    return [_clean_text(item) for item in value if _clean_text(item)]


def _call_chat_completion(endpoint: str, api_key: str, messages: list[dict[str, str]]) -> str:
    url = _build_chat_completion_url(endpoint)
    urls = [url]
    if not _get_env("AZURE_API_VERSION") and "api-version=" in url:
        urls.append(_without_api_version(url))

    base_payload: dict[str, Any] = {
        "messages": messages,
        "temperature": 0.2,
        "max_tokens": 900,
    }

    model = _get_env("AZURE_MODEL") or _get_env("AZURE_OPENAI_MODEL") or _get_env("AZURE_DEPLOYMENT")
    if model:
        base_payload["model"] = model

    payloads = [
        {**base_payload, "response_format": {"type": "json_object"}},
        base_payload,
    ]
    auth_modes = ["api-key", "bearer"]
    last_error: Exception | None = None

    for request_url in urls:
        for payload in payloads:
            for auth_mode in auth_modes:
                try:
                    response = _post_json(request_url, payload, api_key, auth_mode)
                    text = _extract_llm_text(response)
                    if text:
                        return text
                    raise RuntimeError("LLM response did not contain text content.")
                except HTTPError as exc:
                    last_error = exc
                    if exc.code in {400, 401, 403, 404}:
                        continue
                    raise
                except URLError as exc:
                    last_error = exc
                    break

    if last_error:
        raise last_error
    raise RuntimeError("LLM request failed.")


def _call_plain_chat_completion(endpoint: str, api_key: str, messages: list[dict[str, str]]) -> str:
    """Like _call_chat_completion but never requests JSON mode — returns plain text."""
    url = _build_chat_completion_url(endpoint)
    urls = [url]
    if not _get_env("AZURE_API_VERSION") and "api-version=" in url:
        urls.append(_without_api_version(url))

    payload: dict[str, Any] = {"messages": messages, "temperature": 0.2, "max_tokens": 600}
    model = _get_env("AZURE_MODEL") or _get_env("AZURE_OPENAI_MODEL") or _get_env("AZURE_DEPLOYMENT")
    if model:
        payload["model"] = model

    auth_modes = ["api-key", "bearer"]
    last_error: Exception | None = None

    for request_url in urls:
        for auth_mode in auth_modes:
            try:
                response = _post_json(request_url, payload, api_key, auth_mode)
                text = _extract_llm_text(response)
                if text:
                    return text
                raise RuntimeError("LLM response did not contain text content.")
            except HTTPError as exc:
                last_error = exc
                if exc.code in {400, 401, 403, 404}:
                    continue
                raise
            except URLError as exc:
                last_error = exc
                break

    if last_error:
        raise last_error
    raise RuntimeError("LLM request failed.")


def _build_chat_completion_url(endpoint: str) -> str:
    url = endpoint.strip()
    deployment = _get_env("AZURE_DEPLOYMENT") or _get_env("AZURE_DEPLOYMENT_NAME")

    if "/chat/completions" not in url and "/responses" not in url:
        if _is_openai_v1_base_url(url):
            url = f"{url.rstrip('/')}/chat/completions"
            return _with_api_version(url)

        if deployment:
            base = url.rstrip("/")
            if not base.endswith("/openai"):
                url = f"{base}/openai/deployments/{quote(deployment)}/chat/completions"
            else:
                url = f"{base}/deployments/{quote(deployment)}/chat/completions"
        else:
            url = f"{url.rstrip('/')}/chat/completions"

    return _with_api_version(url)


def _with_api_version(url: str) -> str:
    api_version = _get_env("AZURE_API_VERSION", "2024-05-01-preview")
    if not _get_env("AZURE_API_VERSION") and _is_openai_v1_url(url):
        return url
    if not api_version:
        return url

    parts = urlsplit(url)
    if "api-version=" in parts.query:
        return url

    query = parts.query
    api_query = urlencode({"api-version": api_version})
    query = f"{query}&{api_query}" if query else api_query
    return urlunsplit((parts.scheme, parts.netloc, parts.path, query, parts.fragment))


def _is_openai_v1_base_url(url: str) -> bool:
    path = urlsplit(url).path.rstrip("/")
    return path.endswith("/openai/v1")


def _is_openai_v1_url(url: str) -> bool:
    path = urlsplit(url).path
    return "/openai/v1" in path


def _without_api_version(url: str) -> str:
    parts = urlsplit(url)
    query_pairs = [
        (key, value)
        for key, value in parse_qsl(parts.query, keep_blank_values=True)
        if key != "api-version"
    ]
    return urlunsplit((parts.scheme, parts.netloc, parts.path, urlencode(query_pairs), parts.fragment))


def _post_json(url: str, payload: dict[str, Any], api_key: str, auth_mode: str) -> dict[str, Any]:
    headers = {
        "Content-Type": "application/json",
        "Accept": "application/json",
    }
    if auth_mode == "bearer":
        headers["Authorization"] = f"Bearer {api_key}"
    else:
        headers["api-key"] = api_key

    request = Request(
        url,
        data=json.dumps(payload, ensure_ascii=True).encode("utf-8"),
        headers=headers,
        method="POST",
    )
    with urlopen(request, timeout=REQUEST_TIMEOUT_SECONDS) as response:
        body = response.read().decode("utf-8")
    return json.loads(body)


def _extract_llm_text(response: dict[str, Any]) -> str:
    choices = response.get("choices")
    if isinstance(choices, list) and choices:
        message = choices[0].get("message") or {}
        content = message.get("content")
        if isinstance(content, str):
            return content
        if isinstance(content, list):
            text_parts = [part.get("text", "") for part in content if isinstance(part, dict)]
            return "\n".join(part for part in text_parts if part)

    output_text = response.get("output_text")
    if isinstance(output_text, str):
        return output_text

    output = response.get("output")
    if isinstance(output, list):
        text_parts = []
        for item in output:
            for content in item.get("content", []) if isinstance(item, dict) else []:
                if isinstance(content, dict):
                    text_parts.append(content.get("text") or content.get("content") or "")
        return "\n".join(part for part in text_parts if part)

    content = response.get("content")
    return content if isinstance(content, str) else ""


def _parse_llm_summary(text: str, intelligence: dict[str, Any]) -> dict[str, Any]:
    cleaned = text.strip()
    if cleaned.startswith("```"):
        cleaned = cleaned.strip("`")
        if cleaned.lower().startswith("json"):
            cleaned = cleaned[4:].strip()

    start = cleaned.find("{")
    end = cleaned.rfind("}")
    if start >= 0 and end > start:
        cleaned = cleaned[start:end + 1]

    try:
        parsed = json.loads(cleaned)
    except json.JSONDecodeError:
        fallback = _fallback_summary(intelligence)
        fallback["observations"] = [text.strip()[:900]] if text.strip() else fallback["observations"]
        return fallback

    fallback = _fallback_summary(intelligence)
    key_summary_points = (
        _string_list(parsed.get("key_summary_points"))
        or _string_list(parsed.get("observations"))
        or fallback["key_summary_points"]
    )
    recommended_actions = _string_list(parsed.get("recommended_actions"))[:5] or fallback["recommended_actions"]

    return {
        "headline": _clean_text(parsed.get("headline")) or fallback["headline"],
        "key_summary_points": key_summary_points[:5],
        "observations": key_summary_points[:5],
        "recommended_actions": recommended_actions,
    }


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


def _sanitize_error_message(exc: Exception, api_key: str) -> str:
    if isinstance(exc, HTTPError):
        try:
            detail = exc.read().decode("utf-8")[:240]
        except Exception:
            detail = ""
        message = f"LLM request failed with HTTP {exc.code}. {detail}".strip()
    elif isinstance(exc, URLError):
        message = f"LLM request failed: {exc.reason}"
    else:
        message = str(exc)

    if api_key:
        message = message.replace(api_key, "<redacted>")
    if "Missed model deployment" in message:
        message = (
            "LLM request failed: Azure requires a model deployment. "
            "Set AZURE_DEPLOYMENT for Azure OpenAI deployments or AZURE_MODEL for Azure AI Foundry model routing."
        )
    return message[:320]
