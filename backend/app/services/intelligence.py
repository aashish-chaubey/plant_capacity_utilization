from __future__ import annotations

from datetime import datetime, timezone
from math import isfinite
import json
import os
from pathlib import Path
import re
import ssl
from statistics import pstdev
from typing import Any
from urllib.error import HTTPError, URLError
from urllib.parse import parse_qsl, quote, urlencode, urlsplit, urlunsplit
from urllib.request import Request, urlopen


CAPACITY_MINUTES_PER_FOLDER_DAY = 240.0
PF_COMPLIANCE_MINUTES_BY_PLANT = {
    "baroda": 180.0,
    "manesar": 180.0,
    "trivandrum": 150.0,
}
REQUEST_TIMEOUT_SECONDS = 90
MALT_WAIT_PERCENTILE = 50
MALT_MOT_PERCENTILE = 85
MALT_SPARE_PERCENTILE = 30
MALT_IDENTITY_TOLERANCE_MINUTES = 1.0

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
    intelligence: dict[str, Any] | None = None,
    summary: dict[str, Any] | None = None,
    daily_rows: list[dict[str, Any]] | None = None,
    details: list[dict[str, Any]] | None = None,
    tower_details: list[dict[str, Any]] | None = None,
    downtime_reasons: list[dict[str, Any]] | None = None,
    history: list[dict[str, str]] | None = None,
) -> dict[str, Any]:
    intelligence = intelligence or {}
    summary = summary or {}
    daily_rows = daily_rows or []
    details = details or []
    tower_details = tower_details or []
    downtime_reasons = downtime_reasons or []
    history = history or []

    if not (intelligence.get("sections") or {}) and (summary or daily_rows or details):
        intelligence = _build_deterministic_intelligence(
            summary=summary,
            daily_rows=daily_rows,
            folder_rows=details,
            scope_label=_chat_scope_label(daily_rows),
        )

    context = _build_chat_context(
        intelligence=intelligence,
        tower_details=tower_details,
        summary=summary,
        daily_rows=daily_rows,
        details=details,
        downtime_reasons=downtime_reasons,
        question=message,
    )
    deterministic_answer = _try_deterministic_chat_answer(message, context)
    if deterministic_answer:
        return {"answer": deterministic_answer, "status": "ok"}

    endpoint = _get_env("AZURE_ENDPOINT")
    api_key = (
        _get_env("API_KEY")
        or _get_env("AZURE_API_KEY")
        or _get_env("AZURE_OPENAI_API_KEY")
        or _get_env("AZURE_INFERENCE_KEY")
    )

    if not endpoint or not api_key:
        fallback_answer = _fallback_answer_from_context(message, context)
        if fallback_answer:
            return {"answer": fallback_answer, "status": "ok", "plan": None}
        return {"answer": "LLM is not configured.", "status": "unconfigured", "plan": None}

    # Phase 1 — Planner: lightweight call to decide data source + computation
    plan: dict[str, Any] | None = None
    try:
        plan = _call_planner(message, endpoint, api_key)
    except Exception as planner_exc:
        print(f"[chat] planner skipped: {_sanitize_error_message(planner_exc, api_key)}", flush=True)
        plan = None

    plan_section = ""
    if plan:
        plan_section = (
            "AGENT PLAN — execute this exactly before answering:\n"
            f"{json.dumps(plan, indent=2)}\n\n"
            "EXECUTION STEPS:\n"
            "1. Go to the primary_source listed in the plan and locate the relevant rows/fields\n"
            "2. Apply any filters from the plan (folder name, date, complexity, etc.)\n"
            "3. Perform the computation described step by step, quoting exact values\n"
            "4. Self-validate internally before answering: check values are non-negative where expected; "
            "verify Utilized Time = Runtime (SNP + GNP) + Loss Time + Downtime; "
            "spot-check that runtime + loss + downtime + wait + spare ≈ available_capacity. "
            "Do not reveal private reasoning; only return the answer.\n"
            "5. Format your response as specified in output_format\n\n"
        )

    llm_context = _compact_chat_context_for_llm(context)
    context_json = json.dumps(llm_context, separators=(",", ":"), ensure_ascii=True)

    system_content = (
        f"{plan_section}"
        "You are a concise analytics assistant for a print plant production dashboard. "
        "Answer ONLY from the JSON context supplied — never invent values. "
        "Use the curated computed JSON tables supplied here "
        "(exact_dashboard.folders, exact_dashboard.daily, towers, tower_availability, downtime_by_reason, "
        "delayed_pf, max_allowable_loss_time, editions_* tables) before using summary aggregates. "
        "Do not assume access to anything outside this JSON context. "
        "Prefer exact_dashboard values over derived summaries whenever a numeric answer is available. "
        "Before responding, internally identify the metric, filters, numerator, denominator, and formula. "
        "Validate the arithmetic against the JSON, then provide only the final concise answer. "
        "Be brief and direct — no preamble, no filler. "
        "For ranked results use a short numbered list. "
        "Always report duration values in minutes. Do not convert durations into hours or h:mm. "
        "Clock times such as 03:00 or 04:00 may remain clock times. "
        "If the answer is genuinely absent from the data, say: Not available in the current data.\n\n"

        "QUERY INTERPRETATION RULES:\n"
        "- 'runtime' / 'run time' with no qualifier: total aggregate runtime across ALL complexity types. "
        "Report the combined figure first. Break down by SNP/GNP only if the user explicitly says 'SNP runtime', 'GNP runtime', or 'by type'.\n"
        "- 'SNP runtime': sum complexity_by_code entries where type='SNP' (codes C1–C3).\n"
        "- 'GNP runtime': sum complexity_by_code entries where type='GNP' (codes C5–C8).\n"
        "- 'complex runtime': sum entries where is_complex=true (C4 + C9–C15).\n"
        "- 'speed' / 'average speed' with no qualifier: overall average_speed_cph. Qualify by type only when asked.\n"
        "- 'loss time' / 'losses': total lost_time (changeover + late-start + reflong). Waiting time is always separate.\n"
        "- 'spare time' / 'spare capacity': always buffer_time (= spare_time_min in exact_dashboard.folders), never unplanned_time.\n"
        "- 'average spare time per folder' or 'spare time for each folder': use exact_dashboard.folders[].spare_time_min / active_nights. "
        "List every folder with its average spare time per active night in minutes.\n"
        "- 'utilized time' / 'utilised time' / 'utilization' with no qualifier: "
        "Utilized Time = Runtime (SNP + GNP) + Loss Time + Downtime. "
        "Waiting time, spare time, and unplanned time are excluded.\n"
        "- 'MALT' / 'Maximum Allowable Loss Time': use max_allowable_loss_time from context. "
        "Always state the formula used: MALT = 240 - P50(Wait) - P85(MOT) - P30(Spare), where MOT = Run Time + Downtime. "
        "MALT is calibrated per plant per complexity using on-time nights only; compare actual loss_time to MALT for exceedance questions.\n"
        "- 'downtime': mechanical stoppage time, not loss time and not waiting time.\n"
        "- Tower questions: always check towers, tower_availability, "
        "tower_downtime_reason_attribution, and editions_by_tower before saying data is unavailable. "
        "For reason-specific tower questions such as web break, use tower_downtime_reason_attribution.\n"
        "- When the user uses a shorthand metric name without qualification, default to the aggregate and "
        "mention if a breakdown by type/folder is also available.\n\n"

        "OPERATING DEFINITIONS:\n"
        "- Wait Time: idle time at the start of the 00:00 window where the press cannot operate because editorial LPR has not been issued. "
        "Wait ends when LPR is issued. If an earlier edition finishes before LPR for the next edition, the PF-to-LPR gap also counts as Wait.\n"
        "- Loss Time: preparation time after editorial release and before printing. Components are Makeready/LPR-to-Press-Start, Changeover/PF-to-Press-Start when physical change is required, and Reflong changeover losses.\n"
        "- Downtime: unplanned stoppages during an active run.\n"
        "- Run Time: net productive print time. For editions already printing before midnight, count only the portion from midnight to Print Finish.\n"
        "- Spare Time: unused capacity inside the reference window after all other components are accounted for. "
        "Formula: Spare Time = 240 - (Wait + Loss + Downtime + Run). It cannot be negative.\n"
        "- Unplanned Time: periods where the folder or tower was not scheduled or available for production.\n"
        "- Utilized Time / Utilisation: Runtime (SNP + GNP) + Loss Time + Downtime. "
        "Do not include Wait Time, Spare Time, or Unplanned Time.\n"
        "- Spare Capacity: (Spare Time / (Total Available Time - Unplanned Time)) * 100.\n\n"

        "PREDICTION & EXTRAPOLATION RULES:\n"
        "When a question asks for a forecast, prediction, projection, or 'what will X be':\n"
        "1. Extract the daily time-series for the metric from exact_dashboard.daily (runtime_min, utilization_pct, loss_time_min, etc.).\n"
        "2. With ≥3 nights of data: compare the first-half average with the second-half average to get direction and rate of change. "
        "Use that trend to project forward.\n"
        "3. With <3 nights: use the simple average as the best estimate.\n"
        "4. Always state: (a) the observed average, (b) the trend direction if detectable, "
        "(c) the extrapolated value, and (d) a one-line caveat ('Based on N nights; assumes current trend continues').\n"
        "5. NEVER refuse a predictive question — always compute and report the best estimate with appropriate caveats.\n"
        "6. If the user asks about a variable not directly in the data (e.g., 'expected copies next week'), "
        "derive it from available data: copies ≈ print_order from complexity_by_code; project using average nightly print_order × nights.\n\n"

        "Schema key:\n"
        "- resource: 'Machine / Folder' display name\n"
        "- utilization_pct / utilization_percentage: (Runtime (SNP + GNP) + Loss Time + Downtime) ÷ total possible capacity (incl. unplanned nights)\n"
        "- active_day_utilization_pct: (Runtime (SNP + GNP) + Loss Time + Downtime) ÷ capacity only on nights the folder was active\n"
        "- runtime_minutes / runtime_min: actual print runtime (all complexity types combined)\n"
        "- lost_time_min / lost_time_minutes: Loss Time = changeover + late-start + reflong ONLY. "
        "WAITING TIME IS NOT INCLUDED IN LOSS TIME.\n"
        "- waiting_time_min / waiting_time_minutes: wait for editions; SEPARATE from loss_time, NEVER add to loss_time\n"
        "- buffer_time_min / buffer_time_minutes: Spare Time — leftover capacity WITHIN an active night only\n"
        "- unplanned_time_min: capacity from nights the folder had NO activity — NOT spare time\n"
        "- loss_components: breakdown into change_over_time, late_start_time, reflong_related_downtime\n"
        "- average_speed_cph: copies per hour (overall)\n"
        "- top_folders_by_loss: folders with most loss on a given day\n"
        "- uv_tower: true if the tower is UV-enabled\n"
        "- downtime_min: mechanical machine stoppage time\n"
        "- load_share_pct: this folder's share of total plant runtime\n"
        "- variability_pct: std deviation of daily runtime % — high = inconsistent usage\n"
        "- complexity_vs_loss: per-complexity-category avg loss share and total lost/downtime\n"
        "- speed.by_category: speed and runtime share for SNP, GNP, SNP Complex, GNP Complex\n"
        "- delayed_pf: Delayed Print Finish — folders that printed past the plant compliance cutoff "
        "(04:00 default; 03:00 Baroda/Manesar; 02:30 Trivandrum). overrun_minutes = minutes past cutoff. "
        "Rows include cutoff_time, estimated_print_finish_time, editions, complexity_codes, runtime/loss/downtime/wait/spare, "
        "and largest_components. Use for any delayed PF, print finish, threshold breach, late finish, or overrun question.\n"
        "- uv_nights / gnp_nights: per-date classification from computed folder complexity data. "
        "A GNP night / UV night is any date where at least one folder ran GNP or GNP Complex editions "
        "(C5-C15). If no GNP/GNP Complex edition ran, it is an SNP night / non-UV night.\n"
        "- complexity_by_code: runtime and print_order by individual code. "
        "C1/C2/C3=SNP, C4=SNP Complex, C5-C8=GNP, C9-C15=GNP Complex.\n"
        "- complexity_downtime_by_code: allocated downtime/loss by exact C1-C15 code. "
        "When a row has multiple C-codes, downtime is allocated proportional to runtime minutes in that row. "
        "Use this for questions like 'which complexity generated the most downtime using C1 to C15'.\n"
        "- downtime_by_reason: stoppages by machine/folder/reason. top_reasons ranked by event count. "
        "count = events; total_minutes = total downtime for that reason.\n"
        "- max_allowable_loss_time: MALT thresholds by plant × complexity, plus night_exceedances. "
        "percentiles_used is fixed at Wait P50, MOT P85, Spare P30.\n"
        "- downtime_by_folder: total downtime incident count and minutes per folder (machine/folder unit), "
        "sorted by incident_count descending. Use for 'frequency of downtime in each folder' or 'which folder has most incidents'.\n"
        "- editions_by_date: unique edition names printed on each date. "
        "Use for date/night edition-list questions.\n"
        "- editions_by_folder: unique edition names printed per folder across the period. "
        "Each entry has folder, editions (list), edition_count. "
        "Use for 'what editions ran on folder X' or 'which folder printed edition Y'.\n"
        "- towers: comprehensive tower totals with runtime/loss/wait/downtime, active_dates, folders, editions, "
        "complexity_codes, downtime_run_count, and loss_time_run_count.\n"
        "- tower_availability: total_towers, total_days, active_towers_by_day, and percent-threshold summaries. "
        "Use this for 'how many towers', 'how many days at least X% towers were utilised', or tower availability questions.\n"
        "- tower_downtime_reason_attribution: folder-level downtime reason events attributed to towers that ran the same plant/machine/folder in the selected period. "
        "Use this for questions like web break frequency by individual tower. State that reason attribution is folder-to-tower attribution when giving reason-specific tower counts.\n"
        "- editions_by_tower: unique edition names printed per tower across the period. "
        "Each entry has tower, editions (list), edition_count. Use for 'what editions ran on tower X'.\n"
        "- exact_dashboard.daily: per-date rows with runtime_min, utilization_pct, loss_time_min, spare_time_min, "
        "night_type, complexity_codes, and editions — use this for trend analysis and extrapolation.\n"
        "- exact_dashboard.folder_days: per-folder per-date rows. Fields: folder, run_date, active_night, "
        "runtime_min, loss_time_min, waiting_time_min, downtime_min, spare_time_min, unplanned_time_min, "
        "utilization_pct, spare_capacity_pct, complexity_codes, editions. "
        "Use for day-by-day breakdown within a folder, or to filter by a specific date.\n"
        "- exact_dashboard.folders: per-folder aggregated rows across the full period. Fields: resource (folder name), "
        "runtime_min, loss_time_min, waiting_time_min, downtime_min, spare_time_min (= total buffer/spare time), "
        "unplanned_time_min, possible_capacity_min, active_capacity_min, active_nights, total_nights, "
        "unplanned_nights, utilization_pct, active_day_utilization_pct, spare_capacity_pct, "
        "complexity_codes, editions. "
        "USE THIS for any per-folder metric totals or averages (e.g. 'average spare time per folder' = spare_time_min / active_nights). "
        "This is the primary source for folder-level summaries.\n"
        "CRITICAL RULES: (1) spare_time = buffer_time ONLY. unplanned_time is NEVER spare. "
        "(2) waiting_time is NOT a loss component — report separately. "
        "(3) Plant spare time = sum of buffer_time across folders, never add unplanned_time. "
        "(4) Unqualified metric names → aggregate totals first.\n\n"
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
        answer = _call_plain_chat_completion(endpoint, api_key, messages).strip()
        if _is_weak_chat_answer(answer):
            fallback_answer = _fallback_answer_from_context(message, context)
            if fallback_answer:
                return {"answer": fallback_answer, "status": "ok", "plan": plan}
        return {"answer": answer, "status": "ok", "plan": plan}
    except Exception as exc:
        error_detail = _sanitize_error_message(exc, api_key)
        print(f"[chat] executor error: {error_detail}", flush=True)
        fallback_answer = _fallback_answer_from_context(message, context)
        if fallback_answer:
            return {
                "answer": fallback_answer,
                "status": "ok",
                "plan": plan,
                "detail": error_detail,
            }
        return {
            "answer": "It is difficult for me to answer with given context.",
            "status": "error",
            "plan": plan,
            "detail": error_detail,
        }


_PLANNER_SCHEMA = """\
DATA SOURCES (use source_key exactly as shown):

exact_dashboard.folders — per-folder aggregated totals across all dates
  Fields: resource (folder display name), runtime_min, loss_time_min, waiting_time_min,
  downtime_min, spare_time_min, unplanned_time_min, possible_capacity_min,
  active_nights, total_nights, utilization_pct, active_day_utilization_pct, spare_capacity_pct

exact_dashboard.folder_days — per-folder per-date rows (use for day-level breakdown or specific date)
  Fields: folder, run_date, active_night, runtime_min, loss_time_min,
  waiting_time_min, downtime_min, spare_time_min, unplanned_time_min,
  utilization_pct, spare_capacity_pct, complexity_codes, editions

exact_dashboard.daily — per-date plant-level totals (use for daily trends or plant-wide day queries)
  Fields: run_date, runtime_min, loss_time_min, waiting_time_min,
  downtime_min, spare_time_min, utilization_pct, night_type

towers — per-tower aggregated totals across all dates
  Fields: tower, runtime_min, downtime_min, lost_time_min, waiting_time_min,
  spare_time_min, active_nights, utilization_pct, downtime_run_count, uv_tower,
  folders, editions, complexity_codes

tower_days — per-tower per-date rows
  Fields: tower, run_date, runtime_min, downtime_min, loss_time_min,
  waiting_time_min, spare_time_min, editions

downtime_by_reason — downtime events by reason and folder
  Fields: reason, machine, folder, count (event count), total_minutes

downtime_by_folder — total incident counts per folder
  Fields: folder, incident_count, total_minutes

delayed_pf — folders with print finish past compliance cutoff (04:00 / 03:00 / 02:30)
  Fields: folder, run_date, overrun_minutes, cutoff_time, estimated_print_finish_time, editions

editions_by_folder — unique editions printed per folder across the period
  Fields: folder, editions (list of names), edition_count

editions_by_tower — unique editions printed per tower across the period
  Fields: tower, editions (list of names), edition_count

complexity_by_code — runtime by individual C1-C15 complexity code
  Fields: code, type (SNP/GNP/SNP Complex/GNP Complex), runtime_min, is_complex

max_allowable_loss_time — MALT thresholds by plant and complexity
  Fields in rows: plant, complexity, malt_min; in night_exceedances: date, folder, loss_min, malt_min

tower_downtime_reason_attribution — downtime reasons attributed to towers
  Fields in by_tower_reason: tower, reason, event_count, total_minutes

COMPUTATION NOTES:
- "average [metric] per folder" → exact_dashboard.folders; divide total_field by active_nights
- loss_time = changeover + late_start + reflong (NEVER includes waiting_time)
- spare_time = spare_time_min (buffer time only, NOT unplanned_time_min)
- SNP: C1-C3 | SNP Complex: C4 | GNP: C5-C8 | GNP Complex: C9-C15
- downtime incident frequency per folder → downtime_by_folder (incident_count)
- downtime by reason per tower → tower_downtime_reason_attribution
- editions on a tower/folder → editions_by_tower / editions_by_folder
- delayed print finish / overrun → delayed_pf
"""


def _call_planner(message: str, endpoint: str, api_key: str) -> dict[str, Any]:
    system = (
        "You are a data planning agent for a print plant production analytics dashboard. "
        "Given a user question, output ONLY a JSON object (no prose, no markdown) describing how to answer it.\n\n"
        f"Available data sources:\n{_PLANNER_SCHEMA}\n"
        "Output JSON with exactly these fields:\n"
        '{\n'
        '  "intent": "average|total|breakdown|comparison|trend|prediction|ranking|lookup|list|count",\n'
        '  "primary_source": "<source_key from the list above>",\n'
        '  "secondary_sources": ["<optional additional source_keys>"],\n'
        '  "metrics": ["<field names to extract, e.g. spare_time_min, active_nights>"],\n'
        '  "computation": "<plain English: what to compute, e.g. spare_time_min / active_nights for each folder>",\n'
        '  "filters": {"<field>": "<value if any filter applies, else omit>"},\n'
        '  "group_by": "folder|tower|date|plant|reason|none",\n'
        '  "output_format": "table|single_value|list|ranked_list|comparison|trend_chart_description"\n'
        "}"
    )
    messages: list[dict[str, str]] = [
        {"role": "system", "content": system},
        {"role": "user", "content": _clean_text(message)},
    ]
    raw = _call_chat_completion(endpoint, api_key, messages)
    raw = raw.strip()
    if raw.startswith("```"):
        raw = re.sub(r"^```[a-z]*\n?", "", raw).rstrip("`").strip()
    return json.loads(raw)


def _build_chat_context(
    intelligence: dict[str, Any],
    tower_details: list[dict[str, Any]],
    summary: dict[str, Any] | None = None,
    daily_rows: list[dict[str, Any]] | None = None,
    details: list[dict[str, Any]] | None = None,
    downtime_reasons: list[dict[str, Any]] | None = None,
    question: str = "",
) -> dict[str, Any]:
    exact_dashboard = _build_exact_dashboard_context(
        summary=summary or {},
        daily_rows=daily_rows or [],
        folder_rows=details or [],
        question=question,
    )
    sections = intelligence.get("sections") or {}
    folder_util = sections.get("folder_utilization") or {}
    loss_time_sec = sections.get("loss_time") or {}
    complexity = sections.get("complexity_speed") or {}
    malt = sections.get("max_allowable_loss_time") or {}

    # Aggregate tower metrics across all dates
    production_days = _number((intelligence.get("scope") or {}).get("production_days"))
    if production_days <= 0:
        production_days = _number((exact_dashboard.get("scope") or {}).get("production_days"))
    tower_buckets: dict[str, dict] = {}
    for row in tower_details:
        tower = _display_resource_name(row.get("tower") or "")
        if not tower:
            continue
        machine, tower_name = _split_machine_folder(_clean_text(row.get("tower")))
        _, folder_name = _split_machine_folder(_clean_text(row.get("folder")))
        bucket = tower_buckets.setdefault(tower, {
            "machine": machine,
            "tower_name": tower_name,
            "runtime": 0.0, "available": 0.0, "buffer": 0.0,
            "downtime": 0.0, "waiting_time": 0.0,
            "change_over_time": 0.0, "late_start_time": 0.0,
            "reflong_related_downtime": 0.0, "dates": set(),
            "folders": set(), "plants": set(), "rows": [],
            "downtime_run_count": 0, "loss_time_run_count": 0, "waiting_time_run_count": 0,
            "uv_tower": False,
        })
        bucket["rows"].append(row)
        bucket["runtime"] += _number(row.get("runtime"))
        bucket["available"] += _number(row.get("available_capacity"))
        bucket["buffer"] += _number(row.get("buffer_time"))
        bucket["downtime"] += _number(row.get("downtime"))
        bucket["waiting_time"] += _number(row.get("waiting_time"))
        bucket["change_over_time"] += _number(row.get("change_over_time"))
        bucket["late_start_time"] += _number(row.get("late_start_time"))
        bucket["reflong_related_downtime"] += _number(row.get("reflong_related_downtime"))
        if _number(row.get("downtime")) > 0:
            bucket["downtime_run_count"] += 1
        if _loss_time_minutes(row) > 0:
            bucket["loss_time_run_count"] += 1
        if _number(row.get("waiting_time")) > 0:
            bucket["waiting_time_run_count"] += 1
        if row.get("uv_tower"):
            bucket["uv_tower"] = True
        plant = _clean_text(row.get("plant_name"))
        if plant:
            bucket["plants"].add(plant)
        if folder_name:
            bucket["folders"].add(_display_resource_name(row.get("folder")))
        date = _clean_text(row.get("run_date"))
        if date:
            bucket["dates"].add(date)

    tower_rows = []
    for t, v in tower_buckets.items():
        active_nights = len(v["dates"])
        unplanned_nights = max(int(production_days) - active_nights, 0) if production_days > 0 else 0
        unplanned_capacity_min = unplanned_nights * CAPACITY_MINUTES_PER_FOLDER_DAY
        non_wait_lost_time = v["change_over_time"] + v["late_start_time"] + v["reflong_related_downtime"]
        utilized_time = v["runtime"] + v["downtime"] + non_wait_lost_time
        runtime_segments = _runtime_segments_for_rows(v["rows"])
        tower_rows.append({
            "tower": t,
            "machine": v["machine"],
            "tower_name": v["tower_name"],
            "plants": sorted(v["plants"]),
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
            "active_dates": sorted(v["dates"]),
            "folders": sorted(v["folders"]),
            "editions": _editions_for_rows(v["rows"]),
            "complexity_codes": _complexity_codes_for_segments(runtime_segments),
            "complexity_categories": _complexity_categories_for_segments(runtime_segments),
            "runtime_segments": runtime_segments,
            "downtime_run_count": v["downtime_run_count"],
            "loss_time_run_count": v["loss_time_run_count"],
            "waiting_time_run_count": v["waiting_time_run_count"],
            "utilization_pct": _percentage(utilized_time, v["available"]),
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
    tower_day_rows = _tower_day_context_rows(tower_details)
    tower_downtime_runs = [row for row in tower_day_rows if _number(row.get("downtime_min")) > 0]
    tower_availability = _build_tower_availability_summary(tower_rows, tower_day_rows, exact_dashboard)

    delayed_pf_rows = _build_delayed_pf_rows(details or [])

    # GNP/UV night classification: any folder with GNP/GNP Complex runtime makes the date a GNP/UV night.
    uv_nights = _build_gnp_night_classification(
        details or [],
        _sorted_unique(row.get("run_date") for row in details or []),
    )

    # Complexity by individual code: allocate row downtime/loss to C-codes by runtime share.
    complexity_downtime_by_code = _complexity_downtime_by_code(details or [])
    complexity_by_code = sorted(
        complexity_downtime_by_code,
        key=lambda row: _complexity_code_sort_key(row.get("code")),
    )

    # Downtime by reason: aggregate from raw downtime_reasons list
    reason_summary: dict[str, dict[str, Any]] = {}
    for rec in (downtime_reasons or []):
        reason = _clean_text(rec.get("reason"))
        if not reason:
            continue
        entry = reason_summary.setdefault(reason, {
            "reason": reason, "count": 0, "total_minutes": 0.0, "machines": [],
        })
        entry["count"] += int(rec.get("count", 0))
        entry["total_minutes"] += _number(rec.get("total_minutes"))
        machine_id = f"{_clean_text(rec.get('machine'))}/{_clean_text(rec.get('folder'))}"
        if machine_id not in entry["machines"]:
            entry["machines"].append(machine_id)

    top_reasons = sorted(
        [
            {
                "reason": e["reason"],
                "count": e["count"],
                "total_minutes": _clean_number(e["total_minutes"]),
                "affected_machine_folders": len(e["machines"]),
            }
            for e in reason_summary.values()
        ],
        key=lambda r: -r["count"],
    )[:25]

    downtime_by_reason = {
        "top_reasons": top_reasons,
        "by_machine_folder": (downtime_reasons or [])[:500],
    }

    # Downtime incidents per folder: sum all reason counts per machine/folder unit
    folder_incident_map: dict[str, dict[str, Any]] = {}
    for rec in (downtime_reasons or []):
        machine = _clean_text(rec.get("machine"))
        folder_name = _clean_text(rec.get("folder"))
        if not machine or not folder_name:
            continue
        key = _display_resource_name(f"{machine}\n{folder_name}")
        entry = folder_incident_map.setdefault(key, {"folder": key, "incident_count": 0, "total_minutes": 0.0})
        entry["incident_count"] += int(rec.get("count", 0))
        entry["total_minutes"] += _number(rec.get("total_minutes"))

    downtime_by_folder = sorted(
        [
            {
                "folder": e["folder"],
                "incident_count": e["incident_count"],
                "total_minutes": _clean_number(e["total_minutes"]),
            }
            for e in folder_incident_map.values()
        ],
        key=lambda r: -r["incident_count"],
    )
    tower_reason_attribution = _build_tower_downtime_reason_attribution(tower_details, downtime_reasons or [])

    # Editions by folder: unique edition names printed per folder across all dates
    folder_editions_map: dict[str, set[str]] = {}
    for row in (details or []):
        folder_key = _display_resource_name(row.get("folder"))
        if not folder_key:
            continue
        for edition in (row.get("editions") or []):
            ed_text = _clean_text(edition)
            if ed_text:
                folder_editions_map.setdefault(folder_key, set()).add(ed_text)

    editions_by_folder = sorted(
        [
            {"folder": folder, "editions": sorted(eds), "edition_count": len(eds)}
            for folder, eds in folder_editions_map.items()
            if eds
        ],
        key=lambda r: r["folder"],
    )

    date_editions_map: dict[str, set[str]] = {}
    for row in (details or []):
        run_date = _clean_text(row.get("run_date"))
        if not run_date:
            continue
        for edition in (row.get("editions") or []):
            ed_text = _clean_text(edition)
            if ed_text:
                date_editions_map.setdefault(run_date, set()).add(ed_text)

    editions_by_date = sorted(
        [
            {"run_date": run_date, "editions": sorted(editions), "edition_count": len(editions)}
            for run_date, editions in date_editions_map.items()
        ],
        key=lambda row: row["run_date"],
    )

    # Editions by tower: unique edition names printed per tower across all dates
    tower_editions_agg: dict[str, set[str]] = {}
    for row in tower_details:
        tower = _display_resource_name(row.get("tower") or "")
        if not tower:
            continue
        for edition in (row.get("editions") or []):
            ed_text = _clean_text(edition)
            if ed_text:
                tower_editions_agg.setdefault(tower, set()).add(ed_text)

    editions_by_tower = sorted(
        [
            {"tower": tower, "editions": sorted(eds), "edition_count": len(eds)}
            for tower, eds in tower_editions_agg.items()
            if eds
        ],
        key=lambda r: r["tower"],
    )

    return {
        "scope": intelligence.get("scope") or {},
        "summary": intelligence.get("summary") or {},
        "exact_dashboard": exact_dashboard,
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
        "complexity_by_code": complexity_by_code,
        "complexity_downtime_by_code": complexity_downtime_by_code,
        "loss_time": {
            "dominant_driver": loss_time_sec.get("dominant_driver"),
            "driver_totals": loss_time_sec.get("driver_totals"),
            "top_loss_days": (loss_time_sec.get("top_loss_days") or [])[:6],
            "low_loss_days": (loss_time_sec.get("low_loss_days") or [])[:4],
            "all_days": all_days,
        },
        "max_allowable_loss_time": {
            "formula": malt.get("formula"),
            "percentile_policy": malt.get("percentile_policy"),
            "rows": (malt.get("rows") or [])[:200],
            "night_exceedances": (malt.get("night_exceedances") or [])[:500],
        },
        "towers": tower_rows,
        "tower_availability": tower_availability,
        "tower_days": tower_day_rows[:1500],
        "tower_downtime_runs": tower_downtime_runs[:1000],
        "tower_downtime_reason_attribution": tower_reason_attribution,
        "uv_towers": uv_towers,
        "non_uv_towers": non_uv_towers,
        "delayed_pf": delayed_pf_rows[:500],
        "uv_nights": uv_nights,
        "downtime_by_reason": downtime_by_reason,
        "downtime_by_folder": downtime_by_folder,
        "editions_by_date": editions_by_date,
        "editions_by_folder": editions_by_folder,
        "editions_by_tower": editions_by_tower,
    }


def _compact_chat_context_for_llm(context: dict[str, Any]) -> dict[str, Any]:
    exact = context.get("exact_dashboard") or {}
    downtime_by_reason = context.get("downtime_by_reason") or {}
    tower_attribution = context.get("tower_downtime_reason_attribution") or {}
    malt = context.get("max_allowable_loss_time") or {}
    exact_malt = exact.get("max_allowable_loss_time") or {}

    return {
        "scope": context.get("scope") or {},
        "summary": context.get("summary") or {},
        "exact_dashboard": {
            "source": exact.get("source"),
            "scope": exact.get("scope") or {},
            "summary": exact.get("summary") or {},
            "daily": _compact_rows(exact.get("daily") or [], limit=370),
            "folders": _compact_rows(exact.get("folders") or [], limit=200),
            "folder_days": _compact_rows(exact.get("folder_days") or [], limit=150),
            "complexity_downtime_by_code": _compact_rows(exact.get("complexity_downtime_by_code") or [], limit=30),
            "night_classification": exact.get("night_classification") or {},
            "delayed_pf": _compact_rows(exact.get("delayed_pf") or [], limit=120),
            "max_allowable_loss_time": {
                "formula": exact_malt.get("formula"),
                "percentile_policy": exact_malt.get("percentile_policy"),
                "rows": _compact_rows(exact_malt.get("rows") or [], limit=120),
                "night_exceedances": _compact_rows(exact_malt.get("night_exceedances") or [], limit=120),
            },
        },
        "folders": _compact_rows(context.get("folders") or [], limit=200),
        "unused_folders": context.get("unused_folders") or [],
        "speed": {
            "overall": (context.get("speed") or {}).get("overall") or {},
            "by_category": _compact_rows(((context.get("speed") or {}).get("by_category") or []), limit=20),
            "by_folder": _compact_rows(((context.get("speed") or {}).get("by_folder") or []), limit=200),
            "by_machine": _compact_rows(((context.get("speed") or {}).get("by_machine") or []), limit=80),
            "fastest": _compact_rows(((context.get("speed") or {}).get("fastest") or []), limit=10),
            "slowest": _compact_rows(((context.get("speed") or {}).get("slowest") or []), limit=10),
            "highest_complexity_share": _compact_rows(
                ((context.get("speed") or {}).get("highest_complexity_share") or []),
                limit=10,
            ),
        },
        "complexity_vs_loss": _compact_rows(context.get("complexity_vs_loss") or [], limit=30),
        "complexity_by_code": _compact_rows(context.get("complexity_by_code") or [], limit=30),
        "complexity_downtime_by_code": _compact_rows(context.get("complexity_downtime_by_code") or [], limit=30),
        "loss_time": {
            "dominant_driver": (context.get("loss_time") or {}).get("dominant_driver"),
            "driver_totals": (context.get("loss_time") or {}).get("driver_totals"),
            "top_loss_days": _compact_rows(((context.get("loss_time") or {}).get("top_loss_days") or []), limit=20),
            "low_loss_days": _compact_rows(((context.get("loss_time") or {}).get("low_loss_days") or []), limit=20),
            "all_days": _compact_rows(((context.get("loss_time") or {}).get("all_days") or []), limit=370),
        },
        "max_allowable_loss_time": {
            "formula": malt.get("formula"),
            "percentile_policy": malt.get("percentile_policy"),
            "rows": _compact_rows(malt.get("rows") or [], limit=120),
            "night_exceedances": _compact_rows(malt.get("night_exceedances") or [], limit=120),
        },
        "towers": _compact_rows(context.get("towers") or [], limit=200),
        "tower_availability": context.get("tower_availability") or {},
        "tower_downtime_reason_attribution": {
            "attribution_note": tower_attribution.get("attribution_note"),
            "by_tower": _compact_rows(tower_attribution.get("by_tower") or [], limit=100),
            "by_tower_reason": _compact_rows(tower_attribution.get("by_tower_reason") or [], limit=160),
        },
        "uv_towers": _compact_rows(context.get("uv_towers") or [], limit=100),
        "non_uv_towers": _compact_rows(context.get("non_uv_towers") or [], limit=100),
        "delayed_pf": _compact_rows(context.get("delayed_pf") or [], limit=120),
        "uv_nights": context.get("uv_nights") or {},
        "downtime_by_reason": {
            "top_reasons": _compact_rows(downtime_by_reason.get("top_reasons") or [], limit=50),
        },
        "downtime_by_folder": _compact_rows(context.get("downtime_by_folder") or [], limit=120),
        "editions_by_date": _compact_rows(context.get("editions_by_date") or [], limit=370),
        "editions_by_folder": _compact_rows(context.get("editions_by_folder") or [], limit=200),
        "editions_by_tower": _compact_rows(context.get("editions_by_tower") or [], limit=200),
    }


def _compact_rows(rows: list[dict[str, Any]], limit: int) -> list[dict[str, Any]]:
    return [_compact_row(row) for row in rows[:limit] if isinstance(row, dict)]


def _compact_row(row: dict[str, Any]) -> dict[str, Any]:
    omitted_keys = {
        "runtime_segments",
        "rows",
        "raw_rows",
        "folder_days",
        "tower_days",
        "tower_downtime_runs",
    }
    compact: dict[str, Any] = {}
    for key, value in row.items():
        if key in omitted_keys:
            continue
        if isinstance(value, list):
            compact[key] = [_compact_list_value(item) for item in value[:30]]
            if len(value) > 30:
                compact[f"{key}_omitted_count"] = len(value) - 30
        elif isinstance(value, dict):
            compact[key] = {
                child_key: _compact_list_value(child_value)
                for child_key, child_value in value.items()
                if child_key not in omitted_keys
            }
        else:
            compact[key] = value
    return compact


def _compact_list_value(value: Any) -> Any:
    if isinstance(value, dict):
        return _compact_row(value)
    if isinstance(value, list):
        return value[:20]
    return value


def _build_exact_dashboard_context(
    summary: dict[str, Any],
    daily_rows: list[dict[str, Any]],
    folder_rows: list[dict[str, Any]],
    question: str,
) -> dict[str, Any]:
    dates = _sorted_unique(
        [
            *[row.get("run_date") for row in daily_rows],
            *[row.get("run_date") for row in folder_rows],
        ]
    )
    folder_keys = _sorted_unique(row.get("folder") for row in folder_rows)
    exact_daily_rows = _exact_daily_rows(daily_rows, folder_rows, dates, folder_keys)
    exact_folder_rows = _exact_folder_rows(folder_rows, dates)
    exact_folder_day_rows, folder_day_note = _exact_folder_day_rows(folder_rows, question)
    malt = _build_malt_analysis(folder_rows)
    night_classification = _build_gnp_night_classification(folder_rows, dates)
    delayed_pf_rows = _build_delayed_pf_rows(folder_rows)
    complexity_downtime_by_code = _complexity_downtime_by_code(folder_rows)
    total_available = sum(_number(row.get("available_capacity_min")) for row in exact_daily_rows)
    total_runtime = sum(_number(row.get("runtime_min")) for row in exact_daily_rows)
    total_loss_time = sum(_number(row.get("loss_time_min")) for row in exact_daily_rows)
    total_waiting_time = sum(_number(row.get("waiting_time_min")) for row in exact_daily_rows)
    total_downtime = sum(_number(row.get("downtime_min")) for row in exact_daily_rows)
    total_utilized_time = total_runtime + total_downtime + total_loss_time
    total_spare_time = sum(_number(row.get("spare_time_min")) for row in exact_daily_rows)
    total_unplanned_time = sum(_number(row.get("unplanned_time_min")) for row in exact_daily_rows)
    planned_available = max(total_available - total_unplanned_time, 0)

    return {
        "source": "current filtered dashboard rows",
        "scope": {
            "start_date": dates[0] if dates else "",
            "end_date": dates[-1] if dates else "",
            "production_days": len(dates),
            "folder_count": len(folder_keys),
        },
        "summary": {
            "total_available_capacity_min": _clean_number(total_available or summary.get("total_available_capacity")),
            "total_runtime_min": _clean_number(total_runtime or summary.get("total_runtime")),
            "total_loss_time_min": _clean_number(total_loss_time or summary.get("total_lost_time")),
            "total_waiting_time_min": _clean_number(total_waiting_time),
            "total_downtime_min": _clean_number(total_downtime or summary.get("total_downtime")),
            "total_utilized_time_min": _clean_number(total_utilized_time),
            "total_spare_time_min": _clean_number(total_spare_time or summary.get("total_buffer_time")),
            "total_unplanned_time_min": _clean_number(total_unplanned_time or summary.get("total_idle_time")),
            "average_utilization_pct": _percentage(
                total_utilized_time or (
                    _number(summary.get("total_runtime"))
                    + _number(summary.get("total_downtime"))
                    + _number(summary.get("total_lost_time"))
                ),
                total_available or _number(summary.get("total_available_capacity")),
            ),
            "spare_capacity_pct": _percentage(
                total_spare_time or _number(summary.get("total_buffer_time")),
                planned_available or max(
                    _number(summary.get("total_available_capacity")) - _number(summary.get("total_idle_time")),
                    0,
                ),
            ),
            "unplanned_capacity_pct": _percentage(
                total_unplanned_time or _number(summary.get("total_idle_time")),
                total_available or _number(summary.get("total_available_capacity")),
            ),
            "spare_capacity_formula": "(Spare Time / (Available Time - Unplanned Time)) * 100",
        },
        "daily": exact_daily_rows,
        "folders": exact_folder_rows,
        "folder_days": exact_folder_day_rows,
        "folder_day_note": folder_day_note,
        "complexity_downtime_by_code": complexity_downtime_by_code,
        "night_classification": night_classification,
        "delayed_pf": delayed_pf_rows[:500],
        "max_allowable_loss_time": {
            "formula": malt.get("formula"),
            "percentile_policy": malt.get("percentile_policy"),
            "rows": (malt.get("rows") or [])[:200],
            "night_exceedances": (malt.get("night_exceedances") or [])[:500],
        },
    }


def _try_deterministic_chat_answer(message: str, context: dict[str, Any]) -> str:
    question = _clean_text(message).casefold()
    if not question:
        return ""

    if _is_daily_per_folder_average_metric_question(question):
        return _answer_daily_per_folder_average_metric_question(question, context)

    if _is_daily_average_metric_question(question):
        return _answer_daily_average_metric_question(question, context)

    if _is_tower_downtime_frequency_question(question):
        return _answer_tower_downtime_frequency_question(question, context)

    if _is_tower_count_question(question):
        return _answer_tower_count_question(context)

    if _is_tower_availability_threshold_question(question):
        return _answer_tower_availability_threshold_question(question, context)

    return ""


def _is_weak_chat_answer(answer: str) -> bool:
    text = _clean_text(answer).casefold()
    if not text:
        return True
    weak_phrases = [
        "not available in the current data",
        "difficult for me to answer",
        "difficult to answer",
        "given context",
        "insufficient context",
        "not enough context",
        "cannot answer",
        "can't answer",
    ]
    return any(phrase in text for phrase in weak_phrases)


def _fallback_answer_from_context(message: str, context: dict[str, Any]) -> str:
    question = _clean_text(message).casefold()
    if not question:
        return ""

    deterministic = _try_deterministic_chat_answer(message, context)
    if deterministic:
        return deterministic

    if _asks_complexity_downtime(question):
        return _answer_complexity_downtime_question(context)

    if _asks_malt(question):
        return _answer_malt_question(question, context)

    if _asks_delayed_pf(question):
        return _answer_delayed_pf_question(question, context)

    if _asks_night_classification(question):
        return _answer_night_classification_question(question, context)

    if _asks_editions(question):
        return _answer_editions_question(question, context)

    if _asks_downtime_reason(question):
        return _answer_downtime_reason_question(question, context)

    if _asks_summary_metric(question):
        return _answer_summary_metric_question(question, context)

    return ""


def _asks_complexity_downtime(question: str) -> bool:
    return "complex" in question and any(term in question for term in ["downtime", "down time", "stoppage"])


def _answer_complexity_downtime_question(context: dict[str, Any]) -> str:
    rows = context.get("complexity_downtime_by_code") or []
    rows = sorted(rows, key=lambda row: -_number(row.get("allocated_downtime_min")))[:8]
    if not rows:
        return "Not available in the current data."
    lines = ["Complexity downtime by C-code:"]
    for index, row in enumerate(rows, start=1):
        lines.append(
            f"{index}. {row.get('code')}: {row.get('allocated_downtime_min')} min downtime "
            f"({row.get('downtime_row_count')} rows), runtime {row.get('runtime_min')} min"
        )
    return "\n".join(lines)


def _asks_malt(question: str) -> bool:
    return "malt" in question or "maximum allowable loss" in question or "max allowable loss" in question


def _answer_malt_question(question: str, context: dict[str, Any]) -> str:
    malt = context.get("max_allowable_loss_time") or {}
    formula = (
        malt.get("formula")
        or "MALT = 240 - P50(Wait) - P85(MOT) - P30(Spare), where MOT = Run Time + Downtime."
    )
    rows = malt.get("rows") or []
    exceedances = malt.get("night_exceedances") or []
    wants_exceedance = any(term in question for term in ["exceed", "above", "breach", "greater than", "over malt"])

    if wants_exceedance:
        if not exceedances:
            return f"{formula}\nNo nights exceed MALT in the current data."
        ranked = sorted(exceedances, key=lambda row: -_number(row.get("excess_min")))[:10]
        lines = [formula, "Nights where actual Loss Time exceeds MALT:"]
        for index, row in enumerate(ranked, start=1):
            lines.append(
                f"{index}. {row.get('run_date')} | {row.get('plant')} | {row.get('complexity')}: "
                f"actual loss {row.get('actual_loss_min')} min, MALT {row.get('malt_min')} min, "
                f"excess {row.get('excess_min')} min"
            )
        return "\n".join(lines)

    filtered = _filter_context_rows(rows, question, ["plant", "complexity", "complexity_code"])
    selected = (filtered or rows)[:10]
    if not selected:
        return f"{formula}\nNot available in the current data."
    lines = [formula, "MALT by plant and complexity:"]
    for index, row in enumerate(selected, start=1):
        lines.append(
            f"{index}. {row.get('plant')} | {row.get('complexity')}: MALT {row.get('malt_min')} min "
            f"(Wait P50 {row.get('wait_min')} min, MOT P85 {row.get('mot_min')} min, "
            f"Spare P30 {row.get('spare_min')} min)"
        )
    return "\n".join(lines)


def _asks_delayed_pf(question: str) -> bool:
    return any(
        term in question
        for term in ["delayed pf", "delayed print", "print finish", "pf threshold", "threshold", "overrun", "late finish"]
    )


def _answer_delayed_pf_question(question: str, context: dict[str, Any]) -> str:
    rows = context.get("delayed_pf") or (context.get("exact_dashboard") or {}).get("delayed_pf") or []
    filtered = _filter_context_rows(
        rows,
        question,
        ["run_date", "plant", "machine", "folder_name", "folder", "complexity_codes", "complexity_categories", "editions"],
    )
    selected = sorted(filtered or rows, key=lambda row: -_number(row.get("overrun_minutes")))[:10]
    if not selected:
        return "No delayed print finish rows are present in the current data."

    lines = [f"{len(filtered or rows)} delayed print finish rows found in the current data. Top overruns:"]
    for index, row in enumerate(selected, start=1):
        complexities = ", ".join(_string_list(row.get("complexity_codes"))) or "-"
        editions = ", ".join(_string_list(row.get("editions"))[:3])
        edition_text = f"; editions: {editions}" if editions else ""
        lines.append(
            f"{index}. {row.get('run_date')} | {row.get('plant')} | {row.get('folder')}: "
            f"{row.get('overrun_minutes')} min over {row.get('pf_cutoff_time')} "
            f"(finish {row.get('estimated_print_finish_time')}); {complexities}{edition_text}"
        )
    return "\n".join(lines)


def _asks_night_classification(question: str) -> bool:
    has_night = "night" in question or "date" in question or "day" in question
    has_type = any(term in question for term in ["gnp", "uv", "snp", "non-uv", "non uv"])
    return has_night and has_type


def _answer_night_classification_question(question: str, context: dict[str, Any]) -> str:
    nights = context.get("uv_nights") or (context.get("exact_dashboard") or {}).get("night_classification") or {}
    if not nights:
        return "Not available in the current data."
    wants_gnp = "gnp" in question or "uv" in question
    wants_snp = "snp" in question or "non-uv" in question or "non uv" in question
    if wants_gnp and not wants_snp:
        dates = nights.get("gnp_nights") or nights.get("uv_nights") or []
        label = "GNP/UV"
    elif wants_snp and not wants_gnp:
        dates = nights.get("snp_nights") or nights.get("non_uv_nights") or []
        label = "SNP/non-UV"
    else:
        lines = [
            nights.get("definition")
            or "A GNP/UV night is any date where at least one folder ran GNP or GNP Complex editions.",
            f"GNP/UV nights: {nights.get('gnp_night_count', 0)}",
            f"SNP/non-UV nights: {nights.get('snp_night_count', 0)}",
        ]
        return "\n".join(lines)

    date_text = ", ".join(dates[:15])
    if len(dates) > 15:
        date_text += f" (+{len(dates) - 15} more)"
    return f"{label} nights: {len(dates)}. Dates: {date_text or 'none'}."


def _asks_editions(question: str) -> bool:
    return any(term in question for term in ["edition", "editions", "printed", "ran on", "runs on"])


def _answer_editions_question(question: str, context: dict[str, Any]) -> str:
    if "tower" in question:
        rows = context.get("editions_by_tower") or []
        label = "tower"
        name_key = "tower"
    elif "folder" in question:
        rows = context.get("editions_by_folder") or []
        label = "folder"
        name_key = "folder"
    else:
        rows = context.get("editions_by_date") or []
        label = "date"
        name_key = "run_date"

    filtered = _filter_context_rows(rows, question, [name_key, "editions"])
    selected = (filtered or rows)[:10]
    if not selected:
        return "Not available in the current data."

    lines = [f"Edition list by {label}:"]
    for row in selected:
        editions = _string_list(row.get("editions"))
        edition_text = ", ".join(editions[:12])
        if len(editions) > 12:
            edition_text += f" (+{len(editions) - 12} more)"
        lines.append(f"- {row.get(name_key)}: {edition_text or 'none'}")
    return "\n".join(lines)


def _asks_downtime_reason(question: str) -> bool:
    has_downtime = any(term in question for term in ["downtime", "down time", "stoppage", "web break", "web-break"])
    has_reason = any(term in question for term in ["reason", "cause", "web break", "web-break", "event", "events", "incident"])
    return has_downtime and has_reason


def _answer_downtime_reason_question(question: str, context: dict[str, Any]) -> str:
    if "tower" in question:
        tower_answer = _answer_tower_downtime_frequency_question(question, context)
        if tower_answer:
            return tower_answer

    downtime_by_reason = context.get("downtime_by_reason") or {}
    rows = downtime_by_reason.get("top_reasons") or []
    if "web break" in question or "web-break" in question or ("web" in question and "break" in question):
        rows = [
            row for row in rows
            if "web" in _clean_text(row.get("reason")).casefold()
            and "break" in _clean_text(row.get("reason")).casefold()
        ]
    selected = rows[:10]
    if not selected:
        return "Not available in the current data."
    lines = ["Downtime reasons by event count:"]
    for index, row in enumerate(selected, start=1):
        lines.append(
            f"{index}. {row.get('reason')}: {row.get('count')} events, "
            f"{row.get('total_minutes')} min, {row.get('affected_machine_folders')} machine/folders"
        )
    return "\n".join(lines)


def _asks_summary_metric(question: str) -> bool:
    metric_terms = [
        "runtime", "run time", "downtime", "down time", "loss time", "lost time", "wait time",
        "waiting time", "spare", "unplanned", "utilized", "utilised", "utilization", "utilisation", "available", "mot",
    ]
    return any(term in question for term in metric_terms) and any(
        term in question for term in ["total", "overall", "how much", "show", "what is", "what was"]
    )


def _answer_summary_metric_question(question: str, context: dict[str, Any]) -> str:
    summary = ((context.get("exact_dashboard") or {}).get("summary") or {})
    if not summary:
        return ""

    parts = []
    if "mot" in question:
        mot = _number(summary.get("total_runtime_min")) + _number(summary.get("total_downtime_min"))
        parts.append(f"MOT (Run + Down): {_clean_number(mot)} min")
    if "runtime" in question or "run time" in question:
        parts.append(f"Run Time: {summary.get('total_runtime_min')} min")
    if "downtime" in question or "down time" in question:
        parts.append(f"Downtime: {summary.get('total_downtime_min')} min")
    if "loss time" in question or "lost time" in question:
        parts.append(f"Loss Time: {summary.get('total_loss_time_min')} min")
    if "wait time" in question or "waiting time" in question or re.search(r"\bwait\b", question):
        parts.append(f"Wait Time: {summary.get('total_waiting_time_min')} min")
    if "spare capacity" in question:
        parts.append(f"Spare Capacity: {summary.get('spare_capacity_pct')}%")
    elif "spare" in question:
        parts.append(f"Spare Time: {summary.get('total_spare_time_min')} min")
    if "unplanned" in question:
        parts.append(f"Unplanned Time: {summary.get('total_unplanned_time_min')} min")
    if "utilized time" in question or "utilised time" in question:
        utilized_time = (
            _number(summary.get("total_utilized_time_min"))
            or _number(summary.get("total_runtime_min"))
            + _number(summary.get("total_loss_time_min"))
            + _number(summary.get("total_downtime_min"))
        )
        parts.append(
            f"Utilized Time: {_clean_number(utilized_time)} min "
            "(Runtime (SNP + GNP) + Loss Time + Downtime)"
        )
    elif "utilization" in question or "utilisation" in question:
        parts.append(f"Utilisation: {summary.get('average_utilization_pct')}%")
    if "available" in question:
        parts.append(f"Available Capacity: {summary.get('total_available_capacity_min')} min")

    if not parts:
        return ""
    return " | ".join(parts)


def _filter_context_rows(rows: list[dict[str, Any]], question: str, fields: list[str]) -> list[dict[str, Any]]:
    query = _clean_text(question).casefold()
    filtered = []
    for row in rows:
        for field in fields:
            values = row.get(field)
            if not isinstance(values, list):
                values = [values]
            for value in values:
                text = _clean_text(value).casefold()
                if text and text in query:
                    filtered.append(row)
                    break
            else:
                continue
            break
    return filtered


def _is_daily_average_metric_question(question: str) -> bool:
    has_average = any(term in question for term in ["average", "avg", "mean", "per day", "any given day", "given day"])
    has_day_scope = any(term in question for term in ["day", "daily", "night", "per date", "given day"])
    has_metric = _daily_average_metric_spec(question) is not None
    excludes_folder_scope = any(term in question for term in ["per folder", "each folder", "folder wise", "folderwise"])
    return has_average and has_day_scope and has_metric and not excludes_folder_scope


def _is_daily_per_folder_average_metric_question(question: str) -> bool:
    has_average = any(term in question for term in ["average", "avg", "mean"])
    has_day_scope = any(term in question for term in ["per day", "daily", "day", "night", "given day"])
    has_folder_scope = any(
        term in question
        for term in ["per folder", "each folder", "folder wise", "folderwise", "per tower", "each tower"]
    )
    return has_average and has_day_scope and has_folder_scope and _daily_average_metric_spec(question) is not None


def _daily_average_metric_spec(question: str) -> dict[str, Any] | None:
    if "spare capacity" in question:
        return {
            "label": "Spare Capacity",
            "daily_key": "spare_capacity_pct",
            "summary_key": "spare_capacity_pct",
            "unit": "%",
            "average_pct": True,
        }
    if "mot" in question:
        return {
            "label": "MOT (Run + Down)",
            "daily_keys": ["runtime_min", "downtime_min"],
            "summary_keys": ["total_runtime_min", "total_downtime_min"],
            "unit": "min",
        }
    if "runtime" in question or "run time" in question:
        return {"label": "Run Time", "daily_key": "runtime_min", "summary_key": "total_runtime_min", "unit": "min"}
    if "downtime" in question or "down time" in question:
        return {"label": "Downtime", "daily_key": "downtime_min", "summary_key": "total_downtime_min", "unit": "min"}
    if "loss time" in question or "lost time" in question:
        return {"label": "Loss Time", "daily_key": "loss_time_min", "summary_key": "total_loss_time_min", "unit": "min"}
    if "wait time" in question or "waiting time" in question or re.search(r"\bwait\b", question):
        return {"label": "Wait Time", "daily_key": "waiting_time_min", "summary_key": "total_waiting_time_min", "unit": "min"}
    if "spare" in question:
        return {"label": "Spare Time", "daily_key": "spare_time_min", "summary_key": "total_spare_time_min", "unit": "min"}
    if "utilized time" in question or "utilised time" in question:
        return {
            "label": "Utilized Time",
            "daily_keys": ["runtime_min", "loss_time_min", "downtime_min"],
            "summary_keys": ["total_runtime_min", "total_loss_time_min", "total_downtime_min"],
            "unit": "min",
        }
    if "unplanned" in question:
        return {
            "label": "Unplanned Time",
            "daily_key": "unplanned_time_min",
            "summary_key": "total_unplanned_time_min",
            "unit": "min",
        }
    if "available" in question:
        return {
            "label": "Available Capacity",
            "daily_key": "available_capacity_min",
            "summary_key": "total_available_capacity_min",
            "unit": "min",
        }
    if "utilization" in question or "utilisation" in question:
        return {
            "label": "Utilisation",
            "daily_key": "utilization_pct",
            "summary_key": "average_utilization_pct",
            "unit": "%",
            "average_pct": True,
        }
    return None


def _answer_daily_average_metric_question(question: str, context: dict[str, Any]) -> str:
    exact_dashboard = context.get("exact_dashboard") or {}
    summary = exact_dashboard.get("summary") or {}
    daily_rows = exact_dashboard.get("daily") or []
    spec = _daily_average_metric_spec(question)
    if not spec:
        return ""

    days = len([
        row for row in daily_rows
        if _clean_text(row.get("run_date"))
    ])
    if days <= 0:
        days = int(_number((exact_dashboard.get("scope") or {}).get("production_days")))
    if days <= 0:
        return "Not available in the current data."

    if spec.get("average_pct"):
        values = [_number(row.get(spec["daily_key"])) for row in daily_rows if row.get(spec["daily_key"]) is not None]
        if values:
            average_value = sum(values) / len(values)
            return (
                f"Average {spec['label']} per day: {_clean_number(average_value)}{spec['unit']} "
                f"(average of {len(values)} daily values)."
            )
        summary_value = _number(summary.get(spec.get("summary_key")))
        if summary_value:
            return f"Average {spec['label']} per day: {_clean_number(summary_value)}{spec['unit']}."
        return "Not available in the current data."

    daily_keys = spec.get("daily_keys") or [spec.get("daily_key")]
    total = 0.0
    daily_has_values = False
    for row in daily_rows:
        row_total = sum(_number(row.get(key)) for key in daily_keys if key)
        if row_total or any(row.get(key) is not None for key in daily_keys if key):
            daily_has_values = True
        total += row_total

    if not daily_has_values:
        summary_keys = spec.get("summary_keys") or [spec.get("summary_key")]
        total = sum(_number(summary.get(key)) for key in summary_keys if key)

    average_value = total / days if days > 0 else 0.0
    return (
        f"Average {spec['label']} per day: {_clean_number(average_value)} {spec['unit']} "
        f"({spec['label']} total {_clean_number(total)} {spec['unit']} / {days} production days)."
    )


def _answer_daily_per_folder_average_metric_question(question: str, context: dict[str, Any]) -> str:
    exact_dashboard = context.get("exact_dashboard") or {}
    daily_rows = exact_dashboard.get("daily") or []
    folder_rows = exact_dashboard.get("folders") or []
    summary = exact_dashboard.get("summary") or {}
    spec = _daily_average_metric_spec(question)
    if not spec:
        return ""

    days = len([
        row for row in daily_rows
        if _clean_text(row.get("run_date"))
    ])
    if days <= 0:
        days = int(_number((exact_dashboard.get("scope") or {}).get("production_days")))

    folder_count = int(_number((exact_dashboard.get("scope") or {}).get("folder_count")))
    if folder_count <= 0:
        folder_count = len([
            row for row in folder_rows
            if _clean_text(row.get("resource") or row.get("folder"))
        ])
    if folder_count <= 0:
        folder_count = int(max(
            [_number(row.get("capacity_folders")) for row in daily_rows if row.get("capacity_folders") is not None]
            or [0]
        ))

    if days <= 0 or folder_count <= 0:
        return "Not available in the current data."

    if spec.get("average_pct"):
        return _answer_daily_average_metric_question(question, context)

    daily_keys = spec.get("daily_keys") or [spec.get("daily_key")]
    total = 0.0
    daily_has_values = False
    for row in daily_rows:
        row_total = sum(_number(row.get(key)) for key in daily_keys if key)
        if row_total or any(row.get(key) is not None for key in daily_keys if key):
            daily_has_values = True
        total += row_total

    if not daily_has_values:
        summary_keys = spec.get("summary_keys") or [spec.get("summary_key")]
        total = sum(_number(summary.get(key)) for key in summary_keys if key)

    denominator = days * folder_count
    average_value = total / denominator if denominator > 0 else 0.0
    folder_label = "towers" if "tower" in question else "folders"
    return (
        f"Average {spec['label']} per day per {folder_label[:-1]}: {_clean_number(average_value)} {spec['unit']} "
        f"({spec['label']} total {_clean_number(total)} {spec['unit']} / "
        f"{days} production days / {folder_count} {folder_label})."
    )


def _is_tower_downtime_frequency_question(question: str) -> bool:
    has_tower = any(term in question for term in ["tower", "towers"])
    has_downtime = any(term in question for term in ["downtime", "down time", "web break", "web-break", "break"])
    has_frequency = any(term in question for term in ["most often", "appear", "frequency", "frequent", "count", "instances", "events"])
    return has_tower and has_downtime and has_frequency


def _is_tower_count_question(question: str) -> bool:
    has_tower = "tower" in question or "towers" in question
    asks_count = any(term in question for term in ["how many", "total", "count", "number of"])
    excludes_threshold = "%" not in question and "percent" not in question and "utilised" not in question and "utilized" not in question
    return has_tower and asks_count and excludes_threshold


def _answer_tower_count_question(context: dict[str, Any]) -> str:
    availability = context.get("tower_availability") or {}
    total_towers = int(_number(availability.get("total_towers")))
    tower_names = availability.get("tower_names") or []
    total_days = int(_number(availability.get("total_days")))
    if total_towers <= 0:
        return "Not available in the current data."
    names = ", ".join(tower_names[:12])
    suffix = f" Towers: {names}" if names else ""
    if len(tower_names) > 12:
        suffix += f" (+{len(tower_names) - 12} more)"
    return f"There are {total_towers} total towers in the current selection across {total_days} production days.{suffix}"


def _is_tower_availability_threshold_question(question: str) -> bool:
    has_tower = "tower" in question or "towers" in question
    has_threshold = "%" in question or "percent" in question
    has_days = "day" in question or "days" in question or "night" in question or "nights" in question
    has_utilized = any(term in question for term in ["utilised", "utilized", "active", "operational", "used", "running"])
    return has_tower and has_threshold and has_days and has_utilized


def _answer_tower_availability_threshold_question(question: str, context: dict[str, Any]) -> str:
    availability = context.get("tower_availability") or {}
    total_towers = int(_number(availability.get("total_towers")))
    total_days = int(_number(availability.get("total_days")))
    if total_towers <= 0 or total_days <= 0:
        return "Not available in the current data."

    threshold_pct = _extract_percentage(question) or 70
    required_towers = int((threshold_pct / 100.0) * total_towers)
    if required_towers < (threshold_pct / 100.0) * total_towers:
        required_towers += 1

    matching = [
        row for row in (availability.get("active_towers_by_day") or [])
        if _number(row.get("active_towers")) >= required_towers
    ]
    dates = [_clean_text(row.get("run_date")) for row in matching if _clean_text(row.get("run_date"))]
    date_text = f" Dates: {', '.join(dates[:10])}." if dates else ""
    if len(dates) > 10:
        date_text = f" Dates: {', '.join(dates[:10])} (+{len(dates) - 10} more)."
    return (
        f"{len(matching)} of {total_days} days had at least {threshold_pct:g}% towers utilised "
        f"({required_towers} of {total_towers} towers).{date_text}"
    )


def _extract_percentage(question: str) -> float:
    match = re.search(r"(\d+(?:\.\d+)?)\\s*%", question)
    if match:
        return float(match.group(1))
    match = re.search(r"(\d+(?:\.\d+)?)\\s*percent", question)
    if match:
        return float(match.group(1))
    return 0.0


def _answer_tower_downtime_frequency_question(question: str, context: dict[str, Any]) -> str:
    attribution = context.get("tower_downtime_reason_attribution") or {}
    reason_rows = attribution.get("by_tower_reason") or []
    tower_rows = context.get("towers") or []
    downtime_runs = context.get("tower_downtime_runs") or []

    wants_web_break = "web break" in question or "web-break" in question or ("web" in question and "break" in question)
    matching_reason_rows = reason_rows
    if wants_web_break:
        matching_reason_rows = [
            row for row in reason_rows
            if "web" in _clean_text(row.get("reason")).casefold()
            and "break" in _clean_text(row.get("reason")).casefold()
        ]

    if matching_reason_rows:
        ranked = sorted(
            matching_reason_rows,
            key=lambda row: (
                -_number(row.get("attributed_event_count")),
                -_number(row.get("matching_tower_run_count")),
                _clean_text(row.get("tower")),
            ),
        )[:5]
        reason_label = "web break" if wants_web_break else "downtime reason"
        lines = [
            f"Top individual towers for {reason_label} events:",
        ]
        for index, row in enumerate(ranked, start=1):
            dates = row.get("matching_dates") or []
            date_text = f"; dates: {', '.join(dates[:5])}" if dates else ""
            if len(dates) > 5:
                date_text += f" (+{len(dates) - 5} more)"
            lines.append(
                f"{index}. {row.get('tower')}: {row.get('attributed_event_count')} attributed events, "
                f"{row.get('attributed_minutes')} min, {row.get('matching_tower_run_count')} matching tower runs{date_text}"
            )
        note = attribution.get("attribution_note")
        if note:
            lines.append(f"Note: {note}")
        return "\n".join(lines)

    if downtime_runs:
        grouped: dict[str, dict[str, Any]] = {}
        for row in downtime_runs:
            tower = _clean_text(row.get("tower"))
            if not tower:
                continue
            entry = grouped.setdefault(tower, {"tower": tower, "run_count": 0, "downtime_min": 0.0, "dates": set()})
            entry["run_count"] += 1
            entry["downtime_min"] += _number(row.get("downtime_min"))
            date = _clean_text(row.get("run_date"))
            if date:
                entry["dates"].add(date)

        ranked = sorted(
            grouped.values(),
            key=lambda row: (-row["run_count"], -row["downtime_min"], row["tower"]),
        )[:5]
        if ranked:
            lines = ["Top individual towers by runs with downtime:"]
            for index, row in enumerate(ranked, start=1):
                dates = sorted(row["dates"])
                date_text = f"; dates: {', '.join(dates[:5])}" if dates else ""
                if len(dates) > 5:
                    date_text += f" (+{len(dates) - 5} more)"
                lines.append(
                    f"{index}. {row['tower']}: {row['run_count']} runs, {_clean_number(row['downtime_min'])} min downtime{date_text}"
                )
            return "\n".join(lines)

    ranked_towers = sorted(
        [row for row in tower_rows if _number(row.get("downtime_run_count")) > 0],
        key=lambda row: (-_number(row.get("downtime_run_count")), -_number(row.get("downtime_min")), _clean_text(row.get("tower"))),
    )[:5]
    if ranked_towers:
        lines = ["Top individual towers by downtime-run count:"]
        for index, row in enumerate(ranked_towers, start=1):
            lines.append(
                f"{index}. {row.get('tower')}: {row.get('downtime_run_count')} runs, "
                f"{row.get('downtime_min')} min downtime"
            )
        return "\n".join(lines)

    return "Not available in the current data."


def _exact_daily_rows(
    daily_rows: list[dict[str, Any]],
    folder_rows: list[dict[str, Any]],
    dates: list[str],
    folder_keys: list[str],
) -> list[dict[str, Any]]:
    daily_by_date = {
        _clean_text(row.get("run_date")): row
        for row in daily_rows
        if row.get("run_date")
    }
    details_by_date: dict[str, list[dict[str, Any]]] = {date: [] for date in dates}
    for row in folder_rows:
        run_date = _clean_text(row.get("run_date"))
        if run_date:
            details_by_date.setdefault(run_date, []).append(row)

    rows = []
    for run_date in dates:
        daily = daily_by_date.get(run_date, {})
        details = details_by_date.get(run_date, [])

        if details:
            active_folders = len({
                _clean_text(row.get("folder"))
                for row in details
                if _clean_text(row.get("folder")) and _is_active_folder_row(row)
            })
            capacity_folders = _number(daily.get("capacity_folders_count")) or len(folder_keys)
            available = _number(daily.get("available_capacity")) or capacity_folders * CAPACITY_MINUTES_PER_FOLDER_DAY
            runtime = sum(_number(row.get("runtime")) for row in details)
            waiting_time = sum(_number(row.get("waiting_time")) for row in details)
            loss_time = sum(_loss_time_minutes(row) for row in details)
            downtime = sum(_number(row.get("downtime")) for row in details)
            spare_time = sum(_number(row.get("buffer_time")) for row in details)
            unplanned_time = _number(daily.get("idle_time"))
            if unplanned_time <= 0:
                unplanned_time = sum(_number(row.get("idle_time")) for row in details)
        else:
            active_folders = _number(daily.get("active_folders_count"))
            capacity_folders = _number(daily.get("capacity_folders_count"))
            available = _number(daily.get("available_capacity"))
            runtime = _number(daily.get("runtime"))
            waiting_time = _number(daily.get("waiting_time"))
            loss_time = _number(daily.get("loss_time") or daily.get("lost_time"))
            downtime = _number(daily.get("downtime"))
            spare_time = _number(daily.get("buffer_time"))
            unplanned_time = _number(daily.get("idle_time"))
        utilized_time = runtime + downtime + loss_time
        runtime_segments = _runtime_segments_for_rows(details)
        is_gnp_night = any(_is_gnp_segment(segment) for segment in runtime_segments)
        delayed_rows = [row for row in details if _number(row.get("overrun_minutes")) > 0]
        max_overrun = max([_number(row.get("overrun_minutes")) for row in delayed_rows], default=0.0)

        rows.append({
            "run_date": run_date,
            "night_type": "GNP/UV" if is_gnp_night else "SNP/non-UV",
            "gnp_night": is_gnp_night,
            "uv_night": is_gnp_night,
            "active_folders": _clean_number(active_folders),
            "capacity_folders": _clean_number(capacity_folders),
            "available_capacity_min": _clean_number(available),
            "runtime_min": _clean_number(runtime),
            "loss_time_min": _clean_number(loss_time),
            "waiting_time_min": _clean_number(waiting_time),
            "downtime_min": _clean_number(downtime),
            "spare_time_min": _clean_number(spare_time),
            "unplanned_time_min": _clean_number(unplanned_time),
            "utilization_pct": _percentage(utilized_time, available),
            "spare_capacity_pct": _percentage(spare_time, max(available - unplanned_time, 0)),
            "delayed_pf_count": len(delayed_rows),
            "max_overrun_minutes": _clean_number(max_overrun),
            "delayed_pf_folders": [_display_resource_name(row.get("folder")) for row in delayed_rows],
            "complexity_codes": _complexity_codes_for_segments(runtime_segments),
            "complexity_categories": _complexity_categories_for_segments(runtime_segments),
            "runtime_segments": runtime_segments,
            "editions": _editions_for_rows(details),
        })

    return rows


def _exact_folder_rows(folder_rows: list[dict[str, Any]], dates: list[str]) -> list[dict[str, Any]]:
    production_days = len(dates)
    grouped: dict[str, list[dict[str, Any]]] = {}
    for row in folder_rows:
        folder = _clean_text(row.get("folder"))
        if folder:
            grouped.setdefault(folder, []).append(row)

    rows = []
    for folder in _sorted_unique(grouped.keys()):
        details = grouped.get(folder, [])
        active_rows = [row for row in details if _is_active_folder_row(row)]
        dates_with_rows = {_clean_text(row.get("run_date")) for row in details if row.get("run_date")}
        missing_days = max(production_days - len(dates_with_rows), 0)
        possible_capacity = production_days * CAPACITY_MINUTES_PER_FOLDER_DAY if production_days else sum(
            _available_minutes(row) for row in details
        )
        active_capacity = sum(_available_minutes(row) for row in active_rows)
        runtime = sum(_number(row.get("runtime")) for row in details)
        waiting_time = sum(_number(row.get("waiting_time")) for row in details)
        loss_time = sum(_loss_time_minutes(row) for row in details)
        downtime = sum(_number(row.get("downtime")) for row in details)
        utilized_time = runtime + downtime + loss_time
        spare_time = sum(_number(row.get("buffer_time")) for row in details)
        unplanned_time = sum(_number(row.get("idle_time")) for row in details) + missing_days * CAPACITY_MINUTES_PER_FOLDER_DAY
        active_nights = len({_clean_text(row.get("run_date")) for row in active_rows if row.get("run_date")})
        unplanned_nights = max(production_days - active_nights, 0)
        runtime_segments = _runtime_segments_for_rows(details)
        active_dates = _sorted_unique(row.get("run_date") for row in active_rows)

        rows.append({
            "resource": _display_resource_name(folder),
            "runtime_min": _clean_number(runtime),
            "loss_time_min": _clean_number(loss_time),
            "waiting_time_min": _clean_number(waiting_time),
            "downtime_min": _clean_number(downtime),
            "spare_time_min": _clean_number(spare_time),
            "unplanned_time_min": _clean_number(unplanned_time),
            "possible_capacity_min": _clean_number(possible_capacity),
            "active_capacity_min": _clean_number(active_capacity),
            "active_nights": active_nights,
            "total_nights": production_days,
            "unplanned_nights": unplanned_nights,
            "folder_nights": f"{active_nights}/{production_days}" if production_days else "0/0",
            "active_dates": active_dates,
            "utilization_pct": _percentage(utilized_time, possible_capacity),
            "active_day_utilization_pct": _percentage(utilized_time, active_capacity),
            "spare_capacity_pct": _percentage(spare_time, max(possible_capacity - unplanned_time, 0)),
            "complexity_codes": _complexity_codes_for_segments(runtime_segments),
            "complexity_categories": _complexity_categories_for_segments(runtime_segments),
            "runtime_segments": runtime_segments,
            "editions": _editions_for_rows(details),
        })

    rows.sort(key=lambda row: (-_number(row.get("runtime_min")), row.get("resource", "")))
    return rows


def _exact_folder_day_rows(folder_rows: list[dict[str, Any]], question: str) -> tuple[list[dict[str, Any]], str]:
    total_rows = len(folder_rows)
    matching_rows = [row for row in folder_rows if _row_matches_question(row, question)]
    selected_rows = matching_rows or folder_rows
    limit = _folder_day_context_limit(matching=bool(matching_rows))
    note = f"Showing {min(len(selected_rows), limit)} of {total_rows} folder-day rows."
    if matching_rows:
        note = f"Filtered to {min(len(selected_rows), limit)} folder-day rows matching the question out of {total_rows} total rows."
    elif total_rows > limit:
        note = (
            f"Only the first {limit} folder-day rows are included to keep chat context compact; "
            "use daily and folder totals for complete-period answers."
        )

    rows = [_exact_folder_day_row(row) for row in selected_rows[:limit]]
    return rows, note


def _folder_day_context_limit(matching: bool) -> int:
    configured = _get_env("CAPACITY_CHAT_MAX_FOLDER_DAY_ROWS")
    if configured:
        try:
            value = int(configured)
            if value <= 0:
                return 1_000_000
            return max(value, 1)
        except ValueError:
            pass
    return 2000 if matching else 1500


def _exact_folder_day_row(row: dict[str, Any]) -> dict[str, Any]:
    available = _available_minutes(row)
    unplanned_time = _number(row.get("idle_time"))
    spare_time = _number(row.get("buffer_time"))
    runtime = _number(row.get("runtime"))
    loss_time = _loss_time_minutes(row)
    downtime = _number(row.get("downtime"))
    utilized_time = runtime + downtime + loss_time
    runtime_segments = _runtime_segment_rows(row)
    machine, folder_name = _split_machine_folder(_clean_text(row.get("folder")))
    overrun = _number(row.get("overrun_minutes"))
    cutoff_minutes = _pf_compliance_minutes(row.get("plant_name"))
    return {
        "run_date": _clean_text(row.get("run_date")),
        "plant": _clean_text(row.get("plant_name")),
        "machine": machine,
        "folder_name": folder_name,
        "folder": _display_resource_name(row.get("folder")),
        "active_night": _is_active_folder_row(row),
        "available_capacity_min": _clean_number(available),
        "runtime_min": _clean_number(runtime),
        "loss_time_min": _clean_number(loss_time),
        "waiting_time_min": _clean_number(row.get("waiting_time")),
        "downtime_min": _clean_number(downtime),
        "spare_time_min": _clean_number(spare_time),
        "unplanned_time_min": _clean_number(unplanned_time),
        "utilization_pct": _percentage(utilized_time, available),
        "spare_capacity_pct": _percentage(spare_time, max(available - unplanned_time, 0)),
        "delayed_print_finish": overrun > 0,
        "overrun_minutes": _clean_number(overrun),
        "pf_cutoff_time": _format_clock_time(cutoff_minutes),
        "estimated_print_finish_time": _format_clock_time(cutoff_minutes + overrun) if overrun > 0 else "",
        "complexity_codes": _complexity_codes_for_segments(runtime_segments),
        "complexity_categories": _complexity_categories_for_segments(runtime_segments),
        "runtime_segments": runtime_segments,
        "editions": _editions_for_rows([row]),
    }


def _row_matches_question(row: dict[str, Any], question: str) -> bool:
    text = _clean_text(question).casefold()
    if not text:
        return False

    candidates = [
        _clean_text(row.get("run_date")),
        _date_label(_clean_text(row.get("run_date"))),
        _display_resource_name(row.get("folder")),
        _clean_text(row.get("plant_name")),
    ]
    return any(_candidate_matches_question(candidate, text) for candidate in candidates)


def _candidate_matches_question(candidate: str, question_text: str) -> bool:
    text = _clean_text(candidate).casefold()
    if not text:
        return False
    if text in question_text:
        return True

    parts = [
        part
        for part in text.replace("/", " ").replace("-", " ").replace("_", " ").split()
        if len(part) > 1
    ]
    return any(part in question_text for part in parts)


def _date_label(value: str) -> str:
    if not value:
        return ""
    try:
        parsed = datetime.strptime(value, "%Y-%m-%d")
        return f"{parsed.strftime('%b')} {parsed.day}"
    except ValueError:
        return value


def _is_active_folder_row(row: dict[str, Any]) -> bool:
    active_minutes = sum(
        _number(row.get(key))
        for key in [
            "runtime",
            "downtime",
            "buffer_time",
            "change_over_time",
            "waiting_time",
            "reflong_related_downtime",
            "late_start_time",
            "gross_runtime",
            "scheduled_runtime",
            "overlap_minutes",
        ]
    )
    available = _available_minutes(row)
    idle_time = _number(row.get("idle_time"))
    return not (available > 0 and idle_time >= available and active_minutes <= 0)


def _loss_time_minutes(row: dict[str, Any]) -> float:
    component_loss = sum(_number(row.get(key)) for key, _ in LOSS_COMPONENTS)
    if component_loss > 0:
        return component_loss
    return _number(row.get("lost_time") or row.get("loss_time"))


def _runtime_segment_rows(row: dict[str, Any]) -> list[dict[str, Any]]:
    segments = []
    for segment in row.get("runtime_segments") or []:
        if not isinstance(segment, dict):
            continue
        minutes = _number(segment.get("minutes"))
        if minutes <= 0:
            continue
        code = _clean_complexity_code(segment)
        category = _complexity_label(segment)
        segments.append({
            "complexity_code": code,
            "category": category,
            "type": _clean_text(segment.get("type")) or _complexity_type_from_code(code),
            "is_complex": _is_complex_segment(segment),
            "minutes": _clean_number(minutes),
            "print_order": _clean_number(segment.get("print_order")),
            "speed_cph": _clean_number(segment.get("effective_speed")),
        })
    return sorted(segments, key=lambda item: _complexity_code_sort_key(item.get("complexity_code")))


def _runtime_segments_for_rows(rows: list[dict[str, Any]]) -> list[dict[str, Any]]:
    buckets: dict[str, dict[str, Any]] = {}
    for row in rows:
        for segment in _runtime_segment_rows(row):
            code = segment.get("complexity_code") or segment.get("category") or "Unknown"
            bucket = buckets.setdefault(
                code,
                {
                    "complexity_code": segment.get("complexity_code"),
                    "category": segment.get("category"),
                    "type": segment.get("type"),
                    "is_complex": segment.get("is_complex"),
                    "minutes": 0.0,
                    "print_order": 0.0,
                    "speed_weighted_total": 0.0,
                    "speed_weight_minutes": 0.0,
                },
            )
            minutes = _number(segment.get("minutes"))
            speed = _number(segment.get("speed_cph"))
            bucket["minutes"] += minutes
            bucket["print_order"] += _number(segment.get("print_order"))
            if speed > 0:
                bucket["speed_weighted_total"] += speed * minutes
                bucket["speed_weight_minutes"] += minutes

    result = []
    for bucket in buckets.values():
        speed = (
            bucket["speed_weighted_total"] / bucket["speed_weight_minutes"]
            if bucket["speed_weight_minutes"] > 0
            else _speed_from_print_order(bucket["print_order"], bucket["minutes"])
        )
        result.append({
            "complexity_code": bucket.get("complexity_code"),
            "category": bucket.get("category"),
            "type": bucket.get("type"),
            "is_complex": bucket.get("is_complex"),
            "minutes": _clean_number(bucket["minutes"]),
            "print_order": _clean_number(bucket["print_order"]),
            "speed_cph": _clean_number(speed),
        })
    return sorted(result, key=lambda item: _complexity_code_sort_key(item.get("complexity_code")))


def _editions_for_rows(rows: list[dict[str, Any]]) -> list[str]:
    editions = {
        _clean_text(edition)
        for row in rows
        for edition in (row.get("editions") or [])
        if _clean_text(edition)
    }
    return sorted(editions)


def _complexity_codes_for_segments(segments: list[dict[str, Any]]) -> list[str]:
    return [
        code for code in _sorted_unique(segment.get("complexity_code") for segment in segments)
        if code
    ]


def _complexity_categories_for_segments(segments: list[dict[str, Any]]) -> list[str]:
    return [
        category for category in _sorted_unique(segment.get("category") for segment in segments)
        if category
    ]


def _has_gnp_runtime(rows: list[dict[str, Any]]) -> bool:
    return any(_is_gnp_segment(segment) for row in rows for segment in _runtime_segment_rows(row))


def _build_gnp_night_classification(folder_rows: list[dict[str, Any]], dates: list[str]) -> dict[str, Any]:
    rows_by_date: dict[str, list[dict[str, Any]]] = {date: [] for date in dates}
    for row in folder_rows:
        run_date = _clean_text(row.get("run_date"))
        if run_date:
            rows_by_date.setdefault(run_date, []).append(row)

    night_rows = []
    for run_date in _sorted_unique(rows_by_date.keys()):
        rows = rows_by_date.get(run_date, [])
        segments = _runtime_segments_for_rows(rows)
        is_gnp = any(_is_gnp_segment(segment) for segment in segments)
        night_rows.append({
            "run_date": run_date,
            "night_type": "GNP/UV" if is_gnp else "SNP/non-UV",
            "gnp_night": is_gnp,
            "uv_night": is_gnp,
            "complexity_codes": _complexity_codes_for_segments(segments),
            "complexity_categories": _complexity_categories_for_segments(segments),
            "editions": _editions_for_rows(rows),
        })

    gnp_dates = [row["run_date"] for row in night_rows if row["gnp_night"]]
    snp_dates = [row["run_date"] for row in night_rows if not row["gnp_night"]]
    return {
        "definition": "A GNP/UV night is any date where at least one folder ran GNP or GNP Complex editions (C5-C15). Otherwise it is an SNP/non-UV night.",
        "nights": night_rows,
        "gnp_nights": gnp_dates,
        "snp_nights": snp_dates,
        "uv_nights": gnp_dates,
        "non_uv_nights": snp_dates,
        "gnp_night_count": len(gnp_dates),
        "snp_night_count": len(snp_dates),
        "uv_night_count": len(gnp_dates),
        "non_uv_night_count": len(snp_dates),
        "total_nights": len(night_rows),
    }


def _build_delayed_pf_rows(folder_rows: list[dict[str, Any]]) -> list[dict[str, Any]]:
    rows = []
    for row in folder_rows:
        overrun = _number(row.get("overrun_minutes"))
        if overrun <= 0:
            continue
        runtime_segments = _runtime_segment_rows(row)
        plant = _clean_text(row.get("plant_name"))
        cutoff_minutes = _pf_compliance_minutes(plant)
        machine, folder_name = _split_machine_folder(_clean_text(row.get("folder")))
        rows.append({
            "run_date": _clean_text(row.get("run_date")),
            "plant": plant,
            "machine": machine,
            "folder_name": folder_name,
            "folder": _display_resource_name(row.get("folder")),
            "pf_cutoff_time": _format_clock_time(cutoff_minutes),
            "estimated_print_finish_time": _format_clock_time(cutoff_minutes + overrun),
            "overrun_minutes": _clean_number(overrun),
            "runtime_min": _clean_number(row.get("runtime")),
            "loss_time_min": _clean_number(_loss_time_minutes(row)),
            "waiting_time_min": _clean_number(row.get("waiting_time")),
            "downtime_min": _clean_number(row.get("downtime")),
            "spare_time_min": _clean_number(row.get("buffer_time")),
            "unplanned_time_min": _clean_number(row.get("idle_time")),
            "complexity_codes": _complexity_codes_for_segments(runtime_segments),
            "complexity_categories": _complexity_categories_for_segments(runtime_segments),
            "runtime_segments": runtime_segments,
            "editions": _editions_for_rows([row]),
            "largest_components": _largest_delayed_pf_components(row),
            "twin_folder_mode": bool(row.get("twin_folder_mode")),
            "twin_folder_group": _clean_text(row.get("twin_folder_group")),
        })

    rows.sort(key=lambda row: (-_number(row.get("overrun_minutes")), row.get("run_date", ""), row.get("folder", "")))
    return rows


def _complexity_downtime_by_code(rows: list[dict[str, Any]]) -> list[dict[str, Any]]:
    code_buckets: dict[str, dict[str, Any]] = {}
    for row in rows:
        runtime_segments = _runtime_segment_rows(row)
        segment_total = sum(_number(seg.get("minutes")) for seg in runtime_segments)
        row_downtime = _number(row.get("downtime"))
        row_loss_time = _loss_time_minutes(row)
        row_waiting_time = _number(row.get("waiting_time"))
        for seg in runtime_segments:
            code = _clean_text(seg.get("complexity_code"))
            if not code:
                continue
            segment_minutes = _number(seg.get("minutes"))
            share = segment_minutes / segment_total if segment_total > 0 else 0
            bucket = code_buckets.setdefault(
                code,
                {
                    "code": code,
                    "label": _clean_text(seg.get("category")),
                    "type": _clean_text(seg.get("type")),
                    "is_complex": bool(seg.get("is_complex")),
                    "runtime_min": 0.0,
                    "print_order": 0.0,
                    "allocated_downtime_min": 0.0,
                    "allocated_loss_time_min": 0.0,
                    "allocated_waiting_time_min": 0.0,
                    "folder_day_count": 0,
                    "downtime_row_count": 0,
                },
            )
            bucket["runtime_min"] += segment_minutes
            bucket["print_order"] += _number(seg.get("print_order"))
            bucket["allocated_downtime_min"] += row_downtime * share
            bucket["allocated_loss_time_min"] += row_loss_time * share
            bucket["allocated_waiting_time_min"] += row_waiting_time * share
            bucket["folder_day_count"] += 1
            if row_downtime > 0:
                bucket["downtime_row_count"] += 1

    output = [
        {
            "code": bucket["code"],
            "label": bucket["label"],
            "type": bucket["type"],
            "is_complex": bucket["is_complex"],
            "runtime_min": _clean_number(bucket["runtime_min"]),
            "print_order": _clean_number(bucket["print_order"]),
            "allocated_downtime_min": _clean_number(bucket["allocated_downtime_min"]),
            "allocated_loss_time_min": _clean_number(bucket["allocated_loss_time_min"]),
            "allocated_waiting_time_min": _clean_number(bucket["allocated_waiting_time_min"]),
            "folder_day_count": bucket["folder_day_count"],
            "downtime_row_count": bucket["downtime_row_count"],
        }
        for bucket in code_buckets.values()
        if bucket["runtime_min"] > 0
    ]
    output.sort(key=lambda row: (-_number(row.get("allocated_downtime_min")), _complexity_code_sort_key(row.get("code"))))
    return output


def _tower_day_context_rows(tower_details: list[dict[str, Any]]) -> list[dict[str, Any]]:
    rows = []
    for row in tower_details:
        tower = _display_resource_name(row.get("tower"))
        if not tower:
            continue
        machine, tower_name = _split_machine_folder(_clean_text(row.get("tower")))
        _, folder_name = _split_machine_folder(_clean_text(row.get("folder")))
        runtime_segments = _runtime_segment_rows(row)
        loss_time = _loss_time_minutes(row)
        downtime = _number(row.get("downtime"))
        runtime = _number(row.get("runtime"))
        rows.append({
            "run_date": _clean_text(row.get("run_date")),
            "plant": _clean_text(row.get("plant_name")),
            "machine": machine,
            "tower_name": tower_name,
            "tower": tower,
            "folder": _display_resource_name(row.get("folder")),
            "folder_name": folder_name,
            "uv_tower": bool(row.get("uv_tower")),
            "runtime_min": _clean_number(runtime),
            "downtime_min": _clean_number(downtime),
            "loss_time_min": _clean_number(loss_time),
            "waiting_time_min": _clean_number(row.get("waiting_time")),
            "spare_time_min": _clean_number(row.get("buffer_time")),
            "change_over_time_min": _clean_number(row.get("change_over_time")),
            "late_start_time_min": _clean_number(row.get("late_start_time")),
            "reflong_time_min": _clean_number(row.get("reflong_related_downtime")),
            "has_downtime": downtime > 0,
            "has_loss_time": loss_time > 0,
            "has_waiting_time": _number(row.get("waiting_time")) > 0,
            "utilization_pct": _percentage(runtime + downtime + loss_time, _available_minutes(row)),
            "complexity_codes": _complexity_codes_for_segments(runtime_segments),
            "complexity_categories": _complexity_categories_for_segments(runtime_segments),
            "runtime_segments": runtime_segments,
            "editions": _editions_for_rows([row]),
        })

    rows.sort(key=lambda row: (row.get("run_date", ""), row.get("tower", ""), row.get("folder", "")))
    return rows


def _build_tower_downtime_reason_attribution(
    tower_details: list[dict[str, Any]],
    downtime_reasons: list[dict[str, Any]],
) -> dict[str, Any]:
    tower_rows_by_unit: dict[tuple[str, str, str], list[dict[str, Any]]] = {}
    for row in tower_details:
        plant = _clean_text(row.get("plant_name")).casefold()
        machine, folder_name = _split_machine_folder(_clean_text(row.get("folder")))
        key = (plant, machine.casefold(), folder_name.casefold())
        tower_rows_by_unit.setdefault(key, []).append(row)

    by_tower_reason: dict[tuple[str, str], dict[str, Any]] = {}
    by_tower: dict[str, dict[str, Any]] = {}

    for rec in downtime_reasons:
        reason = _clean_text(rec.get("reason"))
        if not reason:
            continue
        plant = _clean_text(rec.get("plant")).casefold()
        machine = _clean_text(rec.get("machine")).casefold()
        folder_name = _clean_text(rec.get("folder")).casefold()
        event_count = int(_number(rec.get("count")))
        total_minutes = _number(rec.get("total_minutes"))
        candidate_keys = [(plant, machine, folder_name)] if plant else []
        if not candidate_keys:
            candidate_keys = [
                key for key in tower_rows_by_unit
                if key[1] == machine and key[2] == folder_name
            ]

        matching_rows = []
        for key in candidate_keys:
            matching_rows.extend(tower_rows_by_unit.get(key, []))

        towers_for_reason: dict[str, list[dict[str, Any]]] = {}
        for row in matching_rows:
            tower = _display_resource_name(row.get("tower"))
            if tower:
                towers_for_reason.setdefault(tower, []).append(row)

        for tower, rows in towers_for_reason.items():
            tower_key = (tower, reason)
            entry = by_tower_reason.setdefault(
                tower_key,
                {
                    "tower": tower,
                    "reason": reason,
                    "attributed_event_count": 0,
                    "attributed_minutes": 0.0,
                    "matching_tower_run_count": 0,
                    "matching_dates": set(),
                    "folders": set(),
                    "editions": set(),
                },
            )
            entry["attributed_event_count"] += event_count
            entry["attributed_minutes"] += total_minutes
            entry["matching_tower_run_count"] += len(rows)
            for row in rows:
                run_date = _clean_text(row.get("run_date"))
                if run_date:
                    entry["matching_dates"].add(run_date)
                folder = _display_resource_name(row.get("folder"))
                if folder:
                    entry["folders"].add(folder)
                for edition in row.get("editions") or []:
                    ed_text = _clean_text(edition)
                    if ed_text:
                        entry["editions"].add(ed_text)

            tower_entry = by_tower.setdefault(
                tower,
                {
                    "tower": tower,
                    "attributed_event_count": 0,
                    "attributed_minutes": 0.0,
                    "matching_tower_run_count": 0,
                    "reasons": set(),
                    "matching_dates": set(),
                },
            )
            tower_entry["attributed_event_count"] += event_count
            tower_entry["attributed_minutes"] += total_minutes
            tower_entry["matching_tower_run_count"] += len(rows)
            tower_entry["reasons"].add(reason)
            for row in rows:
                run_date = _clean_text(row.get("run_date"))
                if run_date:
                    tower_entry["matching_dates"].add(run_date)

    by_tower_reason_rows = [
        {
            "tower": entry["tower"],
            "reason": entry["reason"],
            "attributed_event_count": entry["attributed_event_count"],
            "attributed_minutes": _clean_number(entry["attributed_minutes"]),
            "matching_tower_run_count": entry["matching_tower_run_count"],
            "matching_dates": sorted(entry["matching_dates"]),
            "folders": sorted(entry["folders"]),
            "editions": sorted(entry["editions"]),
        }
        for entry in by_tower_reason.values()
    ]
    by_tower_reason_rows.sort(
        key=lambda row: (-row["attributed_event_count"], -row["matching_tower_run_count"], row["tower"], row["reason"])
    )

    by_tower_rows = [
        {
            "tower": entry["tower"],
            "attributed_event_count": entry["attributed_event_count"],
            "attributed_minutes": _clean_number(entry["attributed_minutes"]),
            "matching_tower_run_count": entry["matching_tower_run_count"],
            "reasons": sorted(entry["reasons"]),
            "matching_dates": sorted(entry["matching_dates"]),
        }
        for entry in by_tower.values()
    ]
    by_tower_rows.sort(key=lambda row: (-row["attributed_event_count"], -row["matching_tower_run_count"], row["tower"]))

    return {
        "attribution_note": (
            "Down Time reason rows are recorded at plant/machine/folder level, not tower level. "
            "These reason counts are attributed to towers that ran the same plant/machine/folder in the selected period."
        ),
        "by_tower": by_tower_rows[:500],
        "by_tower_reason": by_tower_reason_rows[:1000],
    }


def _build_tower_availability_summary(
    tower_rows: list[dict[str, Any]],
    tower_day_rows: list[dict[str, Any]],
    exact_dashboard: dict[str, Any],
) -> dict[str, Any]:
    towers = sorted({_clean_text(row.get("tower")) for row in tower_rows if _clean_text(row.get("tower"))})
    selected_dates = [
        _clean_text(row.get("run_date"))
        for row in (exact_dashboard.get("daily") or [])
        if _clean_text(row.get("run_date"))
    ]
    if not selected_dates:
        selected_dates = _sorted_unique(row.get("run_date") for row in tower_day_rows)

    total_towers = len(towers)
    total_days = len(selected_dates)
    active_sets_by_day: dict[str, set[str]] = {date: set() for date in selected_dates}
    for row in tower_day_rows:
        run_date = _clean_text(row.get("run_date"))
        tower = _clean_text(row.get("tower"))
        if run_date in active_sets_by_day and tower:
            active_sets_by_day[run_date].add(tower)

    active_towers_by_day = []
    for run_date in selected_dates:
        active_count = len(active_sets_by_day.get(run_date, set()))
        active_pct = _percentage(active_count, total_towers)
        active_towers_by_day.append({
            "run_date": run_date,
            "active_towers": active_count,
            "total_towers": total_towers,
            "active_tower_pct": active_pct,
        })

    threshold_percentages = [50, 60, 70, 80, 90, 100]
    threshold_days = []
    for threshold_pct in threshold_percentages:
        required_towers = int((threshold_pct / 100.0) * total_towers)
        if total_towers > 0 and required_towers < (threshold_pct / 100.0) * total_towers:
            required_towers += 1
        matching_days = [
            row.get("run_date")
            for row in active_towers_by_day
            if _number(row.get("active_towers")) >= required_towers and total_towers > 0
        ]
        threshold_days.append({
            "threshold_pct": threshold_pct,
            "required_towers": required_towers,
            "days": len(matching_days),
            "total_days": total_days,
            "matching_dates": matching_days,
        })

    return {
        "total_towers": total_towers,
        "total_days": total_days,
        "tower_names": towers,
        "active_towers_by_day": active_towers_by_day,
        "threshold_days": threshold_days,
    }


def _largest_delayed_pf_components(row: dict[str, Any]) -> list[dict[str, Any]]:
    components = [
        ("runtime", "Run Time", _number(row.get("runtime"))),
        ("loss_time", "Loss Time", _loss_time_minutes(row)),
        ("waiting_time", "Wait Time", _number(row.get("waiting_time"))),
        ("downtime", "Downtime", _number(row.get("downtime"))),
        ("spare_time", "Spare Time", _number(row.get("buffer_time"))),
    ]
    return [
        {"key": key, "label": label, "minutes": _clean_number(minutes)}
        for key, label, minutes in sorted(components, key=lambda item: -item[2])
        if minutes > 0
    ][:3]


def _pf_compliance_minutes(plant_name: Any) -> float:
    return PF_COMPLIANCE_MINUTES_BY_PLANT.get(
        _clean_text(plant_name).casefold(),
        CAPACITY_MINUTES_PER_FOLDER_DAY,
    )


def _format_clock_time(minutes_from_midnight: float) -> str:
    total_minutes = int(round(max(_number(minutes_from_midnight), 0)))
    day_offset = total_minutes // 1440
    clock_minutes = total_minutes % 1440
    hours = clock_minutes // 60
    minutes = clock_minutes % 60
    label = f"{hours:02d}:{minutes:02d}"
    return f"{label} +{day_offset}d" if day_offset > 0 else label


def _chat_scope_label(daily_rows: list[dict[str, Any]]) -> str:
    dates = _sorted_unique(row.get("run_date") for row in daily_rows)
    if not dates:
        return "Selected timeframe"
    if dates[0] == dates[-1]:
        return dates[0]
    return f"{dates[0]} to {dates[-1]}"


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
    malt = _build_malt_analysis(folder_rows)

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
            "malt_formula": malt["formula"],
            "loss_time_percentage": loss_time["loss_time_percentage"],
            "dominant_loss_driver": loss_time["dominant_driver"]["label"] if loss_time["dominant_driver"] else "",
            "peak_loss_day": loss_time["peak_day"]["run_date"] if loss_time["peak_day"] else "",
            "peak_loss_minutes": loss_time["peak_day"]["lost_time_minutes"] if loss_time["peak_day"] else 0,
        },
        "sections": {
            "complexity_speed": complexity_speed,
            "folder_utilization": folder_utilization,
            "loss_time": loss_time,
            "max_allowable_loss_time": malt,
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
        non_wait_lost_time = sum(_loss_time_minutes(row) for row in rows)
        downtime = sum(_number(row.get("downtime")) for row in rows)
        utilized_time = runtime + downtime + non_wait_lost_time
        buffer_time = sum(_number(row.get("buffer_time")) for row in rows)
        if buffer_time <= 0 and rows:
            buffer_time = max(active_capacity - runtime - waiting_time - lost_time_raw - downtime, 0)
        active_days = len({row.get("run_date") for row in rows if row.get("run_date")})
        unplanned_days = max(production_days - active_days, 0)
        unplanned_time = max(possible_capacity - active_capacity, 0)
        daily_utilization_percentages = _daily_folder_utilization_percentages(rows, dates)
        variability = pstdev(daily_utilization_percentages) if len(daily_utilization_percentages) > 1 else 0.0
        utilization = _percentage(utilized_time, possible_capacity)
        active_day_utilization = _percentage(utilized_time, active_capacity)
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
                    idle_days=unplanned_days,
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
        component_values = {
            key: sum(_number(row.get(key)) for row in details)
            for key, _ in LOSS_COMPONENTS
        }
        component_loss = sum(component_values.values())
        lost_time = component_loss if component_loss > 0 else lost_time_raw

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


def _build_malt_analysis(folder_rows: list[dict[str, Any]]) -> dict[str, Any]:
    formula = "MALT = 240 - P50(Wait) - P85(MOT) - P30(Spare), where MOT = Run Time + Downtime"
    groups: dict[str, dict[str, Any]] = {}
    all_samples: list[dict[str, Any]] = []

    for row in folder_rows:
        sample = _malt_sample(row)
        if not sample:
            continue

        all_samples.append(sample)
        if not sample["on_time_calibration_sample"]:
            continue

        group = groups.setdefault(
            sample["calibration_key"],
            {
                "key": sample["calibration_key"],
                "plant": sample["plant"],
                "complexity": sample["complexity"],
                "wait_values": [],
                "mot_values": [],
                "spare_values": [],
            },
        )
        group["wait_values"].append(sample["wait_min"])
        group["mot_values"].append(sample["mot_min"])
        group["spare_values"].append(sample["spare_min"])

    rows = []
    threshold_by_key: dict[str, dict[str, Any]] = {}
    for group in groups.values():
        sample_count = len(group["wait_values"])
        percentiles = _malt_percentiles()
        wait_value = _percentile(group["wait_values"], percentiles["wait"])
        mot_value = _percentile(group["mot_values"], percentiles["mot"])
        spare_value = _percentile(group["spare_values"], percentiles["spare"])
        malt = max(CAPACITY_MINUTES_PER_FOLDER_DAY - wait_value - mot_value - spare_value, 0)
        row = {
            "key": group["key"],
            "plant": group["plant"],
            "complexity": group["complexity"],
            "formula": formula,
            "applied_formula": (
                f"MALT = 240 - P{percentiles['wait']}(Wait) "
                f"- P{percentiles['mot']}(MOT) - P{percentiles['spare']}(Spare); "
                "MOT = Run Time + Downtime"
            ),
            "percentiles_used": percentiles,
            "wait_min": _clean_number(wait_value),
            "mot_min": _clean_number(mot_value),
            "run_plus_down_label": "MOT = Run Time + Downtime",
            "spare_min": _clean_number(spare_value),
            "malt_min": _clean_number(malt),
            "on_time_nights": sample_count,
            "confidence": _malt_confidence(sample_count),
        }
        rows.append(row)
        threshold_by_key[group["key"]] = row

    exceedances = []
    for sample in all_samples:
        threshold = threshold_by_key.get(sample["calibration_key"])
        if not threshold:
            continue
        excess = sample["actual_loss_min"] - _number(threshold.get("malt_min"))
        if excess <= 0:
            continue
        exceedances.append({
            "run_date": sample["run_date"],
            "plant": sample["plant"],
            "machine": sample["machine"],
            "folder": sample["folder"],
            "complexity": sample["complexity"],
            "actual_loss_min": _clean_number(sample["actual_loss_min"]),
            "malt_min": threshold["malt_min"],
            "excess_min": _clean_number(excess),
            "wait_min": _clean_number(sample["wait_min"]),
            "mot_min": _clean_number(sample["mot_min"]),
            "spare_min": _clean_number(sample["spare_min"]),
            "overrun_min": _clean_number(sample["overrun_min"]),
            "threshold_key": sample["calibration_key"],
        })

    rows.sort(key=lambda row: (
        _clean_text(row.get("plant")).casefold(),
        _malt_complexity_rank(_clean_text(row.get("complexity"))),
    ))
    exceedances.sort(key=lambda row: (-_number(row.get("excess_min")), row.get("run_date", ""), row.get("folder", "")))

    return {
        "formula": formula,
        "formula_terms": {
            "fixed_window_min": CAPACITY_MINUTES_PER_FOLDER_DAY,
            "wait": "Editorial wait percentile from on-time calibration nights",
            "mot": "Machine Operating Time percentile, where MOT = Run Time + Downtime",
            "spare": "Finish-buffer spare percentile from on-time calibration nights",
        },
        "percentile_policy": {
            "wait": MALT_WAIT_PERCENTILE,
            "mot": MALT_MOT_PERCENTILE,
            "spare": MALT_SPARE_PERCENTILE,
            "selection": "Fixed percentiles: P50 Wait, P85 MOT, P30 Spare.",
            "calibration_grain": "plant × complexity",
        },
        "calibration_filter": "Only active folder nights with overrun_minutes <= 0 and a 240-minute identity are used to calibrate MALT.",
        "rows": rows,
        "night_exceedances": exceedances,
    }


def _malt_sample(row: dict[str, Any]) -> dict[str, Any] | None:
    if not _is_active_folder_row(row):
        return None

    wait = _number(row.get("waiting_time"))
    run = _number(row.get("runtime"))
    down = _number(row.get("downtime"))
    spare = _number(row.get("buffer_time"))
    idle = _number(row.get("idle_time"))
    actual_loss = _loss_time_minutes(row)
    overrun = _number(row.get("overrun_minutes"))

    if any(value < 0 for value in [wait, run, down, spare, idle, actual_loss, overrun]):
        return None
    if run + down <= 0:
        return None

    plant = _clean_text(row.get("plant_name")) or "Unknown plant"
    machine, folder_name = _split_machine_folder(_clean_text(row.get("folder")))
    complexity = _dominant_complexity_label(row.get("runtime_segments"))
    identity_total = wait + actual_loss + run + down + spare + idle
    on_time = (
        overrun <= 0
        and abs(identity_total - CAPACITY_MINUTES_PER_FOLDER_DAY) <= MALT_IDENTITY_TOLERANCE_MINUTES
    )

    return {
        "run_date": _clean_text(row.get("run_date")),
        "plant": plant,
        "machine": machine or "Unknown machine",
        "folder": _display_resource_name(row.get("folder")) or folder_name,
        "complexity": complexity,
        "calibration_key": "||".join([plant, complexity]),
        "wait_min": wait,
        "mot_min": run + down,
        "spare_min": spare,
        "actual_loss_min": actual_loss,
        "overrun_min": overrun,
        "on_time_calibration_sample": on_time,
    }


def _malt_percentiles() -> dict[str, int]:
    return {
        "wait": MALT_WAIT_PERCENTILE,
        "mot": MALT_MOT_PERCENTILE,
        "spare": MALT_SPARE_PERCENTILE,
    }


def _malt_confidence(sample_count: int) -> str:
    if sample_count >= 60:
        return "High confidence"
    if sample_count >= 30:
        return "Medium confidence"
    return "Low confidence"


def _dominant_complexity_label(segments: Any) -> str:
    if not isinstance(segments, list) or not segments:
        return "Unknown"

    best = max(
        (segment for segment in segments if isinstance(segment, dict)),
        key=lambda segment: _number(segment.get("minutes")),
        default={},
    )
    return _complexity_label(best) if best else "Unknown"


def _malt_complexity_rank(label: str) -> int:
    order = ["SNP", "SNP Complex", "GNP", "GNP Complex", "Unknown"]
    try:
        return order.index(label)
    except ValueError:
        return len(order)


def _percentile(values: list[float], percentile: float) -> float:
    sorted_values = sorted(_number(value) for value in values if isfinite(_number(value)))
    if not sorted_values:
        return 0.0

    index = (percentile / 100.0) * (len(sorted_values) - 1)
    lower = int(index)
    upper = min(lower + 1, len(sorted_values) - 1)
    if lower == upper:
        return sorted_values[lower]
    fraction = index - lower
    return sorted_values[lower] + (sorted_values[upper] - sorted_values[lower]) * fraction


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
    malt = sections.get("max_allowable_loss_time") or {}

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
        "max_allowable_loss_time": {
            "formula": malt.get("formula"),
            "percentile_policy": malt.get("percentile_policy"),
            "rows": (malt.get("rows") or [])[:8],
            "night_exceedances": (malt.get("night_exceedances") or [])[:8],
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


def _daily_folder_utilization_percentages(rows: list[dict[str, Any]], dates: list[str]) -> list[float]:
    rows_by_day = {
        _clean_text(row.get("run_date")): row
        for row in rows
        if row.get("run_date")
    }
    percentages = []
    for day in dates:
        row = rows_by_day.get(day, {})
        utilization_minutes = (
            _number(row.get("runtime"))
            + _number(row.get("downtime"))
            + _loss_time_minutes(row)
        )
        percentages.append(_percentage(utilization_minutes, CAPACITY_MINUTES_PER_FOLDER_DAY))
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
        grouped[folder] = grouped.get(folder, 0.0) + _loss_time_minutes(row)

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


def _clean_complexity_code(segment: dict[str, Any]) -> str:
    code = _clean_text(segment.get("complexity_code")).upper()
    if code.startswith("C") and code[1:].isdigit():
        number = int(code[1:])
        if 1 <= number <= 15:
            return f"C{number}"

    label = _clean_text(segment.get("label")).upper()
    if label.startswith("C") and label[1:].isdigit():
        number = int(label[1:])
        if 1 <= number <= 15:
            return f"C{number}"

    return code if code.startswith("C") else ""


def _complexity_type_from_code(code: str) -> str:
    match = re_fullmatch_complexity(code)
    if not match:
        return ""
    number = int(match)
    if 1 <= number <= 4:
        return "SNP"
    if 5 <= number <= 15:
        return "GNP"
    return ""


def _is_gnp_segment(segment: dict[str, Any]) -> bool:
    code_number = re_fullmatch_complexity(segment.get("complexity_code"))
    if code_number and 5 <= int(code_number) <= 15:
        return True

    text = " ".join(
        [
            _clean_text(segment.get("type")),
            _clean_text(segment.get("category")),
            _clean_text(segment.get("label")),
            _clean_text(segment.get("key")),
        ]
    ).casefold()
    return "gnp" in text


def _complexity_code_sort_key(value: Any) -> tuple[int, str]:
    code_number = re_fullmatch_complexity(value)
    if code_number:
        return int(code_number), _clean_text(value)
    return 999, _clean_text(value)


def re_fullmatch_complexity(value: Any) -> str:
    text = _clean_text(value).upper()
    if text.startswith("C") and text[1:].isdigit():
        number = int(text[1:])
        if 1 <= number <= 15:
            return str(number)
    return ""


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

    model = _get_env("AZURE_MODEL") or _get_env("AZURE_OPENAI_MODEL") or _get_env("AZURE_DEPLOYMENT")
    base: dict[str, Any] = {"messages": messages, "temperature": 0.2}
    if model:
        base["model"] = model

    payloads = [
        {**base, "max_completion_tokens": 1800, "response_format": {"type": "json_object"}},
        {**base, "max_tokens": 1800, "response_format": {"type": "json_object"}},
        {**base, "max_completion_tokens": 1800},
        {**base, "max_tokens": 1800},
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
    model = _get_env("AZURE_MODEL") or _get_env("AZURE_OPENAI_MODEL") or _get_env("AZURE_DEPLOYMENT")
    reasoning_effort = _select_reasoning_effort(messages, model)

    auth_modes = ["api-key", "bearer"]
    last_error: Exception | None = None

    if _should_try_responses_api(endpoint, model):
        response_url = _build_responses_url(endpoint)
        response_urls = [response_url]
        if not _get_env("AZURE_API_VERSION") and "api-version=" in response_url:
            response_urls.append(_without_api_version(response_url))

        for request_url in response_urls:
            for payload in _responses_payloads(messages, model, reasoning_effort, max_output_tokens=20000):
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

    chat_url = _build_chat_completion_url(endpoint)
    chat_urls = [chat_url]
    if not _get_env("AZURE_API_VERSION") and "api-version=" in chat_url:
        chat_urls.append(_without_api_version(chat_url))

    for request_url in chat_urls:
        for payload in _chat_completion_payloads(messages, model, reasoning_effort, max_tokens=6000):
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


def _build_responses_url(endpoint: str) -> str:
    url = endpoint.strip()
    deployment = _get_env("AZURE_DEPLOYMENT") or _get_env("AZURE_DEPLOYMENT_NAME")

    if "/responses" in url:
        return _with_api_version(url)

    if _is_openai_v1_base_url(url):
        return _with_api_version(f"{url.rstrip('/')}/responses")

    if deployment:
        base = url.rstrip("/")
        if not base.endswith("/openai"):
            url = f"{base}/openai/deployments/{quote(deployment)}/responses"
        else:
            url = f"{base}/deployments/{quote(deployment)}/responses"
    else:
        url = f"{url.rstrip('/')}/responses"

    return _with_api_version(url)


def _should_try_responses_api(endpoint: str, model: str) -> bool:
    configured = _get_env("CAPACITY_CHAT_USE_RESPONSES_API") or _get_env("OPENAI_USE_RESPONSES_API")
    if configured:
        return configured.strip().casefold() not in {"0", "false", "no", "off"}

    url = endpoint.strip()
    return "/responses" in url or _is_reasoning_model(model)


def _responses_payloads(
    messages: list[dict[str, str]],
    model: str,
    reasoning_effort: str,
    max_output_tokens: int,
) -> list[dict[str, Any]]:
    instructions, input_items = _messages_to_responses_input(messages)
    base_payload: dict[str, Any] = {
        "input": input_items,
        "max_output_tokens": max_output_tokens,
        "text": {"verbosity": "low"},
    }
    if instructions:
        base_payload["instructions"] = instructions
    if model:
        base_payload["model"] = model

    payloads = []
    if reasoning_effort and reasoning_effort != "none":
        payloads.append({
            **base_payload,
            "reasoning": {"effort": reasoning_effort},
        })
    payloads.append(base_payload)
    return payloads


def _chat_completion_payloads(
    messages: list[dict[str, str]],
    model: str,
    reasoning_effort: str,
    max_tokens: int,
) -> list[dict[str, Any]]:
    base_payload: dict[str, Any] = {"messages": messages}
    if model:
        base_payload["model"] = model

    payloads = []
    if _is_reasoning_model(model) and reasoning_effort and reasoning_effort != "none":
        payloads.append({
            **base_payload,
            "reasoning_effort": reasoning_effort,
            "max_completion_tokens": max_tokens,
        })

    # Try max_completion_tokens first (required by newer models like gpt-4.1),
    # then fall back to max_tokens for older deployments.
    payloads.append({**base_payload, "temperature": 0.2, "max_completion_tokens": max_tokens})
    payloads.append({**base_payload, "temperature": 0.2, "max_tokens": max_tokens})
    return payloads


def _messages_to_responses_input(messages: list[dict[str, str]]) -> tuple[str, list[dict[str, str]]]:
    instructions = ""
    input_items: list[dict[str, str]] = []
    for message in messages:
        role = _clean_text(message.get("role"))
        content = _clean_text(message.get("content"))
        if not content:
            continue
        if role == "system" and not instructions:
            instructions = content
            continue
        if role not in {"user", "assistant"}:
            role = "user"
        input_items.append({"role": role, "content": content})
    return instructions, input_items


def _select_reasoning_effort(messages: list[dict[str, str]], model: str) -> str:
    configured = _get_env("CAPACITY_CHAT_REASONING_EFFORT") or _get_env("OPENAI_REASONING_EFFORT")
    if configured:
        return _normalize_reasoning_effort(configured, model)
    if not _is_reasoning_model(model):
        return ""

    latest_user = ""
    for message in reversed(messages):
        if message.get("role") == "user":
            latest_user = _clean_text(message.get("content")).casefold()
            break

    hard_reasoning_terms = [
        "trend", "interpolate", "interpolation", "forecast", "predict", "projection",
        "why", "reason", "root cause", "cause", "correlation", "relationship",
        "compare", "comparison", "difference", "variance", "pattern", "identify",
        "best", "worst", "optimize", "recommend", "should", "impact",
    ]
    if any(term in latest_user for term in hard_reasoning_terms):
        return "high"
    return "high"


def _normalize_reasoning_effort(value: str, model: str) -> str:
    effort = _clean_text(value).casefold()
    aliases = {
        "off": "none",
        "false": "none",
        "0": "none",
        "min": "minimal",
        "x-high": "xhigh",
        "extra_high": "xhigh",
        "extra-high": "xhigh",
    }
    effort = aliases.get(effort, effort)
    allowed = {"none", "minimal", "low", "medium", "high", "xhigh"}
    if effort not in allowed:
        return "medium" if _is_reasoning_model(model) else ""

    if _is_gpt_51_model(model) and effort in {"minimal", "xhigh"}:
        return "low" if effort == "minimal" else "high"
    return effort


def _is_reasoning_model(model: str) -> bool:
    text = _clean_text(model).casefold()
    if not text:
        return False
    return (
        text.startswith("gpt-5")
        or text.startswith("o1")
        or text.startswith("o3")
        or text.startswith("o4")
    )


def _is_gpt_51_model(model: str) -> bool:
    return _clean_text(model).casefold().startswith("gpt-5.1")


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
    try:
        with urlopen(request, timeout=REQUEST_TIMEOUT_SECONDS) as response:
            body = response.read().decode("utf-8")
    except URLError as exc:
        if not _is_ssl_certificate_verification_error(exc):
            raise

        with urlopen(
            request,
            timeout=REQUEST_TIMEOUT_SECONDS,
            context=ssl._create_unverified_context(),
        ) as response:
            body = response.read().decode("utf-8")
    return json.loads(body)


def _is_ssl_certificate_verification_error(exc: URLError) -> bool:
    reason = getattr(exc, "reason", exc)
    if isinstance(reason, ssl.SSLCertVerificationError):
        return True

    message = str(reason).lower()
    return "certificate_verify_failed" in message or "certificate verify failed" in message


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
