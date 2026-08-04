from __future__ import annotations

import asyncio
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

import httpx


CAPACITY_MINUTES_PER_FOLDER_DAY = 240.0
PF_COMPLIANCE_MINUTES_BY_PLANT = {
    "baroda": 180.0,
    "manesar": 180.0,
    "trivandrum": 150.0,
}
REQUEST_TIMEOUT_SECONDS = int(os.getenv("CAPACITY_CHAT_REQUEST_TIMEOUT_SECONDS", "180") or "180")
CHAT_RESPONSE_MAX_TOKENS = int(os.getenv("CAPACITY_CHAT_RESPONSE_MAX_TOKENS", "4000") or "4000")
CHAT_REASONING_RESPONSE_MAX_TOKENS = int(os.getenv("CAPACITY_CHAT_REASONING_RESPONSE_MAX_TOKENS", "8000") or "8000")
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

# Tolerates common typos ("how may days") so a misspelling doesn't silently fall through a
# keyword-gated deterministic shortcut straight to the full LLM call.
_HOW_MANY_RE = re.compile(r"how\s+(?:many|much|may)\b")
_OUT_OF_SCOPE_CHAT_PATTERNS = [
    re.compile(r"\b(?:python|javascript|java|c\+\+|pygame)\b.*\b(?:code|game|script|program)\b"),
    re.compile(r"\b(?:code|script|program|game)\b.*\b(?:python|javascript|java|c\+\+|pygame)\b"),
    re.compile(r"\b(?:rewrite|rephrase|paraphrase|proofread)\b"),
]


def _asks_how_many(question: str) -> bool:
    return bool(_HOW_MANY_RE.search(question))


def _is_out_of_scope_chat_request(message: str) -> bool:
    """Reject clear general-assistant requests before spending tokens on the decomposer.

    Capacity questions can be phrased very broadly, so this guard intentionally targets only
    explicit non-dashboard tasks observed in production. The final LLM prompt provides the
    broader domain boundary for anything less clear-cut.
    """
    text = _clean_text(message).casefold()
    return bool(text and any(pattern.search(text) for pattern in _OUT_OF_SCOPE_CHAT_PATTERNS))


def _raise_if_chat_cancelled(cancellation_event: asyncio.Event | None = None) -> None:
    if cancellation_event is not None and cancellation_event.is_set():
        raise asyncio.CancelledError()


# ── Conversation state (compact, structured memory instead of raw history turns) ────────────────

_INTENT_KEYWORD_RULES: list[tuple[re.Pattern[str], str]] = [
    (re.compile(r"\btrend|forecast|predict|projection|extrapolat"), "trend"),
    (re.compile(r"\baverage\b|\bavg\b"), "average"),
    (re.compile(r"\bcompare|comparison|\bvs\.?\b|\bversus\b"), "comparison"),
    (re.compile(r"\btop\b|\brank|\bhighest\b|\blowest\b|\bworst\b|\bbest\b"), "ranking"),
    (re.compile(r"\bbreakdown\b|\bcomponents\b|\bsplit\b"), "breakdown"),
]


def _infer_query_intent(message: str, qu_plan: dict[str, Any] | None) -> str:
    """Deterministic, no-extra-LLM-call classification of what kind of question this was.

    Prefers the QU decomposer's own intent field (already computed this turn) and only falls back
    to keyword matching when no plan was produced (force_full_llm or decomposer failure).
    """
    if isinstance(qu_plan, dict):
        intent = _clean_text(qu_plan.get("intent", ""))
        if intent:
            return intent
    question = _clean_text(message).casefold()
    if _asks_how_many(question):
        return "count"
    for pattern, label in _INTENT_KEYWORD_RULES:
        if pattern.search(question):
            return label
    return "lookup"


def _infer_latest_metric(qu_plan: dict[str, Any] | None) -> str:
    """Best-effort human label for what metric this turn was about, from the plan already computed
    this turn — never fabricated, and left blank rather than guessed when no plan exists."""
    if not isinstance(qu_plan, dict):
        return ""
    for metric in qu_plan.get("metrics") or []:
        if isinstance(metric, dict):
            label = _clean_text(metric.get("label") or metric.get("field") or "")
        else:
            label = _clean_text(metric)
        if label:
            return label
    return ""


def _summarize_answer_for_state(answer: str, limit: int = 220) -> str:
    """Deterministic truncation of the real answer just produced — not a fabricated summary."""
    text = _clean_text(answer)
    text = re.sub(r"\|", " ", text)
    text = re.sub(r"[#*_`]", "", text)
    text = re.sub(r"\s+", " ", text).strip()
    if len(text) > limit:
        text = text[:limit].rstrip() + "…"
    return text


def _next_conversation_state(
    selected_plant: str,
    selected_folders: list[str] | None,
    timeframe: dict[str, Any] | None,
    message: str,
    qu_plan: dict[str, Any] | None,
    answer: str,
) -> dict[str, Any]:
    tf = timeframe or {}
    return {
        "selected_plant": _clean_text(selected_plant),
        "selected_folders": [_clean_text(f) for f in (selected_folders or []) if _clean_text(f)],
        "timeframe": {"start": _clean_text(tf.get("start", "")), "end": _clean_text(tf.get("end", ""))},
        "latest_metric_requested": _infer_latest_metric(qu_plan),
        "last_query_intent": _infer_query_intent(message, qu_plan),
        "previous_answer_summary": _summarize_answer_for_state(answer),
    }


def _conversation_state_prompt_block(conversation_state: dict[str, Any] | None) -> str:
    """Renders the PRIOR turn's structured state for prompt injection, replacing reliance on raw
    dialogue for continuity. Explicitly scoped as context-only so the model never treats a stale
    previous_answer_summary as a shortcut instead of recomputing from the current dashboard data."""
    if not isinstance(conversation_state, dict) or not conversation_state:
        return ""
    plant = _clean_text(conversation_state.get("selected_plant", ""))
    folders = [f for f in (conversation_state.get("selected_folders") or []) if _clean_text(f)]
    tf = conversation_state.get("timeframe") or {}
    tf_start = _clean_text(tf.get("start", ""))
    tf_end = _clean_text(tf.get("end", ""))
    metric = _clean_text(conversation_state.get("latest_metric_requested", ""))
    intent = _clean_text(conversation_state.get("last_query_intent", ""))
    summary = _clean_text(conversation_state.get("previous_answer_summary", ""))
    if not any([plant, folders, tf_start, tf_end, metric, intent, summary]):
        return ""
    lines = [
        "CONVERSATION STATE (prior turn — use ONLY to resolve follow-up phrasing such as pronouns "
        "or 'what about X'; NEVER quote its numbers as the answer, always recompute from the "
        "dashboard context below):"
    ]
    if plant:
        lines.append(f"- selected_plant: {plant}")
    if folders:
        lines.append(f"- selected_folders: {', '.join(folders)}")
    if tf_start or tf_end:
        lines.append(f"- timeframe: {tf_start} to {tf_end}")
    if metric:
        lines.append(f"- latest_metric_requested: {metric}")
    if intent:
        lines.append(f"- last_query_intent: {intent}")
    if summary:
        lines.append(f"- previous_answer_summary: {summary}")
    return "\n".join(lines) + "\n\n"


def _qu_decomposer_user_content(message: str, conversation_state: dict[str, Any] | None) -> str:
    """Short one-line conversation hint prefixed onto the decomposer's user message so follow-up
    questions ('and for GNP nights only?') resolve against the right prior entity/metric — the
    decomposer otherwise sees no history at all."""
    clean_message = _clean_text(message)
    if not isinstance(conversation_state, dict) or not conversation_state:
        return clean_message
    parts: list[str] = []
    plant = _clean_text(conversation_state.get("selected_plant", ""))
    folders = [f for f in (conversation_state.get("selected_folders") or []) if _clean_text(f)]
    metric = _clean_text(conversation_state.get("latest_metric_requested", ""))
    intent = _clean_text(conversation_state.get("last_query_intent", ""))
    if plant:
        parts.append(f"plant={plant}")
    if folders:
        parts.append(f"folders={','.join(folders)}")
    if metric:
        parts.append(f"previous_metric={metric}")
    if intent:
        parts.append(f"previous_intent={intent}")
    if not parts:
        return clean_message
    return f"[Conversation context: {'; '.join(parts)}]\nQuestion: {clean_message}"


async def build_chat_response(
    message: str,
    intelligence: dict[str, Any] | None = None,
    summary: dict[str, Any] | None = None,
    daily_rows: list[dict[str, Any]] | None = None,
    details: list[dict[str, Any]] | None = None,
    tower_details: list[dict[str, Any]] | None = None,
    downtime_reasons: list[dict[str, Any]] | None = None,
    book_details: list[dict[str, Any]] | None = None,
    history: list[dict[str, str]] | None = None,
    force_full_llm: bool = False,
    cancellation_event: asyncio.Event | None = None,
    conversation_state: dict[str, Any] | None = None,
    selected_plant: str = "",
    selected_folders: list[str] | None = None,
    timeframe: dict[str, Any] | None = None,
) -> dict[str, Any]:
    _raise_if_chat_cancelled(cancellation_event)
    intelligence = intelligence or {}
    summary = summary or {}
    daily_rows = daily_rows or []
    details = details or []
    tower_details = tower_details or []
    downtime_reasons = downtime_reasons or []
    book_details = book_details or []
    history = history or []
    selected_folders = selected_folders or []

    if _is_out_of_scope_chat_request(message):
        return {
            "answer": (
                "I can only help with this plant-capacity dashboard, its production data, "
                "and related operational analytics."
            ),
            "status": "out_of_scope",
            "detail": "",
            "plan": None,
            "confidence": None,
            "refined": False,
            "llm_used": False,
            "llm_status": "out_of_scope",
            "chart": None,
            "conversation_state": conversation_state,
            "eval_trace": {
                "mode": "out_of_scope",
                "system_prompt": "Request rejected locally as outside the capacity-dashboard scope.",
                "retrieval_context": [],
                "history_turns_used": 0,
            },
        }

    if not (intelligence.get("sections") or {}) and (summary or daily_rows or details):
        intelligence = _build_deterministic_intelligence(
            summary=summary,
            daily_rows=daily_rows,
            folder_rows=details,
            scope_label=_chat_scope_label(daily_rows),
        )

    _raise_if_chat_cancelled(cancellation_event)
    context = _build_chat_context(
        intelligence=intelligence,
        tower_details=tower_details,
        summary=summary,
        daily_rows=daily_rows,
        details=details,
        downtime_reasons=downtime_reasons,
        book_details=book_details,
        question=message,
    )
    endpoint = _get_env("AZURE_ENDPOINT")
    api_key = (
        _get_env("API_KEY")
        or _get_env("AZURE_API_KEY")
        or _get_env("AZURE_OPENAI_API_KEY")
        or _get_env("AZURE_INFERENCE_KEY")
    )

    if not endpoint or not api_key:
        return {
            "answer": "LLM is not configured.",
            "status": "unconfigured",
            "detail": "AZURE_ENDPOINT and API_KEY/AZURE_API_KEY are required.",
            "plan": None,
            "llm_used": False,
            "llm_status": "unconfigured",
            "refined": False,
            "chart": None,
            "conversation_state": conversation_state,
            "eval_trace": {
                "mode": "unconfigured",
                "system_prompt": "LLM is not configured; no system prompt was used.",
                "retrieval_context": [],
                "history_turns_used": 0,
            },
        }

    # Phase 1 — Query Understanding: decompose the question into a rich structured plan,
    # then execute it deterministically. Only falls through to the full LLM when the executor
    # can't produce a confident answer (trend/prediction intents, unresolvable fields, etc.).
    # Still runs when force_full_llm=True — that flag only skips the deterministic fast-path
    # ANSWER below, not this plan. Skipping the decomposer entirely here used to mean every
    # forced-full-LLM question (the frontend's default chat mode) had no required_sources to size
    # context by, so every table fell back to a small stub/no-plan cap regardless of what the
    # question actually needed — the exact "capping causes wrong answers" bug, in the app's
    # default mode. The plan is now used purely for context sizing and prompt guidance here.
    qu_plan: dict[str, Any] | None = None
    if _should_use_chat_planner(message):
        try:
            _raise_if_chat_cancelled(cancellation_event)
            decomposer_input = _qu_decomposer_user_content(message, conversation_state)
            qu_plan = await _call_qu_decomposer_async(decomposer_input, endpoint, api_key, cancellation_event=cancellation_event)
            qu_plan = _normalize_qu_plan_for_question(qu_plan, message, context)
        except Exception as qu_exc:
            print(f"[chat] QU decomposer skipped: {_sanitize_error_message(qu_exc, api_key)}", flush=True)
            qu_plan = None

    if not force_full_llm:
        _raise_if_chat_cancelled(cancellation_event)
        qu_answer = _execute_qu_plan(qu_plan, message, context)
        if qu_answer:
            confidence, conf_reasons = _compute_qu_confidence(qu_plan, qu_answer, context, message)
            if _chat_debug_enabled():
                print(f"[chat] QU confidence={confidence} reasons={conf_reasons}", flush=True)
            if confidence >= _QU_CONFIDENCE_THRESHOLD:
                return {
                    "answer": qu_answer,
                    "status": "ok",
                    "plan": qu_plan,
                    "confidence": confidence,
                    "refined": False,
                    "llm_used": False,
                    "llm_status": "fast_path",
                    "chart": _chart_for_answer(qu_answer, message, context, history, qu_plan),
                    "conversation_state": _next_conversation_state(
                        selected_plant, selected_folders, timeframe, message, qu_plan, qu_answer
                    ),
                    "eval_trace": {
                        "mode": "fast_path",
                        "system_prompt": "Deterministic fast path; no LLM system prompt was used.",
                        "retrieval_context": [
                            _chat_context_to_toon(_compact_chat_context_for_llm(context, message, qu_plan=qu_plan))
                        ],
                        "history_turns_used": 0,
                    },
                }
            # Below threshold — fall through to full LLM for a better answer
            if _chat_debug_enabled():
                print(f"[chat] QU confidence {confidence} below threshold {_QU_CONFIDENCE_THRESHOLD}, escalating to full LLM", flush=True)
    else:
        if _chat_debug_enabled():
            print(
                "[chat] force_full_llm=True — using the QU plan for focused retrieval and "
                "skipping only the deterministic answer",
                flush=True,
            )

    _raise_if_chat_cancelled(cancellation_event)
    plan_section = ""
    if qu_plan:
        plan_section = (
            "AGENT PLAN — execute this exactly before answering:\n"
            f"{json.dumps(qu_plan, indent=2)}\n\n"
            "EXECUTION STEPS:\n"
            "1. Go to the primary_source listed in the plan and locate the relevant rows/fields\n"
            "2. Apply any filters from the plan (folder name, date, complexity, etc.)\n"
            "3. Perform the computation described step by step, quoting exact values\n"
            "4. Self-validate internally before answering: check values are non-negative where expected; "
            "verify Utilized Time = Runtime (SNP + GNP) + Overrun + Lost Time + Wait Time + Downtime; "
            "spot-check that runtime + loss + downtime + wait + spare ≈ available_capacity. "
            "Do not reveal private reasoning; only return the answer.\n"
            "5. Format your response as specified in output_format. For day/night/date counts, "
            "return the distinct count and every authoritative evidence row as a Markdown table; "
            "never return only a single value.\n\n"
        )

    authoritative_date_count = _date_count_evidence_for_plan(qu_plan, message, context)
    llm_context = _compact_chat_context_for_llm(context, message, qu_plan=qu_plan)
    if authoritative_date_count:
        llm_context["authoritative_result"] = {
            key: value
            for key, value in authoritative_date_count.items()
            if key not in {"answer", "required_values"}
        }
    context_text = _chat_context_to_toon(llm_context)
    if len(context_text) > 650_000:
        llm_context = _minimal_chat_context_for_llm(context, qu_plan=qu_plan)
        if authoritative_date_count:
            llm_context["authoritative_result"] = {
                key: value
                for key, value in authoritative_date_count.items()
                if key not in {"answer", "required_values"}
            }
        context_text = _chat_context_to_toon(llm_context)

    system_content = (
        f"{plan_section}"
        "You are a concise analytics assistant exclusively for this print-plant capacity dashboard. "
        "Only answer questions about the supplied dashboard data, its metrics and formulas, print "
        "production, capacity, and related operational analysis. Politely refuse unrelated coding, "
        "games, general writing, rewriting, or other general-assistant requests. "
        "For numerical or data-specific questions, answer ONLY from the TOON dashboard context supplied — never invent values. "
        "For conceptual, definitional, or formula questions (e.g. 'what does X mean', 'what is the formula for Y', 'how is Z calculated', 'explain X'), answer from your domain knowledge — do not say the data is absent. "
        "Use the curated computed tables supplied here "
        "(exact_dashboard.folders, exact_dashboard.daily, towers, tower_runtime_segments, tower_runtime_mix, tower_availability, downtime_by_reason, "
        "delayed_pf, editions_* tables, book_details) before using summary aggregates. "
        "Prefer exact_dashboard values over derived summaries whenever a numeric answer is available. "
        "Before responding, internally identify the metric, filters, numerator, denominator, and formula. "
        "Validate the arithmetic against the supplied context, then provide only the final concise answer. "
        "Be brief and direct — no preamble, no filler. "
        "Always report duration values in minutes. Do not convert durations into hours or h:mm. "
        "Clock times such as 03:00 or 04:00 may remain clock times. "
        "If a specific numerical answer is genuinely absent from the data, say: Not available in the current data. "
        "Never say that for conceptual, formula, or terminology questions — answer those from your knowledge.\n\n"

        "TOON CONTEXT FORMAT:\n"
        "- The dashboard data below is TOON-style structured text, not JSON.\n"
        "- Nested sections use indentation. A line like key: value is a scalar field.\n"
        "- A block like rows[3]{a,b,c}: means 3 table rows; each following indented CSV row maps to columns a,b,c in order.\n"
        "- Quoted cells are strings. null, true, false, and numbers keep their normal meanings. JSON snippets inside a cell are a single cell value.\n"
        "- You can filter, group, count, sum, average, rank, and compare these rows exactly as tabular data.\n\n"
        "- When selected_sources is present, its keys are the exact table names selected by the "
        "query plan, and its rows have already been filtered by the plan's conditions, timeframe, "
        "and entities. source_selection reports the original and selected row counts.\n\n"
        "- When authoritative_result is present, its distinct_date_count and rows were computed "
        "deterministically from the filtered source. Reproduce that count and every row in a "
        "Markdown table. Do not substitute a single-value response or recompute a different result.\n\n"

        "OUTPUT FORMATTING (the response is rendered as Markdown, so use it deliberately):\n"
        "- Any answer with 2+ rows of comparable data (rankings, breakdowns by folder/tower/date/reason, "
        "multi-metric comparisons) MUST be a GitHub-flavored Markdown table with a header row and a "
        "'| --- | --- |' separator row — never a bullet or numbered list of 'label: value' pairs for that case.\n"
        "- A single headline number gets one short sentence, with the number itself in **bold**.\n"
        "- Use **bold** for the specific figures and labels that directly answer the question, not for whole sentences.\n"
        "- Reserve numbered/bulleted lists for short sequences that aren't tabular (e.g. steps, plain names).\n"
        "- Never wrap the whole answer in a single bullet, and never emit raw pipe characters outside a "
        "real Markdown table.\n\n"

        "QUERY INTERPRETATION RULES:\n"
        "- 'runtime' / 'run time' with no qualifier: total aggregate runtime across ALL complexity types. "
        "Report the combined figure first. Break down by SNP/GNP only if the user explicitly says 'SNP runtime', 'GNP runtime', or 'by type'.\n"
        "- 'SNP' / 'standard' as a base category: include SNP and SNP Complex, codes C1-C4, unless the user explicitly separates simple vs complex.\n"
        "- 'GNP' / 'glossy' / 'UV' as a base category: include GNP and GNP Complex, codes C5-C15, unless the user explicitly separates simple vs complex.\n"
        "- 'SNP runtime': sum C1-C4 by default. 'GNP runtime': sum C5-C15 by default.\n"
        "- For runtime percentages across tower types and product types, use tower_runtime_mix. "
        "Example: percentage of GNP/UV tower runtime used for SNP products = "
        "SNP runtime_min where tower_type_key='gnp_uv' / All runtime_min where tower_type_key='gnp_uv' * 100.\n"
        "- When tower_runtime_segments is present, it is the filtered segment-level source for tower/product runtime math. "
        "Use its minutes, print_order, committed_speed_cph, actual_speed_cph, and efficiency_pct fields to recompute or validate numerator/denominator rather than saying segment data is absent.\n"
        "- For GNP-vs-SNP folder/edition questions about average spare, loss, wait, LPR-to-print-start, reflong, downtime, delayed finish, or minimum-3-GNP-folder nights, use gnp_snp_folder_analysis first. "
        "Its comparison_by_product_type, gnp_loss_breakdown_by_folder, nights_with_min_3_gnp_folders, and delayed_finish_complexity tables are precomputed from the data. "
        "For web-break comparisons on named towers, use web_break_gnp_snp_tower_comparison; if can_split_web_break_by_product_type=false, explicitly state that web-break events are not stored with product type and compare against GNP/SNP runtime mix only.\n"
        "- For '<metric> on days this folder ran GNP/GNP Complex/SNP' questions, use "
        "exact_dashboard.folder_days and the FOLDER-SPECIFIC flags folder_has_gnp, "
        "folder_has_gnp_complex, folder_has_snp, or folder_has_snp_only. Never use the plant-level "
        "plant_night_type/plant_gnp_night fields for that question. Use plant_night_type only when "
        "the user explicitly asks whether the plant as a whole ran GNP that night.\n"
        "- For any efficiency question involving DATES, DAYS, or THRESHOLDS (e.g. 'days below 90% efficiency', 'average efficiency per night', 'trend of efficiency'), "
        "use daily_efficiency. It has one row per production night with run_date, efficiency_pct, total_po, total_runtime_min, total_dt_min, actual_speed_cph, committed_speed_cph. "
        "Filter rows by efficiency_pct threshold, count matching rows, or group by month/weekday from run_date.\n"
        "- 'complex runtime': sum entries where is_complex=true (C4 + C9–C15).\n"
        "- 'speed' / 'average speed' with no qualifier: overall average_speed_cph. Qualify by type only when asked.\n"
        "- 'lost time' / 'losses': total lost_time (changeover + late-start + reflong). Waiting time is always separate.\n"
        "- 'unscheduled time' means Unplanned Time, not Lost Time. Unplanned Time is capacity where the folder/tower was not scheduled or available for production.\n"
        "- 'spare time' / 'spare capacity': always buffer_time (= spare_time_min in exact_dashboard.folders), never unplanned_time.\n"
        "- 'wait-time percentage' / 'waiting-time percentage': Waiting Time / Available Capacity * 100. "
        "For a folder-night, use available_capacity_min (normally 240 minutes) as the denominator. "
        "Never divide waiting time by runtime + waiting time.\n"
        "- 'average spare time per folder' or 'spare time for each folder': use exact_dashboard.folders[].spare_time_min / active_nights. "
        "List every folder with its average spare time per active night in minutes.\n"
        "- 'utilized time' / 'utilised time' with no qualifier: Utilized Time = Runtime (SNP + GNP) + Overrun "
        "(delayed print-finish minutes) + Lost Time + Wait Time + Downtime. Spare time and unplanned time are excluded.\n"
        "- 'utilization' / 'utilisation' / 'capacity utilization' with no qualifier: "
        "Capacity Utilization % = Utilized Time ÷ Available Time × 100. Calculated at folder level only — towers do not "
        "have a utilization percentage.\n"
        "- 'downtime': mechanical stoppage time, not lost time and not waiting time.\n"
        "- Tower questions: always check towers, tower_runtime_mix, tower_availability, "
        "tower_downtime_reason_attribution, and editions_by_tower before saying data is unavailable. "
        "For reason-specific tower questions such as web break, use tower_downtime_reason_attribution.\n"
        "- When the user uses a shorthand metric name without qualification, default to the aggregate and "
        "mention if a breakdown by type/folder is also available.\n\n"

        "OPERATIONAL MODEL:\n"
        "- A plant has one or more Machines. Each Machine has multiple Towers (the units that print pages) and "
        "Folders (the units that receive printed output from towers and fold it into finished copies).\n"
        "- There is NO fixed Tower-Folder mapping. Tower-Folder combinations are configured per edition print and "
        "can differ between editions on the same machine — never assume a tower always feeds the same folder; "
        "read towers_list/Folder per row instead.\n"
        "- A Folder receives output from one Tower-Folder combination at a time, never from multiple simultaneously.\n"
        "- Printing is Parallel when different editions run at the same time on different Tower-Folder combinations "
        "on the same machine, and Sequential when editions share the same Tower-Folder combination and must run "
        "one after another (a combination can't start a new edition until the previous one on it finishes). "
        "Only state which mode applied when Start/End times in the data actually show it — don't assume.\n"
        "- An edition print is one row identified by IssueID (one IssueID = one edition printed on one date, "
        "with its own Towers list and one Folder). Lifecycle: editorial releases the digital page (Last Tiff) → "
        "machine/Tower-Folder prep → Start Time → End Time, with Print Order (copies) and Total Pages as "
        "edition attributes.\n"
        "- In book_details, the raw 'Total Downtime' field INCLUDES Reflong time. True mechanical downtime for "
        "that row = Total Downtime − Reflong time; don't quote book_details' Total Downtime as Downtime "
        "without that adjustment (the dashboard's own downtime_min already applies it).\n\n"

        "OPERATING DEFINITIONS:\n"
        "- Wait Time: idle time at the start of the 00:00 window where the press cannot operate because editorial LPR has not been issued. "
        "Wait ends when LPR is issued. If an earlier edition finishes before LPR for the next edition, the PF-to-LPR gap also counts as Wait.\n"
        "- Lost Time: preparation time after editorial release and before printing. Components are Makeready/LPR-to-Press-Start, Changeover/PF-to-Press-Start when physical change is required, and Reflong changeover losses.\n"
        "- Downtime: unplanned stoppages during an active run.\n"
        "- Run Time: net productive print time. For editions already printing before midnight, count only the portion from midnight to Print Finish.\n"
        "- Spare Time: unused capacity inside the reference window after all other components are accounted for. "
        "Formula: Spare Time = 240 - (Wait + Loss + Downtime + Run). It cannot be negative.\n"
        "- Unplanned Time: periods where the folder or tower was not scheduled or available for production.\n"
        "- Utilized Time: Runtime (SNP + GNP) + Overrun (delayed print-finish minutes) + Lost Time + Wait Time + Downtime. "
        "Do not include Spare Time or Unplanned Time.\n"
        "- Capacity Utilization % / Utilisation: Utilized Time ÷ Available Time × 100. Folder level only — "
        "there is no tower-level utilization percentage.\n"
        "- Spare Capacity: (Spare Time / (Total Available Time - Unplanned Time)) * 100.\n"
        "- Speed Efficiency / Efficiency %: measures how fast the machine actually ran relative to its committed speed. "
        "Calculated in four steps:\n"
        "  1. Speed per Minute = Avg Speed (CPH) ÷ 60\n"
        "  2. Apportioned PO (per slot) = Speed per Minute × clipped runtime minutes in that slot. "
        "Slot rules: post-midnight slot counts only minutes after 00:00; editions crossing print finish time are clipped at that time.\n"
        "  3. Actual Speed = Total PO ÷ (Total Runtime + Total Downtime) — converting runtime+DT to hours to match CPH units.\n"
        "  4. Efficiency % = Actual Speed ÷ Committed Speed × 100.\n"
        "Important: committed speed is only the denominator/target in step 4; never use committed speed to calculate "
        "Speed per Minute, Apportioned PO, or Total PO. Those use Avg Speed.\n"
        "In the data, efficiency_pct in tower_runtime_segments is pre-computed using these formulas. "
        "Use it directly for segment-level efficiency questions; for period/folder/plant-level efficiency, "
        "sum the segment PO and runtime+DT values then reapply step 3–4.\n"
        "- Print Finish Time vs Delayed Print Finish — these are NOT the same thing: "
        "Print Finish Time is the actual clock time printing ended for an edition/folder/night, "
        "regardless of whether that was on time or late — EVERY active night has one. "
        "Delayed Print Finish (delayed_pf) is the SUBSET of those finishes that crossed the plant's "
        "compliance cutoff window (04:00 default; 03:00 Baroda/Manesar; 02:30 Trivandrum) — only late "
        "nights appear there. If asked for 'the print finish time' / 'when did printing finish' without "
        "the word 'delayed', use print_finish_time (exact_dashboard.daily / exact_dashboard.folder_days), "
        "never delayed_pf — delayed_pf would silently omit every on-time night.\n\n"

        "MULTI-PART AND CORRELATION QUESTIONS:\n"
        "When a question contains multiple '?' or asks several things in sequence (e.g. 'X? Any correlation with Y? Provide Z'), "
        "treat it as N separate sub-questions and answer each one in order under a clear heading. "
        "Do not collapse them into a single number. Structure: answer sub-question 1 fully → answer sub-question 2 → etc.\n"
        "For delayed print finish questions: use gnp_snp_folder_analysis.delayed_finish_complexity — it already has "
        "run_date, folder, print_finish_time, overrun_minutes, complexity_codes, complexity_categories, largest_components, editions per delayed night. "
        "List every delayed night as a table row, then summarise which complexity categories appear most in delayed nights.\n"
        "For 'average spare time when minimum N GNP folders are running': use gnp_snp_folder_analysis.nights_with_min_3_gnp_folders — "
        "it has one row per qualifying night with avg_spare_time_min already computed. Average that column across all rows.\n"
        "For delay reasons on specific nights: match the run_date values from delayed_pf or delayed_finish_complexity against "
        "loss_time.all_days (which has dominant_driver per date) and downtime_by_reason (overall top reasons).\n\n"

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
        "- utilization_pct / utilization_percentage: (Runtime (SNP + GNP) + Overrun + Lost Time + Wait Time + Downtime) "
        "÷ possible capacity (incl. unplanned nights). Folder level only.\n"
        "- active_day_utilization_pct: same formula, but capacity/Wait/Lost/Downtime are scoped to nights the folder was active only\n"
        "- runtime_minutes / runtime_min: actual print runtime (all complexity types combined)\n"
        "- lost_time_min / lost_time_minutes: Lost Time = changeover + late-start + reflong ONLY. "
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
        "Rows include night_type (GNP/UV or SNP/non-UV, precomputed per row — use this directly for GNP-vs-SNP-night "
        "overrun comparisons, no join needed), cutoff_time, estimated_print_finish_time, editions, complexity_codes, "
        "runtime/loss/downtime/wait/spare, and largest_components. "
        "Use for any delayed PF, print finish, threshold breach, late finish, or overrun question.\n"
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
        "- downtime_by_folder: total downtime incident count and minutes per folder (machine/folder unit), "
        "sorted by incident_count descending. Use for 'frequency of downtime in each folder' or 'which folder has most incidents'.\n"
        "- editions_by_date: unique edition names printed on each date. "
        "Use for date/night edition-list questions.\n"
        "- editions_by_folder: unique edition names printed per folder across the period. "
        "Each entry has folder, editions (list), edition_count. "
        "Use for 'what editions ran on folder X' or 'which folder printed edition Y'.\n"
        "- towers: per-tower totals across the full period. Fields: tower, machine, tower_name, runtime_min, "
        "downtime_min, loss_time_min (changeover + late-start + reflong), waiting_time_min, change_over_time_min, "
        "late_start_time_min, reflong_downtime_min, spare_time_min, active_nights, "
        "downtime_run_count, loss_time_run_count, waiting_time_run_count, uv_tower, active_dates, folders, "
        "editions, complexity_codes. Towers have NO utilization percentage — capacity utilization is folder-level only "
        "(see exact_dashboard.folders/folder_days). USE THIS for any per-tower metric totals or averages "
        "(e.g. 'average lost time per tower' = loss_time_min / active_nights).\n"
        "- tower_days: per-tower PER-DATE rows. Fields: run_date, weekday (Monday-Sunday, precomputed — never "
        "compute weekday yourself from run_date), month (YYYY-MM, precomputed), plant, machine, tower, "
        "tower_name, folder, uv_tower, runtime_min, downtime_min, loss_time_min, waiting_time_min, "
        "spare_time_min, change_over_time_min, late_start_time_min, reflong_time_min, "
        "complexity_codes, complexity_categories, editions. "
        "Use this for a specific-date or per-tower-per-night tower question. On large datasets this table "
        "is row-capped and may not include every tower/date — for weekday or month PATTERN questions use "
        "tower_weekday_summary / tower_month_summary instead, which are small, always-complete pre-aggregated tables.\n"
        "- tower_weekday_summary: per-tower per-weekday AVERAGES, already aggregated (at most towers x 7 rows, "
        "always complete regardless of dataset size). Fields: tower, tower_name, weekday, night_count, "
        "avg_runtime_min, avg_downtime_min, avg_loss_time_min, avg_waiting_time_min. "
        "USE THIS for ANY weekday-wise/day-of-week tower pattern question — do not group tower_days yourself.\n"
        "- tower_month_summary: per-tower per-month TOTALS/AVERAGES, already aggregated (towers x number of "
        "months, always complete regardless of dataset size). Fields: tower, tower_name, month, night_count, "
        "total_runtime_min, total_downtime_min, total_loss_time_min, total_waiting_time_min. "
        "USE THIS for ANY tower-level month-on-month or monthly trend question — do not group tower_days yourself.\n"
        "- tower_availability: total_towers, total_days, active_towers_by_day, and percent-threshold summaries. "
        "Use this for 'how many towers', 'how many days at least X% towers were utilised', or tower availability questions.\n"
        "- tower_usage_distribution: histogram of active tower counts by day. Fields: towers_used, day_count, dates. "
        "Use this for charts or tables where X axis is towers used and Y axis is number of days.\n"
        "- tower_runtime_mix: generic runtime mix by tower type and product type. Fields: tower_type_key, tower_type, "
        "product_type, runtime_min, share_of_tower_type_runtime_pct, tower_day_count, tower_count, towers. "
        "Use this for runtime share/percentage questions involving SNP/GNP products on GNP/UV or non-UV towers.\n"
        "- gnp_snp_folder_analysis: precomputed folder-night comparisons between base GNP (C5-C15) and base SNP (C1-C4). "
        "Use this for questions comparing GNP vs SNP editions on spare time, lost time, wait time, LPR-to-print-start, reflong, downtime, and delayed print finish. "
        "Tables: comparison_by_product_type, gnp_loss_breakdown_by_folder, nights_with_min_3_gnp_folders, delayed_finish_complexity, web_break_gnp_snp_tower_comparison.\n"
        "- tower_downtime_reason_attribution: folder-level downtime reason events attributed to towers that ran the same plant/machine/folder in the selected period. "
        "Use this for questions like web break frequency by individual tower. State that reason attribution is folder-to-tower attribution when giving reason-specific tower counts.\n"
        "- editions_by_tower: unique edition names printed per tower across the period. "
        "Each entry has tower, editions (list), edition_count. Use for 'what editions ran on tower X'.\n"
        "- book_details: per-print-job rows from the master view (Book Wise Details joined with General and Down Time). "
        "Fields: IssueID, Report Date, Run Date, Edition, Products (from General), Machine, Folder, Plant Name, "
        "Total Run Time (mnts), Total Downtime, towers_list (list of tower names), towers_str (flat string), "
        "total_pages (number of pages / pagination for that edition, from the General sheet's 'Sum of Pages' column), "
        "downtime_total_min, downtime_count, downtime_departments. "
        "Use for edition-level queries: which towers an edition used, what product ran on which folder, "
        "how much downtime a specific edition had, or any pagination / page-count questions.\n"
        "- exact_dashboard.daily: per-date rows with run_date, weekday (Monday-Sunday, precomputed), "
        "month (YYYY-MM, ALREADY PRECOMPUTED — group by this field directly for month-on-month/monthly trend "
        "questions; never refuse for lack of a month rollup and never derive it yourself), runtime_min, "
        "utilization_pct, loss_time_min, spare_time_min, night_type, complexity_codes, editions, "
        "print_finish_time (HH:MM, the ACTUAL last print finish across all folders that night, populated "
        "for every active night — use this for 'what time did printing finish' questions, NOT delayed_pf), "
        "last_edition / last_edition_name / last_folder (which edition/folder produced that last finish) "
        "— use this for trend analysis, extrapolation, and weekday-wise or month-wise plant-level questions.\n"
        "- exact_dashboard.folder_days: exactly one row per folder and run_date. Fields: plant, machine, "
        "folder_name, folder, run_date, weekday "
        "(Monday-Sunday, precomputed), month (YYYY-MM, ALREADY PRECOMPUTED), active_night, "
        "runtime_min, loss_time_min, waiting_time_min, downtime_min, spare_time_min, unplanned_time_min, "
        "utilization_pct, spare_capacity_pct, complexity_codes, editions, print_finish_time (HH:MM, that "
        "folder's actual finish time that night, populated whether on time or late), last_edition / "
        "last_edition_name (which edition produced it), delayed_print_finish (bool), overrun_minutes, "
        "folder_has_gnp (that folder ran any C5-C15), folder_has_gnp_complex (that folder ran any "
        "C9-C15), folder_has_snp (that folder ran any C1-C4), folder_has_snp_only, "
        "folder_product_types, waiting_time_pct (= waiting_time_min / available_capacity_min * 100), "
        "plant_night_type and plant_gnp_night. "
        "Use for day-by-day breakdown within a folder, weekday-wise or month-wise per-folder questions, to filter by a specific date, "
        "or to filter a per-folder metric by what that specific folder printed. Do not use "
        "plant_night_type for 'days this folder ran GNP/SNP'. After filtering a named folder, never "
        "repeat a run_date in the answer.\n"
        "- loss_time.all_days: per-date plant-level loss breakdown. Fields: run_date, weekday (precomputed), "
        "month (YYYY-MM, ALREADY PRECOMPUTED), runtime_min, lost_time_min (= loss_time_min), waiting_time_min, "
        "available_capacity_min, loss_pct, dominant_driver, loss_components (a dict keyed by component name — "
        "changeover/late-start/reflong — with minutes for that date), top_folders_by_loss. "
        "Use this for any question about the COMPONENTS of lost time (changeover vs late-start vs reflong) over "
        "time, including month-on-month or weekday-wise component trends — group rows by month or weekday and "
        "sum each key inside loss_components.\n"
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
        "(4) Unqualified metric names → aggregate totals first. "
        "(5) When comparing plants, machines, towers, folders, editions, or categories, always name the metric the "
        "comparison is based on. "
        "(6) Wait-time percentage uses available_capacity_min as its denominator, never runtime + waiting time. "
        "(7) For 'why' questions, separate observation from cause: an observation (e.g. 'lost time was higher on "
        "this folder') can be drawn directly from the data; only state a root cause if the data explicitly shows "
        "it (e.g. a specific downtime reason). If several explanations are possible, say what the data suggests "
        "without presenting speculation as settled fact.\n\n"
        f"{_conversation_state_prompt_block(conversation_state)}"
        f"Dashboard context (TOON):\n{context_text}"
    )

    messages: list[dict[str, str]] = [{"role": "system", "content": system_content}]
    history_char_limit = 800
    history_turns_used = 0
    for turn in (history or [])[-3:]:
        role = _clean_text(turn.get("role", ""))
        content = _clean_text(turn.get("content", ""))
        if role in ("user", "assistant") and content:
            if len(content) > history_char_limit:
                content = content[:history_char_limit] + "… [truncated]"
            messages.append({"role": role, "content": content})
            history_turns_used += 1
    messages.append({"role": "user", "content": _clean_text(message)})
    eval_trace = {
        "mode": "full_llm",
        "system_prompt": system_content,
        "retrieval_context": [context_text],
        "history_turns_used": history_turns_used,
    }

    try:
        _raise_if_chat_cancelled(cancellation_event)
        answer = (await _call_plain_chat_completion_async(
            endpoint,
            api_key,
            messages,
            cancellation_event=cancellation_event,
        )).strip()
        if _chat_debug_enabled():
            print(f"[chat-debug] raw_answer={answer!r}", flush=True)
        authoritative_fallback = bool(
            authoritative_date_count
            and not _date_count_answer_is_complete(answer, authoritative_date_count)
        )
        if authoritative_fallback:
            answer = _clean_text(authoritative_date_count.get("answer"))
        _raise_if_chat_cancelled(cancellation_event)
        return {
            "answer": answer,
            "status": "ok",
            "plan": qu_plan,
            "refined": True,
            "llm_used": True,
            "llm_status": (
                "authoritative_fallback"
                if authoritative_fallback
                else ("weak_answer" if _is_weak_chat_answer(answer) else "answered")
            ),
            "chart": _chart_for_answer(answer, message, context, history, qu_plan),
            "conversation_state": _next_conversation_state(
                selected_plant, selected_folders, timeframe, message, qu_plan, answer
            ),
            "eval_trace": eval_trace,
        }
    except Exception as exc:
        detail = _sanitize_error_message(exc, api_key)
        if _chat_debug_enabled():
            print(f"[chat] full LLM request failed: {_chat_error_kind(exc)} — {detail}", flush=True)
        if authoritative_date_count:
            answer = _clean_text(authoritative_date_count.get("answer"))
            return {
                "answer": answer,
                "status": "ok",
                "detail": detail,
                "plan": qu_plan,
                "llm_used": False,
                "llm_status": "authoritative_fallback",
                "refined": False,
                "chart": _chart_for_answer(answer, message, context, history, qu_plan),
                "conversation_state": _next_conversation_state(
                    selected_plant, selected_folders, timeframe, message, qu_plan, answer
                ),
                "eval_trace": eval_trace,
            }
        if _is_timeout_error(exc):
            return {
                "answer": (
                    "The full AI request timed out before producing an answer. "
                    "Please try again with a narrower plant, date range, folder, tower, or metric."
                ),
                "status": "timeout",
                "detail": detail,
                "plan": qu_plan,
                "llm_used": False,
                "llm_status": "timeout",
                "refined": False,
                "chart": None,
                "conversation_state": conversation_state,
                "eval_trace": eval_trace,
            }
        return {
            "answer": "The full AI request failed before producing an answer.",
            "status": "llm_error",
            "detail": detail,
            "plan": qu_plan,
            "llm_used": False,
            "llm_status": "failed",
            "refined": False,
            "chart": None,
            "conversation_state": conversation_state,
            "eval_trace": eval_trace,
        }


_PLANNER_SCHEMA = """\
DATA SOURCES (use source_key exactly as shown):

exact_dashboard.folders — per-folder aggregated totals across all dates
  Fields: resource (folder display name), runtime_min, loss_time_min, waiting_time_min,
  downtime_min, spare_time_min, unplanned_time_min, possible_capacity_min,
  active_nights, total_nights, utilization_pct, active_day_utilization_pct, spare_capacity_pct

exact_dashboard.folder_days — per-folder per-date rows (use for day-level breakdown, weekday-wise, month-wise, or specific date)
  Fields: plant, machine, folder_name, folder, run_date, weekday (Monday-Sunday, ALREADY PRECOMPUTED — use this field directly,
  never derive weekday from run_date yourself), month (YYYY-MM, ALREADY PRECOMPUTED — use this field
  directly for month-on-month grouping, never derive it yourself), active_night, runtime_min, loss_time_min,
  waiting_time_min, downtime_min, spare_time_min, unplanned_time_min,
  waiting_time_pct (= waiting_time_min / available_capacity_min * 100), utilization_pct,
  spare_capacity_pct, complexity_codes, complexity_categories,
  folder_has_gnp, folder_has_gnp_complex, folder_has_snp, folder_has_snp_only, folder_product_types,
  plant_night_type, plant_gnp_night, editions, print_finish_time (HH:MM — the
  ACTUAL print finish time for that folder that night, populated whether on time or late; use this
  for "print finish time" questions, NOT delayed_pf which only has the late subset), last_edition,
  last_edition_name (which edition produced that finish), delayed_print_finish (bool), overrun_minutes

exact_dashboard.daily — per-date plant-level totals (use for daily trends, weekday-wise, month-wise, or plant-wide day queries)
  Fields: run_date, weekday (Monday-Sunday, ALREADY PRECOMPUTED), month (YYYY-MM, ALREADY PRECOMPUTED —
  use directly for month-on-month trend questions), runtime_min, loss_time_min, waiting_time_min,
  downtime_min, spare_time_min, utilization_pct, night_type, print_finish_time (HH:MM — the ACTUAL
  latest print finish across ALL folders that night, populated for every active night; use this for
  "what time did printing finish" questions, NOT delayed_pf), last_edition, last_edition_name,
  last_folder (which edition/folder produced that last finish)

loss_time.all_days — per-date plant-level loss breakdown (use for month-on-month or weekday-wise
  trend questions about the COMPONENTS of lost time: changeover, late-start, reflong)
  Fields: run_date, weekday (ALREADY PRECOMPUTED), month (YYYY-MM, ALREADY PRECOMPUTED), runtime_min,
  lost_time_min (= loss_time_min), waiting_time_min, available_capacity_min, loss_pct, dominant_driver,
  loss_components (dict keyed by component name with minutes for that date), top_folders_by_loss

towers — per-tower aggregated totals across all dates. Towers have NO utilization percentage — capacity
  utilization is calculated at folder level only (see exact_dashboard.folders/folder_days above).
  Fields: tower, machine, tower_name, runtime_min, downtime_min, loss_time_min, waiting_time_min,
  change_over_time_min, late_start_time_min, reflong_downtime_min, spare_time_min,
  active_nights, downtime_run_count, loss_time_run_count, waiting_time_run_count,
  uv_tower, folders, editions, complexity_codes

	tower_days — per-tower per-date rows (use for specific-date or per-tower-per-night questions; for
	  weekday PATTERN questions use tower_weekday_summary instead — tower_days is row-capped on large
	  datasets and may not cover every tower/weekday)
	  Fields: tower, run_date, weekday (Monday-Sunday, ALREADY PRECOMPUTED — use this field directly,
	  never derive weekday from run_date yourself), month (YYYY-MM, ALREADY PRECOMPUTED), plant, machine,
	  tower_name, folder, uv_tower, runtime_min, downtime_min, loss_time_min, waiting_time_min,
	  spare_time_min, change_over_time_min, late_start_time_min, reflong_time_min,
	  complexity_codes, complexity_categories, editions

	tower_runtime_segments — segment-level runtime rows included only when the question needs tower/product runtime math
	  Fields: run_date, tower, tower_type_key (gnp_uv/non_uv), tower_type, uv_tower, product_type (SNP/GNP/Unknown),
	  complexity_code, category, minutes, print_order, source_print_order, committed_speed_cph, actual_speed_cph,
	  efficiency_pct. Use this to calculate or validate tower runtime percentages by product type and speed efficiency.

tower_weekday_summary — per-tower per-weekday AVERAGES, already aggregated (towers x at most 7 rows,
  always complete regardless of dataset size). Use this for any "weekday wise" / day-of-week tower
  pattern question instead of grouping tower_days yourself.
  Fields: tower, tower_name, weekday (Monday-Sunday), night_count, avg_runtime_min, avg_downtime_min,
  avg_loss_time_min, avg_waiting_time_min

tower_month_summary — per-tower per-month TOTALS/AVERAGES, already aggregated (towers x number of
  months, always complete regardless of dataset size). Use this for any tower-level month-on-month or
  monthly trend question instead of grouping tower_days yourself, which is row-capped on large datasets.
  Fields: tower, tower_name, month (YYYY-MM), night_count, total_runtime_min, total_downtime_min,
  total_loss_time_min, total_waiting_time_min

downtime_by_reason — downtime events by reason and folder
  Fields: reason, machine, folder, count (event count), total_minutes

downtime_by_folder — total incident counts per folder
  Fields: folder, incident_count, total_minutes

delayed_pf — ONLY the folders/nights whose print finish crossed the compliance cutoff (04:00 / 03:00 /
  02:30). This is a SUBSET, not "print finish time in general" — on-time nights never appear here. For
  "what time did printing finish" without the word "delayed", use exact_dashboard.daily / .folder_days
  print_finish_time instead, which is populated for every night.
  Fields: folder, run_date, night_type (GNP/UV or SNP/non-UV, ALREADY PRECOMPUTED per row —
  use this directly for GNP-vs-SNP-night comparisons, never join to a separate nights table),
  overrun_minutes, cutoff_time, estimated_print_finish_time, editions

editions_by_folder — unique editions printed per folder across the period
  Fields: folder, editions (list of names), edition_count

editions_by_tower — unique editions printed per tower across the period
  Fields: tower, editions (list of names), edition_count

book_details — per-print-job rows from master view (Book Wise + General + Down Time joined)
  Fields: IssueID, Report Date, Run Date, Edition, Products, Machine, Folder, Plant Name,
  Total Run Time (mnts), Total Downtime, towers_list (list), towers_str,
  downtime_total_min, downtime_count, downtime_departments
  Use for: "which towers did edition X use", "what product ran on folder Y",
  "how much downtime did edition Z have", cross-referencing edition+product+tower

complexity_by_code — runtime by individual C1-C15 complexity code
  Fields: code, type (SNP/GNP/SNP Complex/GNP Complex), runtime_min, is_complex

tower_downtime_reason_attribution — downtime reasons attributed to towers
  Fields in by_tower_reason: tower, reason, event_count, total_minutes

tower_usage_distribution — histogram of active tower counts by day
  Fields: towers_used, day_count, dates. Use for "number of towers used" distribution charts.

tower_runtime_mix — generic runtime mix by tower type and product type
  Fields: tower_type_key, tower_type, product_type, runtime_min, share_of_tower_type_runtime_pct, tower_day_count, tower_count, towers.
  Use for product-runtime share questions on tower types. Example: SNP share on GNP/UV towers =
  row(tower_type_key='gnp_uv', product_type='SNP').runtime_min / row(tower_type_key='gnp_uv', product_type='All').runtime_min * 100.

tower_runtime_segments — segment-level runtime rows included only when needed
  Fields: run_date, tower, tower_type_key, tower_type, uv_tower, product_type, complexity_code, category, minutes,
  print_order, source_print_order, committed_speed_cph, actual_speed_cph, efficiency_pct.
  Use these rows for direct calculation when product/tower runtime percentages or speed efficiency require raw segment detail.

gnp_snp_folder_analysis — precomputed GNP vs SNP folder-night comparison tables
  comparison_by_product_type fields: product_type (GNP/SNP), folder_day_count, total_runtime_min,
  avg_spare_time_min, avg_loss_time_min, avg_waiting_time_min, avg_lpr_to_start_min,
  avg_reflong_time_min, avg_downtime_min, delayed_folder_day_count, delayed_folder_day_pct,
  avg_overrun_min.
  gnp_loss_breakdown_by_folder fields: folder, gnp_folder_day_count, total_loss_time_min,
  avg_loss_time_min, change_over_time_min, lpr_to_start_min, reflong_time_min.
  nights_with_min_3_gnp_folders fields: run_date, gnp_folder_count, avg_spare_time_min,
  total_spare_time_min, folders.
  delayed_finish_complexity fields: run_date, folder, print_finish_time, overrun_minutes,
  complexity_codes, complexity_categories, largest_components, editions.
  web_break_gnp_snp_tower_comparison fields: tower, product_type, event_count, total_minutes,
  avg_minutes_per_event, matching_note.
  Use this source for the questions in query.docx about GNP vs SNP editions/folders and web break.

COMPUTATION NOTES:
- "average [metric] per folder" → exact_dashboard.folders; divide total_field by active_nights
- loss_time = changeover + late_start + reflong (NEVER includes waiting_time)
- unscheduled time = unplanned_time_min, not loss_time
- spare_time = spare_time_min (buffer time only, NOT unplanned_time_min)
- Base SNP / standard: C1-C4, including SNP Complex. Base GNP / glossy / UV: C5-C15, including GNP Complex.
- If the user explicitly asks for simple vs complex buckets, use SNP: C1-C3 | SNP Complex: C4 | GNP: C5-C8 | GNP Complex: C9-C15
- downtime incident frequency per folder → downtime_by_folder (incident_count)
- downtime by reason per tower → tower_downtime_reason_attribution
- editions on a tower/folder → editions_by_tower / editions_by_folder
- delayed print finish / overrun → delayed_pf
- "print finish time" / "last edition for the day" / "when did printing finish" (WITHOUT the word
  "delayed") → exact_dashboard.daily (plant-level: print_finish_time, last_edition, last_folder) or
  exact_dashboard.folder_days (per-folder: print_finish_time, last_edition). Do NOT use delayed_pf for
  this — it only has the late subset, not every night. Do NOT use editions_by_date — it lists every
  edition that ran that day, not which one finished LAST or when.
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
        '  "filters": {"<field>": "<value for an equality/contains match, OR {\\"op\\": \\">|<|>=|<=\\", \\"value\\": <number>} for a numeric threshold, else omit>"},\n'
        '  "group_by": "folder|tower|date|plant|reason|night_type|uv_tower|complexity|none",\n'
        '  "output_format": "table|single_value|list|ranked_list|comparison|trend_chart_description"\n'
        "}\n\n"
        "FILTER RULES:\n"
        "- A question with a comparator word (\"greater than\", \"more than\", \"over\", \"above\", \"at least\" → "
        "op \">\" or \">=\"; \"less than\", \"under\", \"below\", \"at most\" → op \"<\" or \"<=\") MUST use the "
        "{\"op\": ..., \"value\": ...} filter shape on the relevant numeric field — never put a comparator "
        "phrase into a plain equality filter, and never drop the condition from filters.\n"
        "- A bare number/percentage with NO comparator word (e.g. \"worked with spare capacity 10%\", "
        "\"nights at 10% utilization\") almost always means a THRESHOLD, not an exact match — interpret it "
        "as {\"op\": \"<=\", \"value\": 10} (at or below that level), since the real intent is virtually "
        "always \"how rare/low did this get\", not an exact equality nobody would ask about a continuous metric.\n"
        "- If the question asks 'how many days/nights/dates' from a source where each row is NOT one "
        "row per day (e.g. delayed_pf has one row per delayed folder per night), still set intent to "
        "\"count\" — the day/night deduplication is handled outside the plan, you do not need to do it.\n"
        "- If the question also asks for 'components', 'breakdown', or 'key components' alongside a count "
        "or filter, still just set intent/filters normally — the components breakdown is added "
        "automatically when those words appear in the question; you do not need a special field for it.\n"
        "- CRITICAL: every field named in filters/metrics MUST actually exist on primary_source's field "
        "list above — an unresolvable numeric filter field aborts the whole answer rather than silently "
        "matching every row, so picking the wrong table is worse than a normal mistake. For a plant-level "
        "night filter on runtime/lost time/downtime/wait time/spare time/utilization/spare capacity, the "
        "field lives on exact_dashboard.daily (plant-wide) or exact_dashboard.folder_days (per folder) — "
        "use one of those as primary_source. loss_time.all_days only has run_date, runtime_min, "
        "lost_time_min, waiting_time_min, loss_pct, dominant_driver, and loss_components — it has NO "
        "downtime_min or spare_time_min field, so never filter on those there; it is auto-joined for the "
        "loss_components breakdown regardless of which table you pick as primary_source, so you do not "
        "need to select it just because the question says 'components'.\n"
        "- A metric mentioned with NO number at all — \"did we have downtime\", \"how many nights had "
        "downtime\", \"any downtime\", \"experienced lost time\", \"with delays\" — means that metric was "
        "PRESENT/non-zero, not literally any value. Use {\"op\": \">\", \"value\": 0} on that field. Do not "
        "omit the filter just because no explicit number was stated in the question."
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


# ── Query Understanding (QU) Layer ───────────────────────────────────────────
# Replaces the shallow _call_planner + _answer_from_plan pipeline with a richer
# decomposer that understands multi-condition AND/OR, time scope, entity filters,
# and compound sub-questions — all executed deterministically before any full LLM call.

_QU_DECOMPOSER_SOURCES = """\
exact_dashboard.daily       — per-date plant totals; fields: run_date, weekday, month, runtime_min, loss_time_min, downtime_min, waiting_time_min, spare_time_min, unplanned_time_min, utilization_pct, spare_capacity_pct, night_type
exact_dashboard.folders     — per-folder period totals; fields: resource (folder name), runtime_min, loss_time_min, downtime_min, waiting_time_min, spare_time_min, unplanned_time_min, active_nights, utilization_pct, spare_capacity_pct
exact_dashboard.folder_days — exactly one row per folder per date; fields: plant, machine, folder_name, folder, run_date, weekday, month, runtime_min, loss_time_min, downtime_min, waiting_time_min, waiting_time_pct (= waiting_time_min / available_capacity_min * 100), spare_time_min, unplanned_time_min, utilization_pct, spare_capacity_pct, complexity_codes, complexity_categories, folder_has_gnp (bool: this folder ran any C5-C15), folder_has_gnp_complex (bool: this folder ran any C9-C15), folder_has_snp (bool: this folder ran any C1-C4), folder_has_snp_only (bool), folder_product_types, plant_night_type, plant_gnp_night, delayed_print_finish, overrun_minutes, print_finish_time. For what a specific folder printed, use folder_has_*; plant_night_type is only the whole-plant classification. For an arbitrary clock threshold such as "after 03:30", compare print_finish_time here; do not use delayed_pf unless the question asks about the compliance cutoff.
loss_time.all_days          — per-date loss breakdown; fields: run_date, weekday, month, runtime_min, lost_time_min, waiting_time_min, loss_pct, loss_components (dict), dominant_driver
delayed_pf                  — late-night folder rows only; fields: run_date, folder, overrun_minutes, loss_time_min, downtime_min, waiting_time_min, runtime_min, night_type, editions
towers                      — per-tower period totals; fields: tower, machine, runtime_min, downtime_min, loss_time_min, waiting_time_min, spare_time_min, active_nights, uv_tower. No utilization_pct — utilization is folder-level only.
tower_days                  — per-tower per-date; fields: tower, run_date, weekday, month, runtime_min, downtime_min, loss_time_min, waiting_time_min, uv_tower. No utilization_pct — utilization is folder-level only.
tower_runtime_mix           — runtime split by tower TYPE × product TYPE (use this for any question about SNP/GNP products running on UV/GNP or non-UV towers); fields: tower_type_key ("gnp_uv" or "non_uv"), tower_type (human label), product_type ("SNP", "GNP", or "Unknown"), runtime_min, share_of_tower_type_runtime_pct, tower_day_count, tower_count
tower_runtime_segments      — segment-level runtime rows (most granular; use when tower_runtime_mix is not precise enough); fields: run_date, tower, tower_type_key, tower_type, uv_tower, product_type, complexity_code, category, minutes, print_order, committed_speed_cph, actual_speed_cph, efficiency_pct
gnp_snp_folder_analysis     — precomputed GNP vs SNP folder-night comparisons. Use for GNP/SNP questions about average spare, loss, wait, LPR-to-start, reflong, downtime, delayed finish, and web break. Subtables include comparison_by_product_type, gnp_loss_breakdown_by_folder, nights_with_min_3_gnp_folders, delayed_finish_complexity, web_break_gnp_snp_tower_comparison.
daily_efficiency            — per-date plant-wide efficiency summary (one row per production night); fields: run_date, total_po, total_runtime_min, total_dt_min, actual_speed_cph, committed_speed_cph, efficiency_pct. USE THIS for any question about efficiency by date, days above/below an efficiency threshold, or efficiency trends over time.
downtime_by_reason          — top reasons ranked by event count; fields: reason, count, total_minutes
complexity_by_code          — runtime by C1-C15 code; fields: code, runtime_min, print_order
book_details                — per print job; fields: IssueID, Edition, Machine, Folder, towers_list, total_pages (pagination / number of pages per edition), downtime_total_min"""

_QU_DECOMPOSER_SYSTEM = (
    "You are a query-understanding agent for a newspaper print-plant analytics dashboard.\n"
    "Given a user question, output ONLY a JSON object — no prose, no markdown fences.\n\n"
    "OUTPUT SCHEMA:\n"
    "{\n"
    '  "intent": "count|aggregate|average|breakdown|trend|comparison|lookup|list|ranking|prediction",\n'
    '  "primary_source": "<source key from the list below>",\n'
    '  "entities": [{"type": "folder|machine|tower|plant|edition|date", "value": "<specific name or ALL>"}],\n'
    '  "metrics": [{"field": "<exact field name from source>", "label": "<human label>", "aggregation": "sum|avg|max|min|count"}],\n'
    '  "conditions": [\n'
    '    {"field": "<exact field name>", "op": ">|<|>=|<=|=|contains", "value": <number, string, or boolean>, "label": "<human readable>"}\n'
    '  ],\n'
    '  "condition_logic": "AND|OR",\n'
    '  "time_scope": {\n'
    '    "type": "none|weekday|month|date_range|specific_date",\n'
    '    "weekdays": ["Monday|Tuesday|Wednesday|Thursday|Friday|Saturday|Sunday"],\n'
    '    "months": ["YYYY-MM"],\n'
    '    "date_from": "YYYY-MM-DD or null",\n'
    '    "date_to": "YYYY-MM-DD or null",\n'
    '    "specific_date": "YYYY-MM-DD or null"\n'
    '  },\n'
    '  "group_by": "folder|tower|date|weekday|month|plant|reason|night_type|none",\n'
    '  "sort_by": {"field": "<field name>", "order": "asc|desc"},\n'
    '  "limit": null,\n'
    '  "sub_questions": [\n'
    '    {"id": "q1", "intent": "count|aggregate|breakdown|list", "primary_source": "<source key or omit if same as top-level>", "description": "<what this sub-question computes>"}\n'
    '  ],\n'
    '  "required_sources": ["<primary_source>", "<any additional source keys the full LLM will need>"],\n'
    '  "output_format": "table|single_value|list|ranked_list|comparison"\n'
    "}\n\n"
    f"DATA SOURCES:\n{{_QU_DECOMPOSER_SOURCES}}\n\n"
    "RULES:\n"
    "R1 MULTI-CONDITION: Put every filter in conditions[]. Use AND (default) or OR between them. "
    "Each condition field MUST exist on primary_source — pick a different source if needed. "
    "Never put a comparator word ('greater than', 'more than') into a plain string equality filter.\n"
    "R2 TIME SCOPE: 'on Fridays'→weekdays:['Friday']. 'weekends'→['Friday','Saturday','Sunday'] "
    "(Indian newspaper plants: Friday/Saturday/Sunday nights are weekend editions). "
    "'in September'→months:['2025-09']. Date range→date_from/date_to. No time constraint→type:'none'.\n"
    "R3 ENTITY: 'for folder F1'→entities:[{type:folder,value:F1}]. A press/machine name such as "
    "'Hiline-1'→entities:[{type:machine,value:Hiline-1}]. Unspecified scope→entities:[].\n"
    "R4 COMPOUND QUESTION: Two separate things asked (e.g. 'how many nights AND what are the key components') "
    "→ sub_questions with one entry per distinct ask. Simple single question→sub_questions:[].\n"
    "R5 IMPLICIT CONDITIONS: 'had downtime'→{field:downtime_min,op:>,value:0}. "
    "'was delayed'→{field:overrun_minutes,op:>,value:0}. 'had lost time'→{field:loss_time_min,op:>,value:0}.\n"
    "R6 TABLE CHOICE: loss_time.all_days has NO downtime_min or spare_time_min — never filter those there. "
    "For downtime filter use exact_dashboard.daily or exact_dashboard.folder_days or towers. "
    "For delayed-finish overrun filters use delayed_pf.\n"
    "R6b SNP/GNP ON TOWER TYPE: Any question about runtime of SNP or GNP *products* on UV/GNP *towers* "
    "(e.g. 'SNP products on GNP tower', 'GNP runtime on non-UV towers') MUST use tower_runtime_mix. "
    "Filter on tower_type_key ('gnp_uv' for UV/GNP towers, 'non_uv' for standard towers) "
    "and product_type ('SNP' or 'GNP'). NEVER use the towers table for this — it has no product_type field. "
    "For even more granular segment-level data use tower_runtime_segments with the same fields.\n"
    "R7 COUNT DEDUP AND EVIDENCE: When intent='count' asks for days, nights, or dates, count "
    "distinct run_date values, set output_format:'table', and include the matching dates and "
    "relevant condition values. Never use output_format:'single_value' for a date count. "
    "Sources such as delayed_pf and folder_days can contain multiple matching rows per date.\n"
    "R8 METRIC FIELDS: Use exact field names from the source listed above (e.g. 'loss_time_min', not 'lost time'). "
    "Aggregation defaults to 'sum' for totals, 'avg' for per-night averages.\n"
    "R9 SORT: Include sort_by only when user asks for ranking or top/bottom N. Otherwise omit or set to null.\n"
    "R10 TREND/PREDICTION: If intent is 'trend' or 'prediction', still output a plan but note it in intent — "
    "the executor will defer time-series computation to the full LLM.\n"
    "R11 GROUP BY — MANDATORY: Any question containing 'per folder', 'by folder', 'for each folder', "
    "'each folder', 'folder-wise', 'folder wise' MUST set group_by:'folder'. "
    "'per tower'/'by tower'/'for each tower' → group_by:'tower'. "
    "'per date'/'day by day'/'daily'/'each day'/'each night' → group_by:'date'. "
    "'per week'/'day of week'/'weekday wise' → group_by:'weekday'. "
    "'by month'/'month on month'/'monthly' → group_by:'month'. "
    "'by reason' → group_by:'reason'. "
    "NEVER leave group_by as 'none' when the question explicitly names a grouping dimension with "
    "'per', 'by', 'each', 'wise', or 'breakdown'. "
    "For per-folder breakdowns prefer exact_dashboard.folders (one row per folder, already aggregated) "
    "with intent:'aggregate'. For per-folder per-night detail use exact_dashboard.folder_days.\n"
    "R12 REQUIRED SOURCES — MANDATORY: Set required_sources to every source key the full LLM will need. "
    "Rules: (a) always include primary_source; (b) for multi-part questions with multiple '?' include a "
    "source for each distinct part; (c) for cross-reference questions (e.g. 'delayed nights AND the reasons') "
    "include the event table AND the reason/driver table (e.g. ['gnp_snp_folder_analysis.delayed_finish_complexity', 'loss_time.all_days']); "
    "(d) if a sub_question has a different primary_source, include it; (e) limit to 6 sources maximum. "
    "Examples: 'days below 90% efficiency'→['daily_efficiency']; "
    "'delayed nights, complexity, and reasons'→['gnp_snp_folder_analysis.delayed_finish_complexity','loss_time.all_days','exact_dashboard.folder_days']; "
    "'average spare time when 3+ GNP folders'→['gnp_snp_folder_analysis.nights_with_min_3_gnp_folders']; "
    "'downtime by tower by reason'→['towers','tower_downtime_reason_attribution'].\n"
    "R13 FOLDER-SPECIFIC GNP/SNP — MANDATORY: For any question about days/nights when a named "
    "folder itself ran GNP, GNP Complex, or SNP, use exact_dashboard.folder_days. Filter "
    "folder_has_gnp=true for GNP including GNP Complex (C5-C15), folder_has_gnp_complex=true for "
    "GNP Complex only (C9-C15), folder_has_snp=true when it ran any SNP (C1-C4), or "
    "folder_has_snp_only=true for SNP-only nights. Never use plant_night_type or plant_gnp_night "
    "unless the user explicitly asks what the whole plant ran. Example: 'average spare-time "
    "percentage for Folder B across all days it ran GNP including GNP Complex' → primary_source "
    "exact_dashboard.folder_days, entity folder=Folder B, condition folder_has_gnp=true, metric "
    "spare_capacity_pct aggregation=avg, intent=average. 'average wait-time percentage' uses "
    "waiting_time_pct aggregation=avg, whose denominator is available capacity, never runtime + wait. "
    "Include only exact_dashboard.folder_days in required_sources "
    "unless another distinct part of the question truly needs another table.\n"
    "R14 PRINT-FINISH CLOCK THRESHOLDS — MANDATORY: Questions such as 'print finish after 03:30' "
    "use exact_dashboard.folder_days.print_finish_time and a normal clock condition. A named press "
    "such as Hiline-1 is a machine entity, not a folder equality condition. Use delayed_pf only "
    "when the question explicitly refers to delayed/compliance-cutoff finishes.\n"
)

# Inject the source list at runtime to avoid f-string issues with the braces in the schema above
_QU_DECOMPOSER_SYSTEM = _QU_DECOMPOSER_SYSTEM.replace(
    "{_QU_DECOMPOSER_SOURCES}", _QU_DECOMPOSER_SOURCES
)

_QU_WEEKDAY_DISPLAY = {
    "monday": "Monday", "tuesday": "Tuesday", "wednesday": "Wednesday",
    "thursday": "Thursday", "friday": "Friday", "saturday": "Saturday", "sunday": "Sunday",
}


def _call_qu_decomposer(message: str, endpoint: str, api_key: str) -> dict[str, Any]:
    """LLM call (JSON mode) that deeply parses a user question into a structured QU plan."""
    messages: list[dict[str, str]] = [
        {"role": "system", "content": _QU_DECOMPOSER_SYSTEM},
        {"role": "user", "content": _clean_text(message)},
    ]
    raw = _call_chat_completion(endpoint, api_key, messages)
    raw = raw.strip()
    if raw.startswith("```"):
        raw = re.sub(r"^```[a-z]*\n?", "", raw).rstrip("`").strip()
    return json.loads(raw)


async def _call_qu_decomposer_async(
    message: str,
    endpoint: str,
    api_key: str,
    cancellation_event: asyncio.Event | None = None,
) -> dict[str, Any]:
    """Cancellable chat-only variant of _call_qu_decomposer."""
    messages: list[dict[str, str]] = [
        {"role": "system", "content": _QU_DECOMPOSER_SYSTEM},
        {"role": "user", "content": _clean_text(message)},
    ]
    _raise_if_chat_cancelled(cancellation_event)
    raw = await _call_chat_completion_async(endpoint, api_key, messages, cancellation_event=cancellation_event)
    raw = raw.strip()
    if raw.startswith("```"):
        raw = re.sub(r"^```[a-z]*\n?", "", raw).rstrip("`").strip()
    return json.loads(raw)


def _normalize_qu_plan_for_question(
    qu_plan: dict[str, Any] | None,
    message: str,
    context: dict[str, Any] | None = None,
) -> dict[str, Any] | None:
    """Correct high-risk source/field mistakes before a plan controls retrieval.

    The decomposer remains responsible for general query understanding. This validator only
    enforces semantic invariants that are knowable from the data model, chiefly that "this folder
    ran GNP" is a folder-night property rather than a plant-night property.
    """
    if not isinstance(qu_plan, dict) or not qu_plan:
        return qu_plan

    normalized = json.loads(json.dumps(qu_plan))
    question = _clean_text(message).casefold()
    entities = normalized.get("entities") or []
    has_folder_scope = (
        "folder" in question
        or bool(re.search(r"\bcolor\s+[a-z0-9]+\b", question))
        or _clean_text(normalized.get("group_by")).casefold() == "folder"
        or any(_clean_text(entity.get("type")).casefold() == "folder" for entity in entities)
    )
    mentions_gnp = "gnp" in question
    mentions_snp = bool(re.search(r"\bsnp\b", question))
    if has_folder_scope and (mentions_gnp or mentions_snp):
        source_key = "exact_dashboard.folder_days"
        normalized["primary_source"] = source_key

        if not any(_clean_text(entity.get("type")).casefold() == "folder" for entity in entities):
            alias_match = re.search(r"\bfolder\s+([a-z0-9])\b", _clean_text(message), re.IGNORECASE)
            if alias_match:
                entities.append({"type": "folder", "value": f"Folder {alias_match.group(1).upper()}"})
            else:
                color_match = re.search(r"\bcolor\s+([a-z0-9]+)\b", _clean_text(message), re.IGNORECASE)
                if color_match:
                    entities.append({"type": "folder", "value": f"COLOR {color_match.group(1).upper()}"})
        normalized["entities"] = entities

        conditions = [
            condition
            for condition in (normalized.get("conditions") or [])
            if _clean_text(condition.get("field")).casefold()
            not in {
                "night_type",
                "gnp_night",
                "plant_night_type",
                "plant_gnp_night",
                "folder_has_gnp",
                "folder_has_gnp_complex",
                "folder_has_snp",
                "folder_has_snp_only",
            }
        ]
        # A GNP-vs-SNP comparison needs both populations, so do not reduce it to one flag.
        if mentions_gnp and not mentions_snp:
            complex_only = "gnp complex" in question and not any(
                term in question for term in ["including gnp complex", "include gnp complex", "including complex"]
            )
            condition_field = "folder_has_gnp_complex" if complex_only else "folder_has_gnp"
            conditions.append({
                "field": condition_field,
                "op": "=",
                "value": True,
                "label": "folder ran GNP Complex" if complex_only else "folder ran GNP (including GNP Complex)",
            })
        elif mentions_snp and not mentions_gnp:
            condition_field = "folder_has_snp_only" if "snp only" in question or "only snp" in question else "folder_has_snp"
            conditions.append({
                "field": condition_field,
                "op": "=",
                "value": True,
                "label": "folder ran SNP only" if condition_field.endswith("_only") else "folder ran SNP",
            })
        normalized["conditions"] = conditions
        normalized["condition_logic"] = "AND"

        asks_percentage = any(term in question for term in ["percentage", "percent", "%"])
        if asks_percentage and any(term in question for term in ["wait time", "wait-time", "waiting time"]):
            normalized["metrics"] = [{
                "field": "waiting_time_pct",
                "label": "Wait-time percentage",
                "aggregation": "avg" if "average" in question or "avg" in question else "sum",
            }]
        elif asks_percentage and "spare" in question:
            normalized["metrics"] = [{
                "field": "spare_capacity_pct",
                "label": "Spare-capacity percentage",
                "aggregation": "avg" if "average" in question or "avg" in question else "sum",
            }]

        required_sources = [source_key]
        for sub_question in normalized.get("sub_questions") or []:
            sub_source = _clean_text(sub_question.get("primary_source"))
            if sub_source and sub_source != source_key and sub_source not in required_sources:
                required_sources.append(sub_source)
        normalized["required_sources"] = required_sources[:6]

    if context:
        normalized = _resolve_qu_plan_entities(normalized, context)
    if _plan_counts_dates(normalized, question):
        normalized["output_format"] = "table"
    return normalized


_QU_ENTITY_FIELD_CANDIDATES: dict[str, list[str]] = {
    "folder": ["folder_name", "folder", "resource"],
    "machine": ["machine", "machine_name"],
    "tower": ["tower_name", "tower"],
    "plant": ["plant", "plant_name"],
    "edition": ["edition", "edition_name", "editions"],
    "date": ["run_date", "date"],
}


def _plan_counts_dates(plan: dict[str, Any], question: str) -> bool:
    has_count_intent = _clean_text(plan.get("intent")).casefold() == "count" or any(
        isinstance(sub_question, dict)
        and _clean_text(sub_question.get("intent")).casefold() == "count"
        for sub_question in (plan.get("sub_questions") or [])
    )
    if not has_count_intent:
        return False
    if re.search(r"\b(?:day|days|night|nights|date|dates)\b", question):
        return True
    return any(
        _clean_text(metric.get("field") if isinstance(metric, dict) else metric).casefold()
        in {"run_date", "date"}
        and (
            not isinstance(metric, dict)
            or _clean_text(metric.get("aggregation") or "count").casefold() == "count"
        )
        for metric in (plan.get("metrics") or [])
    )


def _entity_match_score(entity_type: str, expected: str, actual: Any) -> int:
    expected_normalized = " ".join(re.findall(r"[a-z0-9]+", _clean_text(expected).casefold()))
    actual_normalized = " ".join(re.findall(r"[a-z0-9]+", _row_value_text(actual).casefold()))
    if not expected_normalized or not actual_normalized:
        return 0
    if expected_normalized == actual_normalized:
        return 4
    if _entity_value_matches(entity_type, expected, actual):
        return 2
    return 0


def _resolve_qu_plan_entities(
    plan: dict[str, Any],
    context: dict[str, Any],
) -> dict[str, Any]:
    """Resolve decomposer entity labels against canonical fields on its selected source."""
    rows = _rows_for_plan_source(_clean_text(plan.get("primary_source")), context)
    if not rows:
        return plan

    resolved_entities: list[dict[str, Any]] = []
    original_entity_values: set[str] = set()
    for entity in plan.get("entities") or []:
        if not isinstance(entity, dict):
            continue
        entity_value = _clean_text(entity.get("value"))
        declared_type = _clean_text(entity.get("type")).casefold()
        if not entity_value or entity_value.casefold() in {"all", "any"}:
            resolved_entities.append(entity)
            continue
        original_entity_values.add(" ".join(re.findall(r"[a-z0-9]+", entity_value.casefold())))
        best_type = declared_type
        best_score = 0
        for candidate_type, fields in _QU_ENTITY_FIELD_CANDIDATES.items():
            for field in fields:
                resolved_field = _resolve_row_field(rows, field)
                if not resolved_field:
                    continue
                score = max(
                    (_entity_match_score(candidate_type, entity_value, row.get(resolved_field)) for row in rows),
                    default=0,
                )
                if score > best_score or (
                    score == best_score and score > 0 and candidate_type == declared_type
                ):
                    best_type = candidate_type
                    best_score = score
        resolved_entities.append({**entity, "type": best_type or declared_type})

    identity_fields = {
        field
        for fields in _QU_ENTITY_FIELD_CANDIDATES.values()
        for field in fields
    }
    conditions: list[dict[str, Any]] = []
    for condition in plan.get("conditions") or []:
        if not isinstance(condition, dict):
            continue
        field = _clean_text(condition.get("field")).casefold()
        value_normalized = " ".join(
            re.findall(r"[a-z0-9]+", _clean_text(condition.get("value")).casefold())
        )
        is_redundant_entity_equality = (
            _clean_text(condition.get("op") or "=") == "="
            and field in identity_fields
            and value_normalized in original_entity_values
        )
        if not is_redundant_entity_equality:
            conditions.append(condition)

    plan["entities"] = resolved_entities
    plan["conditions"] = conditions
    return plan


_CLOCK_COMPARISON_FIELDS = {
    "printfinishtime",
    "actualprintfinishtime",
    "estimatedprintfinishtime",
    "pfcutofftime",
    "cutofftime",
}


def _clock_minutes(value: Any) -> float | None:
    """Parse a clock value without turning invalid strings or blanks into midnight."""
    if isinstance(value, datetime):
        return value.hour * 60 + value.minute + value.second / 60
    text = _clean_text(value).casefold()
    if not text:
        return None
    text = re.sub(r"\b([ap])\.?m\.?$", r"\1m", text)
    text = re.sub(r"(?<=\d)\.(?=\d{2}(?:\s*[ap]m)?$)", ":", text)
    match = re.search(
        r"(\d{1,2}):(\d{2})(?::(\d{2}))?\s*([ap]m)?$",
        text,
        flags=re.IGNORECASE,
    )
    if not match:
        return None
    hour = int(match.group(1))
    minute = int(match.group(2))
    second = int(match.group(3) or 0)
    meridiem = (match.group(4) or "").casefold()
    if minute > 59 or second > 59:
        return None
    if meridiem:
        if not 1 <= hour <= 12:
            return None
        hour = hour % 12 + (12 if meridiem == "pm" else 0)
    elif hour > 23:
        return None
    return hour * 60 + minute + second / 60


def _finite_number_or_none(value: Any) -> float | None:
    try:
        numeric = float(value)
    except (TypeError, ValueError):
        return None
    return numeric if isfinite(numeric) else None


def _compare_condition_values(field: str, actual: Any, expected: Any, op: str) -> bool:
    compare_fn = _COMPARATOR_FUNCS.get(op)
    if compare_fn is None:
        return False
    normalized_field = _normalize_field_name(field)
    actual_clock = _clock_minutes(actual)
    expected_clock = _clock_minutes(expected)
    if normalized_field in _CLOCK_COMPARISON_FIELDS or (
        actual_clock is not None and expected_clock is not None
    ):
        return bool(
            actual_clock is not None
            and expected_clock is not None
            and compare_fn(actual_clock, expected_clock)
        )
    actual_number = _finite_number_or_none(actual)
    expected_number = _finite_number_or_none(expected)
    return bool(
        actual_number is not None
        and expected_number is not None
        and compare_fn(actual_number, expected_number)
    )


def _apply_qu_conditions(
    rows: list[dict[str, Any]],
    conditions: list[dict[str, Any]],
    logic: str = "AND",
) -> list[dict[str, Any]] | None:
    """Multi-condition filter with AND/OR logic.

    Returns None when ANY condition references a field that doesn't exist anywhere on the source
    (same abort-signal contract as _apply_plan_filters) so the caller can fall through cleanly
    rather than returning an over-broad unfiltered count that looks like a real answer. This used
    to only apply to numeric comparators (">"/"<"/etc) — an "=" or "contains" condition on an
    unresolved field silently passed every row, which looked like a real filtered answer but
    was actually the full unfiltered set.
    """
    if not conditions or not rows:
        return rows

    logic = (logic or "AND").strip().upper()

    # Pre-validate: any condition (of any operator) on a field absent from the whole row set
    # aborts the whole plan rather than silently no-op-filtering.
    for cond in conditions:
        field_raw = _clean_text(cond.get("field", ""))
        if field_raw and not _resolve_row_field(rows, field_raw):
            return None

    def _passes(row: dict[str, Any]) -> bool:
        results: list[bool] = []
        for cond in conditions:
            field_raw = _clean_text(cond.get("field", ""))
            op = _clean_text(cond.get("op", "=")).strip()
            value = cond.get("value")
            field_name = _resolve_row_field([row], field_raw) or _resolve_row_field(rows, field_raw)
            if not field_name:
                # Field exists on the row set overall (pre-validated above) but not on this
                # specific row — treat as a non-match rather than an automatic pass.
                results.append(False)
                continue
            row_val = row.get(field_name)
            if op in (">", "<", ">=", "<="):
                results.append(_compare_condition_values(field_name, row_val, value, op))
            elif op == "=":
                results.append(
                    _clean_text(row_val).casefold() == _clean_text(str(value) if value is not None else "").casefold()
                )
            elif op == "contains":
                results.append(_clean_text(str(value) if value is not None else "").casefold() in _row_value_text(row_val).casefold())
            else:
                results.append(True)
        if not results:
            return True
        return any(results) if logic == "OR" else all(results)

    return [row for row in rows if _passes(row)]


def _apply_qu_time_scope(rows: list[dict[str, Any]], time_scope: dict[str, Any]) -> list[dict[str, Any]]:
    """Filter rows by time_scope: weekday, month, specific_date, or date_range."""
    if not time_scope or not rows:
        return rows
    scope_type = _clean_text(time_scope.get("type", "none")).casefold()
    if scope_type == "none":
        return rows

    date_field = _resolve_row_field(rows, "run_date") or _resolve_row_field(rows, "date")
    weekday_field = _resolve_row_field(rows, "weekday")
    month_field = _resolve_row_field(rows, "month")
    result = list(rows)

    if scope_type == "weekday" and weekday_field:
        target = {
            _QU_WEEKDAY_DISPLAY.get(_clean_text(w).casefold(), _clean_text(w))
            for w in (time_scope.get("weekdays") or [])
        }
        if target:
            result = [r for r in result if _clean_text(r.get(weekday_field)) in target]

    elif scope_type == "month" and month_field:
        target_months = {_clean_text(m) for m in (time_scope.get("months") or [])}
        if target_months:
            result = [r for r in result if _clean_text(r.get(month_field)) in target_months]

    elif scope_type == "specific_date" and date_field:
        specific = _clean_text(time_scope.get("specific_date") or "")
        if specific:
            result = [r for r in result if _clean_text(r.get(date_field)) == specific]

    elif scope_type == "date_range" and date_field:
        date_from = _clean_text(time_scope.get("date_from") or "")
        date_to = _clean_text(time_scope.get("date_to") or "")
        if date_from:
            result = [r for r in result if _clean_text(r.get(date_field)) >= date_from]
        if date_to:
            result = [r for r in result if _clean_text(r.get(date_field)) <= date_to]

    return result


def _apply_qu_entity_filters(
    rows: list[dict[str, Any]], entities: list[dict[str, Any]]
) -> list[dict[str, Any]]:
    """Filter rows by typed entity (folder, machine, tower, plant, edition, date)."""
    if not entities or not rows:
        return rows
    result = list(rows)
    for entity in entities:
        entity_type = _clean_text(entity.get("type", "")).casefold()
        entity_value = _clean_text(entity.get("value", ""))
        if not entity_value or entity_value.casefold() in ("all", "any", ""):
            continue
        field_names = [
            resolved
            for candidate in _QU_ENTITY_FIELD_CANDIDATES.get(entity_type, [entity_type])
            if (resolved := _resolve_row_field(result, candidate))
        ]
        if not field_names:
            continue
        result = [
            row for row in result
            if any(
                _entity_value_matches(entity_type, entity_value, row.get(field_name))
                for field_name in field_names
            )
        ]
    return result


def _entity_value_matches(entity_type: str, expected: str, actual: Any) -> bool:
    expected_text = _clean_text(expected).casefold()
    actual_text = _row_value_text(actual).casefold()
    if not expected_text or expected_text in actual_text:
        return bool(expected_text)
    # Treat repeated whitespace and punctuation consistently. This handles canonical names such as
    # "COLOR  B" when the user or decomposer emits "COLOR B".
    expected_normalized = " ".join(re.findall(r"[a-z0-9]+", expected_text))
    actual_normalized = " ".join(re.findall(r"[a-z0-9]+", actual_text))
    if expected_normalized and expected_normalized in actual_normalized:
        return True
    if entity_type != "folder":
        return False

    # Users commonly refer to dashboard aliases as "Folder B" while the canonical value is
    # "COLOR B". Match that alias to the final folder-name token without broad fuzzy matching.
    alias = re.fullmatch(r"(?:folder\s*)?([a-z0-9])", expected_text)
    actual_tokens = re.findall(r"[a-z0-9]+", actual_text)
    return bool(alias and actual_tokens and actual_tokens[-1] == alias.group(1))


def _qu_metric_fields(qu_plan: dict[str, Any], rows: list[dict[str, Any]]) -> list[str]:
    """Resolve metric field names from the QU plan's metrics array."""
    fields: list[str] = []
    for metric in qu_plan.get("metrics") or []:
        field_raw = _clean_text(
            metric.get("field", "") if isinstance(metric, dict) else str(metric)
        )
        resolved = _resolve_row_field(rows, field_raw)
        if resolved and resolved not in fields and _field_has_numeric_values(rows, resolved):
            fields.append(resolved)
    return fields


_QU_IMPLICIT_GROUP_PATTERNS: list[tuple[re.Pattern[str], str]] = [
    (re.compile(r"\b(?:per|by|each|for each|wise)\s+folder\b|folder[\s-]wise\b"), "folder"),
    (re.compile(r"\b(?:per|by|each|for each|wise)\s+tower\b|tower[\s-]wise\b"), "tower"),
    (re.compile(r"\b(?:per|by|each|for each|wise)\s+plant\b|plant[\s-]wise\b"), "plant"),
    (re.compile(r"\b(?:per|by|each|for each|wise)\s+(?:reason|cause)\b"), "reason"),
    (re.compile(r"\b(?:per|by|each|for each|wise)\s+(?:date|night|day)\b|day[\s-]by[\s-]day\b|nightly\b"), "date"),
    (re.compile(r"\b(?:per|by|each|for each|wise)\s+(?:week(?:day)?|day of week)\b|weekday[\s-]wise\b"), "weekday"),
    (re.compile(r"\b(?:per|by|each|for each|wise)\s+month\b|month[\s-]on[\s-]month\b|month[\s-]wise\b|monthly\b"), "month"),
    (re.compile(r"\b(?:per|by|each|for each|wise)\s+(?:edition|book)\b"), "edition"),
]


def _qu_group_field(qu_plan: dict[str, Any], rows: list[dict[str, Any]], question: str = "") -> str:
    """Resolve group_by from the QU plan, with fallback pattern-matching on the question.

    The decomposer sometimes leaves group_by as 'none' for phrasing like 'per folder'
    or 'for each tower' — the pattern fallback catches those cases so the executor
    always groups when the question explicitly names a grouping dimension.
    """
    group_by = _clean_text(qu_plan.get("group_by") or "").casefold()
    if group_by not in ("none", ""):
        resolved = _resolve_row_field(rows, group_by)
        if resolved and not _is_list_valued_field(rows, resolved):
            return resolved

    # Fallback: scan the question for explicit grouping phrases
    q = question.casefold()
    for pattern, dimension in _QU_IMPLICIT_GROUP_PATTERNS:
        if pattern.search(q):
            resolved = _resolve_row_field(rows, dimension)
            if resolved and not _is_list_valued_field(rows, resolved):
                return resolved

    return ""


def _qu_conditions_label(conditions: list[dict[str, Any]], logic: str = "AND") -> str:
    """Human-readable summary of applied conditions for display in answers."""
    if not conditions:
        return ""
    labels = [_clean_text(c.get("label", "")) for c in conditions if _clean_text(c.get("label", ""))]
    if not labels:
        return ""
    joiner = f" {(logic or 'AND').strip().upper()} "
    return joiner.join(labels)


def _qu_time_scope_label(time_scope: dict[str, Any]) -> str:
    """Human-readable summary of the time scope."""
    scope_type = _clean_text(time_scope.get("type", "none")).casefold()
    if scope_type == "weekday":
        days = time_scope.get("weekdays") or []
        return ", ".join(days) if days else ""
    if scope_type == "month":
        months = time_scope.get("months") or []
        return ", ".join(months) if months else ""
    if scope_type == "specific_date":
        return _clean_text(time_scope.get("specific_date") or "")
    if scope_type == "date_range":
        date_from = _clean_text(time_scope.get("date_from") or "")
        date_to = _clean_text(time_scope.get("date_to") or "")
        if date_from and date_to:
            return f"{date_from} to {date_to}"
        return date_from or date_to
    return ""


def _execute_qu_subquestion(
    sub_q: dict[str, Any],
    rows: list[dict[str, Any]],
    source_key: str,
    context: dict[str, Any],
    question: str,
) -> str:
    """Execute one sub-question against the already-filtered row set."""
    intent = _clean_text(sub_q.get("intent", "")).casefold()

    if intent == "count":
        count_unit_field = _plan_count_unit_field(question, rows)
        if count_unit_field:
            distinct = {
                _clean_text(r.get(count_unit_field))
                for r in rows if _clean_text(r.get(count_unit_field))
            }
            unit_label = _humanize_field(count_unit_field).lower()
            return f"**{len(distinct)}** distinct {unit_label}(s)"
        return f"**{len(rows)}** matching row(s)"

    if intent == "breakdown":
        return _plan_components_breakdown(question, rows, context)

    if intent == "list":
        date_field = _resolve_row_field(rows, "run_date") or _resolve_row_field(rows, "date")
        if date_field:
            dates = _sorted_unique(r.get(date_field) for r in rows)
            return "\n".join(f"- {d}" for d in dates)
        return ""

    if intent in ("aggregate", "average"):
        metric_plan = {"metrics": [m.get("field") for m in (sub_q.get("metrics") or [])]}
        metric_fields = _qu_metric_fields(metric_plan, rows)
        if not metric_fields:
            return ""
        is_avg = intent == "average"
        totals = {
            m: _clean_number(
                _average([_number(r.get(m)) for r in rows]) if is_avg
                else sum(_number(r.get(m)) for r in rows)
            )
            for m in metric_fields
        }
        label = "Average" if is_avg else "Total"
        parts = [
            f"{_humanize_field(m)}: {_format_plan_metric_value(m, v)}"
            for m, v in totals.items()
        ]
        return f"{label}: " + " | ".join(parts)

    return ""


def _execute_qu_plan(
    qu_plan: dict[str, Any] | None,
    message: str,
    context: dict[str, Any],
) -> str:
    """Execute a QU decomposer plan deterministically. Returns '' to signal LLM fallthrough."""
    if not isinstance(qu_plan, dict) or not qu_plan:
        return ""

    intent = _clean_text(qu_plan.get("intent", "")).casefold()
    if intent in ("trend", "prediction"):
        return ""

    source_key = _clean_text(qu_plan.get("primary_source", ""))
    source_rows = _rows_for_plan_source(source_key, context)
    rows = list(source_rows)
    if not source_rows:
        return ""

    # 1. Multi-condition filter (AND/OR)
    conditions = qu_plan.get("conditions") or []
    condition_logic = _clean_text(qu_plan.get("condition_logic") or "AND").upper()
    rows = _apply_qu_conditions(rows, conditions, condition_logic)
    if rows is None:
        return ""

    # 2. Time scope filter
    time_scope = qu_plan.get("time_scope") or {}
    rows = _apply_qu_time_scope(rows, time_scope)

    # 3. Entity filters (folder X, tower Y, plant Z)
    entities = qu_plan.get("entities") or []
    rows = _apply_qu_entity_filters(rows, entities)

    question = _clean_text(message).casefold()
    date_count_evidence = _build_date_count_evidence(
        qu_plan,
        question,
        rows,
        source_rows,
    )
    if not rows:
        if date_count_evidence:
            return _clean_text(date_count_evidence.get("answer"))
        cond_label = _qu_conditions_label(conditions, condition_logic)
        scope_label = _qu_time_scope_label(time_scope)
        note_parts = [f"where {cond_label}" if cond_label else "", scope_label]
        note = " ".join(p for p in note_parts if p)
        return f"No rows match the specified filters{(' (' + note + ')') if note else ''}."

    # 4. Compound question: run each sub-question and combine
    sub_questions = qu_plan.get("sub_questions") or []
    if sub_questions:
        parts: list[str] = [
            _clean_text(date_count_evidence.get("answer"))
        ] if date_count_evidence else []
        for sub_q in sub_questions:
            if date_count_evidence and _clean_text(sub_q.get("intent")).casefold() in {"count", "list"}:
                continue
            result = _execute_qu_subquestion(sub_q, rows, source_key, context, question)
            if result:
                parts.append(result)
        if parts:
            cond_label = _qu_conditions_label(conditions, condition_logic)
            scope_label = _qu_time_scope_label(time_scope)
            header_parts = []
            if cond_label:
                header_parts.append(f"where {cond_label}")
            if scope_label:
                header_parts.append(f"({scope_label})")
            header = (" ".join(header_parts)).strip()
            combined = "\n\n".join(parts)
            return (header + ":\n\n" + combined) if header else combined

    if date_count_evidence:
        evidence_answer = _clean_text(date_count_evidence.get("answer"))
        if _plan_wants_components_breakdown(question):
            breakdown = _plan_components_breakdown(question, rows, context)
            return evidence_answer + breakdown if breakdown else evidence_answer
        return evidence_answer

    # 5. Single intent execution
    metric_fields = _qu_metric_fields(qu_plan, rows)
    group_field = _qu_group_field(qu_plan, rows, question)

    # Breakdown intent
    if intent == "breakdown" or _plan_wants_components_breakdown(question):
        count_unit_field = _plan_count_unit_field(question, rows)
        if count_unit_field:
            distinct = sorted({
                _clean_text(r.get(count_unit_field))
                for r in rows if _clean_text(r.get(count_unit_field))
            })
            headline = f"**{len(distinct)}** distinct {_humanize_field(count_unit_field).lower()}(s)"
        else:
            headline = f"**{len(rows)}** matching row(s)"
        breakdown = _plan_components_breakdown(question, rows, context)
        return headline + breakdown if breakdown else headline

    # Count intent
    if intent == "count" or (not metric_fields and _asks_how_many(question)):
        if group_field:
            counts_map: dict[str, int] = {}
            for row in rows:
                key = _plan_group_value(row, group_field)
                counts_map[key] = counts_map.get(key, 0) + 1
            ranked = sorted(counts_map.items(), key=lambda item: (-item[1], item[0]))
            return _format_plan_count_answer(source_key, group_field, ranked)
        count_unit_field = _plan_count_unit_field(question, rows)
        if count_unit_field:
            distinct = {
                _clean_text(r.get(count_unit_field))
                for r in rows if _clean_text(r.get(count_unit_field))
            }
            return (
                f"**{len(distinct)}** distinct {_humanize_field(count_unit_field).lower()}(s) "
                f"(across {len(rows)} matching row(s))."
            )
        return f"**{len(rows)}** matching row(s) from {source_key}."

    if not metric_fields:
        return ""

    wants_average = (
        intent == "average"
        or "average" in question
        or bool(re.search(r"\bavg\b", question))
        or any(
            isinstance(m, dict) and _clean_text(m.get("aggregation", "")).casefold() == "avg"
            for m in (qu_plan.get("metrics") or [])
        )
    )
    wants_ranking = intent == "ranking" or _clean_text(qu_plan.get("output_format") or "").casefold() == "ranked_list"

    # Grouped / comparison / ranking
    if group_field or intent in ("comparison", "ranking"):
        group_field = group_field or _default_group_field(rows)
        if not group_field:
            return ""
        grouped = _aggregate_plan_rows(rows, metric_fields, group_field, wants_average)
        if not grouped:
            return ""
        # Only collapse single-group results to a scalar when the group_by came from the
        # decomposer's default, not from an explicit "per X" / "by X" in the question.
        explicit_group = any(pat.search(question) for pat, _ in _QU_IMPLICIT_GROUP_PATTERNS)
        if len(grouped) <= 1 and not explicit_group:
            return ""
        sort_by = qu_plan.get("sort_by") or {}
        sort_field_raw = _clean_text(sort_by.get("field", ""))
        sort_order = _clean_text(sort_by.get("order", "desc")).casefold()
        sort_field = _resolve_row_field(rows, sort_field_raw) if sort_field_raw else ""
        if sort_field or wants_ranking:
            sf = sort_field or (metric_fields[0] if metric_fields else "")
            grouped = sorted(grouped, key=lambda r: _number(r.get(sf, 0)), reverse=(sort_order != "asc"))
        limit = qu_plan.get("limit")
        if isinstance(limit, int) and limit > 0:
            grouped = grouped[:limit]
        return _format_plan_grouped_answer(source_key, metric_fields, group_field, grouped, wants_average)

    # Scalar total / average (ungrouped)
    totals = {
        m: _clean_number(
            _average([_number(r.get(m)) for r in rows]) if wants_average
            else sum(_number(r.get(m)) for r in rows)
        )
        for m in metric_fields
    }
    label = "Average" if wants_average else "Total"
    parts_txt = [
        f"{_humanize_field(m)}: {_format_plan_metric_value(m, v)}"
        for m, v in totals.items()
    ]
    cond_label = _qu_conditions_label(conditions, condition_logic)
    suffix = f" (where {cond_label})" if cond_label else ""
    return f"{label}{suffix}: " + " | ".join(parts_txt)


# ── QU Confidence Scoring ─────────────────────────────────────────────────────

_QU_CONFIDENCE_THRESHOLD = float(os.getenv("CAPACITY_CHAT_QU_CONFIDENCE_THRESHOLD", "0.65") or "0.65")


def _compute_qu_confidence(
    qu_plan: dict[str, Any],
    answer: str,
    context: dict[str, Any],
    question: str,
) -> tuple[float, list[str]]:
    """Score 0.0–1.0 for how reliable the QU executor's answer is.

    Penalises: wrong/empty source, ineffective filters, thin answer for the intent,
    missing numeric output for aggregate intents.
    Returns (score, [human-readable reasons]) so the caller can log or surface them.
    """
    score = 1.0
    reasons: list[str] = []

    if not qu_plan or not answer:
        return 0.0, ["no plan or answer produced"]

    intent = _clean_text(qu_plan.get("intent", "")).casefold()
    source_key = _clean_text(qu_plan.get("primary_source", ""))
    conditions = qu_plan.get("conditions") or []
    time_scope = qu_plan.get("time_scope") or {}
    entities = qu_plan.get("entities") or []

    # Trend/prediction answers from the executor are inherently low-confidence
    if intent in ("trend", "prediction"):
        score -= 0.5
        reasons.append("time-series/prediction questions need full LLM analysis")

    # Re-fetch and re-filter rows (pure in-memory dict lookup, no I/O)
    all_rows = _rows_for_plan_source(source_key, context)

    if not all_rows:
        score -= 0.5
        reasons.append(f"data source '{source_key}' returned no rows")
    else:
        condition_logic = _clean_text(qu_plan.get("condition_logic") or "AND").upper()
        matched = _apply_qu_conditions(all_rows, conditions, condition_logic)

        if matched is None:
            score -= 0.4
            reasons.append("a filter condition referenced a field not found in the source")
        else:
            matched = _apply_qu_time_scope(matched, time_scope)
            matched = _apply_qu_entity_filters(matched, entities)

            if not matched:
                score -= 0.35
                reasons.append("no rows survived the filters")
            elif len(all_rows) < 3:
                score -= 0.25
                reasons.append(f"only {len(all_rows)} row(s) in source — possible wrong table")
            elif conditions and len(matched) == len(all_rows):
                # Every row passed despite conditions being present → field likely didn't resolve
                score -= 0.15
                reasons.append("conditions had zero selectivity — filter may not have matched real fields")

    # Answer-quality signals
    answer_lower = answer.casefold()

    if "no rows match" in answer_lower:
        score -= 0.35
        reasons.append("executor found no matching data")

    # Aggregate/average/ranking intents must produce numbers
    if intent in ("aggregate", "average", "ranking", "comparison") and not re.search(r"\d", answer):
        score -= 0.30
        reasons.append("numeric result expected but answer contains no numbers")

    # Thin answer for a complex question
    if len(question.split()) > 8 and len(answer.strip()) < 50:
        score -= 0.20
        reasons.append("answer too brief for the complexity of the question")

    # No filters at all for a non-trivial intent → may be an unfiltered aggregate
    time_type = time_scope.get("type", "none")
    if not conditions and not entities and time_type == "none":
        if intent not in ("list", "lookup", "breakdown", "count"):
            score -= 0.10
            reasons.append("no filters applied — answer may be an unfiltered aggregate")

    return round(max(0.0, min(1.0, score)), 2), reasons


def _build_chat_context(
    intelligence: dict[str, Any],
    tower_details: list[dict[str, Any]],
    summary: dict[str, Any] | None = None,
    daily_rows: list[dict[str, Any]] | None = None,
    details: list[dict[str, Any]] | None = None,
    downtime_reasons: list[dict[str, Any]] | None = None,
    book_details: list[dict[str, Any]] | None = None,
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
        runtime_segments = _runtime_segments_for_rows(v["rows"])
        tower_rows.append({
            "tower": t,
            "machine": v["machine"],
            "tower_name": v["tower_name"],
            "plants": sorted(v["plants"]),
            "uv_tower": v["uv_tower"],
            "runtime_min": _clean_number(v["runtime"]),
            "downtime_min": _clean_number(v["downtime"]),
            "loss_time_min": _clean_number(non_wait_lost_time),
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
        })
    tower_rows.sort(key=lambda r: -r["runtime_min"])

    def _slim_day(row: dict[str, Any]) -> dict[str, Any]:
        return {
            "run_date": row.get("run_date"),
            "weekday": _weekday_label(row.get("run_date")),
            "month": _month_label(row.get("run_date")),
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
    tower_usage_distribution = _build_tower_usage_distribution(tower_availability)
    tower_runtime_mix = _build_tower_runtime_mix(tower_day_rows)

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
    gnp_snp_folder_analysis = _build_gnp_snp_folder_analysis(
        folder_rows=details or [],
        tower_day_rows=tower_day_rows,
        tower_reason_attribution=tower_reason_attribution,
    )

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
        "towers": tower_rows,
        "tower_runtime_mix": tower_runtime_mix,
        "tower_availability": tower_availability,
        "tower_usage_distribution": tower_usage_distribution,
        "tower_days": tower_day_rows[:1500],
        "tower_days_all": tower_day_rows,
        "tower_weekday_summary": _tower_weekday_summary(tower_day_rows),
        "tower_month_summary": _tower_month_summary(tower_day_rows),
        "daily_efficiency": _daily_efficiency_summary(tower_day_rows),
        "tower_downtime_runs": tower_downtime_runs[:1000],
        "tower_downtime_runs_all": tower_downtime_runs,
        "tower_downtime_reason_attribution": tower_reason_attribution,
        "gnp_snp_folder_analysis": gnp_snp_folder_analysis,
        "uv_towers": uv_towers,
        "non_uv_towers": non_uv_towers,
        # delayed_pf and book_details used to be hard-capped here (500 rows each) with no uncapped
        # fallback, unlike tower_days/tower_downtime_runs above — that silently truncated the base
        # data the deterministic executor itself reads (_rows_for_plan_source has no "_all" variant
        # for either), undercounting "how many delayed nights" / edition-level questions on longer
        # date ranges. Sent in full now.
        "delayed_pf": delayed_pf_rows,
        "uv_nights": uv_nights,
        "downtime_by_reason": downtime_by_reason,
        "downtime_by_folder": downtime_by_folder,
        "editions_by_date": editions_by_date,
        "editions_by_folder": editions_by_folder,
        "editions_by_tower": editions_by_tower,
        "book_details": _select_book_details_for_llm(book_details or [], question, limit=None),
    }


def _compact_chat_context_for_llm(
    context: dict[str, Any],
    question: str = "",
    qu_plan: dict[str, Any] | None = None,
) -> dict[str, Any]:
    if qu_plan:
        return _focused_chat_context_for_llm(context, qu_plan, question)

    exact = context.get("exact_dashboard") or {}
    downtime_by_reason = context.get("downtime_by_reason") or {}
    tower_attribution = context.get("tower_downtime_reason_attribution") or {}
    gnp_snp_analysis = context.get("gnp_snp_folder_analysis") or {}

    # ── Plan-driven context selection ────────────────────────────────────────
    # The QU decomposer (an LLM that understands the question semantically) declares
    # `required_sources`: the exact set of tables it needs. We use this to decide which
    # detail tables get their full row count vs. a minimal stub.
    # This replaces keyword heuristics — the LLM knows what it needs, we just obey.
    required: set[str] = set()
    if qu_plan:
        for src in (qu_plan.get("required_sources") or []):
            s = _clean_text(src).casefold()
            if s:
                required.add(s)
        for sq in (qu_plan.get("sub_questions") or []):
            sq_src = _clean_text(sq.get("primary_source") or "").casefold()
            if sq_src:
                required.add(sq_src)

    def _req(key: str) -> bool:
        """True if key (or its parent/child) is in required_sources."""
        k = key.casefold()
        return k in required or any(
            r == k or r.startswith(k + ".") or k.startswith(r + ".")
            for r in required
        )

    def _lim(key: str, full: int, stub: int) -> int | None:
        """Return None (uncapped — every row) if the source is required, or if no plan is
        available at all (force_full_llm=True skips the decomposer entirely, and the frontend's
        default chat mode uses force_full_llm=True — so 'no plan' is the common case, not a rare
        edge case). Only when a plan DOES exist and explicitly does not need this table do we send
        the small stub — that's the one case where truncation doesn't risk cutting real data the
        question needs. Capped 'full' values used to still be small fixed numbers (e.g. 120 rows),
        which silently truncated the exact table a question needed on larger workbooks and
        produced wrong aggregates/counts. The overall context-size check further down (which
        degrades to _minimal_chat_context_for_llm) is the backstop against oversized requests, not
        a blanket per-table cap."""
        if not required or _req(key):
            return None
        return stub

    # tower_runtime_segments is a large table — only include when explicitly required, but send
    # every row once it is (no cap) so runtime/product-share math isn't computed off a truncated
    # subset of segments.
    wants_tower_runtime_segments = _req("tower_runtime_segments")
    tower_runtime_segment_rows = (
        _tower_runtime_segment_context_rows(
            context.get("tower_days_all") or context.get("tower_days") or [],
            "",
            limit=None,
        )
        if wants_tower_runtime_segments
        else []
    )

    # Pre-filter daily_efficiency by QU conditions so the LLM only receives qualifying nights.
    # This prevents the LLM from needing to self-filter large unordered row lists when the
    # QU executor correctly identifies the condition but escalates because it can't answer
    # the full compound question (e.g. "efficiency < 95% AND loss components").
    _daily_eff_all = context.get("daily_efficiency") or []
    _daily_eff_filtered = _daily_eff_all
    _qu_filtered_dates: set[str] | None = None

    if qu_plan:
        _ps = _clean_text(qu_plan.get("primary_source", "")).casefold()
        if _ps == "daily_efficiency":
            _conds = qu_plan.get("conditions") or []
            _cond_logic = _clean_text(qu_plan.get("condition_logic") or "AND").upper()
            _ts = qu_plan.get("time_scope") or {}
            _entities = qu_plan.get("entities") or []
            _filtered = _apply_qu_conditions(_daily_eff_all, _conds, _cond_logic)
            if _filtered is not None:
                _filtered = _apply_qu_time_scope(_filtered, _ts)
                _filtered = _apply_qu_entity_filters(_filtered, _entities)
                if _filtered:
                    _daily_eff_filtered = _filtered
                    _qu_filtered_dates = {
                        _clean_text(r.get("run_date") or "")
                        for r in _filtered
                        if r.get("run_date")
                    }

    return {
        "scope": context.get("scope") or {},
        "summary": context.get("summary") or {},
        "exact_dashboard": {
            "source": exact.get("source"),
            "scope": exact.get("scope") or {},
            "summary": exact.get("summary") or {},
            # daily and folders are one row per date/folder — sent uncapped so longer date ranges
            # never get silently truncated into a wrong aggregate.
            "daily": _compact_rows(exact.get("daily") or [], limit=None),
            "folders": _compact_rows(exact.get("folders") or [], limit=None),
            "folder_days": _compact_rows(
                exact.get("folder_days") or [],
                limit=_lim("exact_dashboard.folder_days", 120, 5),
            ),
            "complexity_downtime_by_code": _compact_rows(exact.get("complexity_downtime_by_code") or [], limit=None),
        },
        "folders": _compact_rows(context.get("folders") or [], limit=None),
        "unused_folders": context.get("unused_folders") or [],
        "speed": {
            "overall": (context.get("speed") or {}).get("overall") or {},
            "by_category": _compact_rows(((context.get("speed") or {}).get("by_category") or []), limit=None),
            "by_folder": _compact_rows(((context.get("speed") or {}).get("by_folder") or []), limit=None),
            "by_machine": _compact_rows(((context.get("speed") or {}).get("by_machine") or []), limit=None),
            # fastest/slowest/highest_complexity_share are already top-N ranked lists computed
            # upstream (sorted + sliced to ~4-6 items) — this limit is a harmless no-op, not a
            # truncation of a larger real answer.
            "fastest": _compact_rows(((context.get("speed") or {}).get("fastest") or []), limit=10),
            "slowest": _compact_rows(((context.get("speed") or {}).get("slowest") or []), limit=10),
            "highest_complexity_share": _compact_rows(
                ((context.get("speed") or {}).get("highest_complexity_share") or []),
                limit=10,
            ),
        },
        "complexity_vs_loss": _compact_rows(context.get("complexity_vs_loss") or [], limit=None),
        "complexity_by_code": _compact_rows(context.get("complexity_by_code") or [], limit=None),
        "complexity_downtime_by_code": _compact_rows(context.get("complexity_downtime_by_code") or [], limit=None),
        "loss_time": {
            "dominant_driver": (context.get("loss_time") or {}).get("dominant_driver"),
            "driver_totals": (context.get("loss_time") or {}).get("driver_totals"),
            # top_loss_days/low_loss_days are already top-N ranked lists computed upstream.
            "top_loss_days": _compact_rows(((context.get("loss_time") or {}).get("top_loss_days") or []), limit=20),
            "low_loss_days": _compact_rows(((context.get("loss_time") or {}).get("low_loss_days") or []), limit=20),
            # all_days is one row per date — sent uncapped; restricted to QU-filtered dates when an
            # efficiency condition is active
            "all_days": _compact_rows(
                [
                    r for r in ((context.get("loss_time") or {}).get("all_days") or [])
                    if not _qu_filtered_dates or _clean_text(r.get("run_date") or "") in _qu_filtered_dates
                ],
                limit=None,
            ),
        },
        "towers": _compact_rows(context.get("towers") or [], limit=None),
        "tower_runtime_segments": tower_runtime_segment_rows,
        "tower_runtime_mix": _compact_rows(context.get("tower_runtime_mix") or [], limit=None),
        "tower_days": _compact_rows(
            context.get("tower_days_all") or context.get("tower_days") or [],
            limit=_lim("tower_days", 150, 5),
        ),
        "tower_usage_distribution": _compact_rows(context.get("tower_usage_distribution") or [], limit=None),
        "tower_weekday_summary": _compact_rows(
            context.get("tower_weekday_summary") or [],
            limit=_lim("tower_weekday_summary", 500, 10),
        ),
        "tower_month_summary": _compact_rows(
            context.get("tower_month_summary") or [],
            limit=_lim("tower_month_summary", 500, 10),
        ),
        "daily_efficiency": _daily_eff_filtered,
        "tower_availability": context.get("tower_availability") or {},
        "tower_downtime_reason_attribution": {
            "attribution_note": tower_attribution.get("attribution_note"),
            "by_tower": _compact_rows(
                tower_attribution.get("by_tower") or [],
                limit=_lim("tower_downtime_reason_attribution", 200, 5),
            ),
            "by_tower_reason": _compact_rows(
                tower_attribution.get("by_tower_reason") or [],
                limit=_lim("tower_downtime_reason_attribution", 200, 5),
            ),
        },
        "gnp_snp_folder_analysis": {
            "definition": gnp_snp_analysis.get("definition"),
            # comparison_by_product_type is always tiny (2 rows: GNP vs SNP) — send unconditionally
            "comparison_by_product_type": _compact_rows(
                gnp_snp_analysis.get("comparison_by_product_type") or [], limit=10
            ),
            "gnp_loss_breakdown_by_folder": _compact_rows(
                gnp_snp_analysis.get("gnp_loss_breakdown_by_folder") or [],
                limit=_lim("gnp_snp_folder_analysis.gnp_loss_breakdown_by_folder", 120, 5),
            ),
            "nights_with_min_3_gnp_folders": _compact_rows(
                gnp_snp_analysis.get("nights_with_min_3_gnp_folders") or [],
                limit=_lim("gnp_snp_folder_analysis.nights_with_min_3_gnp_folders", 120, 5),
            ),
            "delayed_finish_complexity": _compact_rows(
                gnp_snp_analysis.get("delayed_finish_complexity") or [],
                limit=_lim("gnp_snp_folder_analysis.delayed_finish_complexity", 120, 5),
            ),
            "web_break_gnp_snp_tower_comparison": _compact_rows(
                gnp_snp_analysis.get("web_break_gnp_snp_tower_comparison") or [],
                limit=_lim("gnp_snp_folder_analysis.web_break_gnp_snp_tower_comparison", 200, 5),
            ),
            "correlation_summary": gnp_snp_analysis.get("correlation_summary") or {},
        },
        "delayed_pf": _compact_rows(
            context.get("delayed_pf") or [],
            limit=_lim("delayed_pf", 150, 5),
        ),
        "uv_nights": {
            "definition": (context.get("uv_nights") or {}).get("definition"),
            "gnp_nights": (context.get("uv_nights") or {}).get("gnp_nights") or [],
            "snp_nights": (context.get("uv_nights") or {}).get("snp_nights") or [],
            "nights": _compact_rows(
                (context.get("uv_nights") or {}).get("nights") or [],
                limit=_lim("uv_nights", 120, 5),
            ),
        },
        "downtime_by_reason": {
            "top_reasons": _compact_rows(downtime_by_reason.get("top_reasons") or [], limit=None),
        },
        "downtime_by_folder": _compact_rows(context.get("downtime_by_folder") or [], limit=None),
        "editions_by_date": _compact_rows(
            context.get("editions_by_date") or [],
            limit=_lim("editions_by_date", 300, 5),
        ),
        "editions_by_folder": _compact_rows(
            context.get("editions_by_folder") or [],
            limit=_lim("editions_by_folder", 300, 5),
        ),
        "editions_by_tower": _compact_rows(
            context.get("editions_by_tower") or [],
            limit=_lim("editions_by_tower", 300, 5),
        ),
        "book_details": _compact_rows(
            context.get("book_details") or [],
            limit=_lim("book_details", 80, 5),
        ),
    }


def _focused_chat_context_for_llm(
    context: dict[str, Any],
    qu_plan: dict[str, Any],
    question: str = "",
) -> dict[str, Any]:
    """Serialize only plan-selected sources, filtered before tokenization.

    This keeps the decomposer useful without making its required source expensive: a question
    about one folder's GNP nights sends those matching folder-night rows, not every folder-night
    plus dozens of unrelated tables.
    """
    primary_source = _clean_text(qu_plan.get("primary_source"))
    source_keys: list[str] = []
    for source in [
        primary_source,
        *(qu_plan.get("required_sources") or []),
        *[
            sub_question.get("primary_source")
            for sub_question in (qu_plan.get("sub_questions") or [])
            if isinstance(sub_question, dict)
        ],
    ]:
        key = _clean_text(source)
        if key and key not in source_keys:
            source_keys.append(key)

    conditions = qu_plan.get("conditions") or []
    condition_logic = _clean_text(qu_plan.get("condition_logic") or "AND").upper()
    time_scope = qu_plan.get("time_scope") or {}
    entities = qu_plan.get("entities") or []
    selected_sources: dict[str, list[dict[str, Any]]] = {}
    source_selection: list[dict[str, Any]] = []

    for source_key in source_keys[:6]:
        source_rows = _rows_for_plan_source(source_key, context)
        selected_rows = list(source_rows)
        condition_status = "not_applicable"
        if source_key.casefold() == primary_source.casefold():
            conditioned = _apply_qu_conditions(selected_rows, conditions, condition_logic)
            if conditioned is None:
                # Keep the relevant source available to the answering model when the decomposer
                # invents a field. An empty replacement would turn a recoverable planning error
                # into a false "no data" answer. Entity/time filters below still keep this focused.
                condition_status = "invalid_field_unapplied"
            else:
                selected_rows = conditioned
                condition_status = "applied" if conditions else "none"
        selected_rows = _apply_qu_time_scope(selected_rows, time_scope)
        selected_rows = _apply_qu_entity_filters(selected_rows, entities)
        selected_sources[source_key] = _compact_rows(selected_rows, limit=None)
        source_selection.append({
            "source": source_key,
            "source_row_count": len(source_rows),
            "selected_row_count": len(selected_rows),
            "condition_status": condition_status,
        })

    focused_context = {
        "scope": context.get("scope") or {},
        "summary": context.get("summary") or {},
        "context_note": (
            "Token-optimized context. selected_sources contains every row matching the decomposer "
            "plan; source_selection reports full and selected row counts."
        ),
        "source_selection": source_selection,
        "selected_sources": selected_sources,
    }
    evidence = _date_count_evidence_for_plan(qu_plan, question, context)
    if evidence:
        focused_context["authoritative_result"] = {
            key: value
            for key, value in evidence.items()
            if key not in {"answer", "required_values"}
        }
    return focused_context


def _minimal_chat_context_for_llm(
    context: dict[str, Any],
    qu_plan: dict[str, Any] | None = None,
) -> dict[str, Any]:
    """Last-resort compact context for large workbooks that exceed model input limits.

    Unlike _compact_chat_context_for_llm, row AND nested-list caps here are deliberately kept
    tight (max_list_items=10) regardless of what triggered the fallback — this path only exists to
    guarantee the request fits, so it must not inherit the "send every row/list item" behavior of
    the regular path.
    """
    primary_source = _clean_text((qu_plan or {}).get("primary_source") or "")
    focused = _focused_chat_context_for_llm(context, qu_plan or {}) if primary_source else {}
    source_rows = ((focused.get("selected_sources") or {}).get(primary_source) or [])
    metric_aggregates = _aggregate_plan_metrics_for_context(source_rows, qu_plan or {})
    exact = context.get("exact_dashboard") or {}
    return {
        "scope": context.get("scope") or {},
        "summary": context.get("summary") or {},
        "context_note": (
            "Large focused context was reduced to stay under the model input limit. "
            "primary_source_aggregates were calculated from every selected row; "
            "primary_source_rows is only a sample."
        ),
        "primary_source": primary_source,
        "primary_source_selected_row_count": len(source_rows),
        "primary_source_aggregates": metric_aggregates,
        "primary_source_rows": _compact_rows(source_rows, limit=80, max_list_items=10),
        "exact_dashboard": {
            "summary": (exact.get("summary") or {}),
            "daily": _compact_rows(exact.get("daily") or [], limit=120, max_list_items=10),
            "folders": _compact_rows(exact.get("folders") or [], limit=80, max_list_items=10),
            "folder_days": _compact_rows(exact.get("folder_days") or [], limit=30, max_list_items=10),
        },
        "downtime_by_reason": {
            "top_reasons": _compact_rows(
                ((context.get("downtime_by_reason") or {}).get("top_reasons") or []), limit=30, max_list_items=10
            ),
        },
    }


def _aggregate_plan_metrics_for_context(
    rows: list[dict[str, Any]],
    qu_plan: dict[str, Any],
) -> dict[str, dict[str, Any]]:
    aggregates: dict[str, dict[str, Any]] = {}
    for metric in qu_plan.get("metrics") or []:
        if isinstance(metric, dict):
            requested_field = _clean_text(metric.get("field"))
            aggregation = _clean_text(metric.get("aggregation") or "sum").casefold()
        else:
            requested_field = _clean_text(metric)
            aggregation = "sum"
        field = _resolve_row_field(rows, requested_field)
        if not field:
            continue
        values = [_number(row.get(field)) for row in rows]
        if aggregation == "avg":
            value = _average(values)
        elif aggregation == "max":
            value = max(values, default=0.0)
        elif aggregation == "min":
            value = min(values, default=0.0)
        elif aggregation == "count":
            value = len(values)
        else:
            value = sum(values)
        aggregates[field] = {
            "aggregation": aggregation,
            "value": _clean_number(value),
            "row_count": len(values),
        }
    return aggregates


def _chat_context_to_toon(context: dict[str, Any]) -> str:
    lines: list[str] = []
    _write_toon_object(context, lines, indent=0)
    return "\n".join(lines)


def _write_toon_object(value: dict[str, Any], lines: list[str], indent: int) -> None:
    for key, child in value.items():
        _write_toon_value(str(key), child, lines, indent)


def _write_toon_value(key: str, value: Any, lines: list[str], indent: int) -> None:
    prefix = "  " * indent
    if isinstance(value, dict):
        if not value:
            lines.append(f"{prefix}{key}: {{}}")
            return
        lines.append(f"{prefix}{key}:")
        _write_toon_object(value, lines, indent + 1)
        return

    if isinstance(value, list):
        _write_toon_list(key, value, lines, indent)
        return

    lines.append(f"{prefix}{key}: {_toon_scalar(value)}")


def _write_toon_list(key: str, values: list[Any], lines: list[str], indent: int) -> None:
    prefix = "  " * indent
    if not values:
        lines.append(f"{prefix}{key}[0]:")
        return

    dict_rows = [row for row in values if isinstance(row, dict)]
    if len(dict_rows) == len(values):
        headers = _toon_headers(dict_rows)
        lines.append(f"{prefix}{key}[{len(values)}]{{{','.join(headers)}}}:")
        row_prefix = "  " * (indent + 1)
        for row in dict_rows:
            lines.append(f"{row_prefix}{_toon_csv_row([_toon_cell(row.get(header)) for header in headers])}")
        return

    if all(not isinstance(item, (dict, list)) for item in values):
        lines.append(f"{prefix}{key}[{len(values)}]: {_toon_csv_row([_toon_cell(item) for item in values])}")
        return

    lines.append(f"{prefix}{key}[{len(values)}]:")
    item_prefix = "  " * (indent + 1)
    for item in values:
        if isinstance(item, dict):
            lines.append(f"{item_prefix}-:")
            _write_toon_object(item, lines, indent + 2)
        elif isinstance(item, list):
            _write_toon_list("-", item, lines, indent + 1)
        else:
            lines.append(f"{item_prefix}-: {_toon_scalar(item)}")


def _toon_headers(rows: list[dict[str, Any]]) -> list[str]:
    headers: list[str] = []
    seen: set[str] = set()
    for row in rows:
        for key in row.keys():
            header = str(key)
            if header not in seen:
                headers.append(header)
                seen.add(header)
    return headers


def _toon_csv_row(cells: list[str]) -> str:
    return ",".join(cells)


def _toon_cell(value: Any) -> str:
    if value is None:
        return "null"
    if isinstance(value, bool):
        return "true" if value else "false"
    if isinstance(value, (int, float)):
        return _toon_number(value)
    if isinstance(value, (dict, list)):
        return _toon_csv_escape(json.dumps(value, separators=(",", ":"), ensure_ascii=True, default=str))
    return _toon_csv_escape(str(value))


def _toon_scalar(value: Any) -> str:
    if value is None:
        return "null"
    if isinstance(value, bool):
        return "true" if value else "false"
    if isinstance(value, (int, float)):
        return _toon_number(value)
    if isinstance(value, (dict, list)):
        return json.dumps(value, separators=(",", ":"), ensure_ascii=True, default=str)
    text = str(value)
    if _toon_needs_quotes(text):
        return json.dumps(text, ensure_ascii=True)
    return text


def _toon_number(value: int | float) -> str:
    if isinstance(value, float) and not isfinite(value):
        return "null"
    return json.dumps(value, ensure_ascii=True)


def _toon_csv_escape(text: str) -> str:
    if _toon_needs_csv_quotes(text):
        return '"' + text.replace('"', '""') + '"'
    return text


def _toon_needs_quotes(text: str) -> bool:
    if text == "" or text != text.strip():
        return True
    lowered = text.casefold()
    if lowered in {"null", "true", "false"}:
        return True
    return any(ch in text for ch in [":", ",", "{", "}", "[", "]", '"', "\n", "\r"]) or any(
        ord(ch) > 127 for ch in text
    )


def _toon_needs_csv_quotes(text: str) -> bool:
    if text == "" or text != text.strip():
        return True
    if text.casefold() in {"null", "true", "false"}:
        return True
    return any(ch in text for ch in [",", '"', "\n", "\r"]) or any(ord(ch) > 127 for ch in text)


def _compact_rows(
    rows: list[dict[str, Any]],
    limit: int | None,
    include_keys: set[str] | None = None,
    max_list_items: int | None = None,
) -> list[dict[str, Any]]:
    selected = rows if limit is None else rows[:limit]
    return [
        _compact_row(row, include_keys=include_keys, max_list_items=max_list_items)
        for row in selected if isinstance(row, dict)
    ]


def _compact_row(
    row: dict[str, Any],
    include_keys: set[str] | None = None,
    max_list_items: int | None = None,
) -> dict[str, Any]:
    include_keys = include_keys or set()
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
        if key in omitted_keys and key not in include_keys:
            continue
        if isinstance(value, list):
            items = value if max_list_items is None else value[:max_list_items]
            compact[key] = [
                _compact_list_value(item, include_keys=include_keys, max_list_items=max_list_items)
                for item in items
            ]
            if max_list_items is not None and len(value) > max_list_items:
                compact[f"{key}_omitted_count"] = len(value) - max_list_items
        elif isinstance(value, dict):
            compact[key] = {
                child_key: _compact_list_value(child_value, include_keys=include_keys, max_list_items=max_list_items)
                for child_key, child_value in value.items()
                if child_key not in omitted_keys or child_key in include_keys
            }
        else:
            compact[key] = value
    return compact


def _compact_list_value(
    value: Any,
    include_keys: set[str] | None = None,
    max_list_items: int | None = None,
) -> Any:
    if isinstance(value, dict):
        return _compact_row(value, include_keys=include_keys, max_list_items=max_list_items)
    return value


def _wants_tower_runtime_segment_context(question: str) -> bool:
    has_tower = "tower" in question or "towers" in question
    has_runtime = "runtime" in question or "run time" in question
    has_product_or_type = any(
        term in question
        for term in ["snp", "gnp", "uv", "glossy", "standard", "product", "complexity", "c1", "c2", "c3", "c4"]
    )
    has_calculation = any(
        term in question
        for term in ["percentage", "percent", "%", "share", "ratio", "split", "total", "calculate", "utilized", "utilised"]
    )
    return has_tower and has_runtime and has_product_or_type and has_calculation


def _tower_runtime_segment_context_rows(
    tower_day_rows: list[dict[str, Any]],
    question: str,
    limit: int | None,
) -> list[dict[str, Any]]:
    wants_non_uv = any(term in question for term in ["non uv", "non-uv", "non gnp", "non-gnp"])
    wants_gnp_uv_tower = (
        not wants_non_uv
        and ("tower" in question or "towers" in question)
        and any(term in question for term in ["gnp", "uv", "glossy"])
    )

    rows: list[dict[str, Any]] = []
    for row in tower_day_rows or []:
        uv_tower = bool(row.get("uv_tower"))
        if wants_non_uv and uv_tower:
            continue
        if wants_gnp_uv_tower and not uv_tower:
            continue

        tower_type_key = "gnp_uv" if uv_tower else "non_uv"
        tower_type = "GNP/UV tower" if uv_tower else "Non-GNP/non-UV tower"
        for segment in row.get("runtime_segments") or []:
            minutes = _number(segment.get("minutes"))
            if minutes <= 0:
                continue
            if _is_snp_segment(segment):
                product_type = "SNP"
            elif _is_gnp_segment(segment):
                product_type = "GNP"
            else:
                product_type = "Unknown"
            rows.append({
                "run_date": row.get("run_date"),
                "tower": row.get("tower"),
                "tower_type_key": tower_type_key,
                "tower_type": tower_type,
                "uv_tower": uv_tower,
                "product_type": product_type,
                "complexity_code": segment.get("complexity_code"),
                "category": segment.get("category"),
                "minutes": _clean_number(minutes),
            })
            if limit is not None and len(rows) >= limit:
                return rows

    return rows


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
    gnp_night_lookup_all = _gnp_night_lookup(folder_rows)
    exact_folder_day_rows_all = [_exact_folder_day_row(row, gnp_night_lookup_all) for row in folder_rows]
    night_classification = _build_gnp_night_classification(folder_rows, dates)
    delayed_pf_rows = _build_delayed_pf_rows(folder_rows)
    complexity_downtime_by_code = _complexity_downtime_by_code(folder_rows)
    total_available = sum(_number(row.get("available_capacity_min")) for row in exact_daily_rows)
    total_runtime = sum(_number(row.get("runtime_min")) for row in exact_daily_rows)
    total_loss_time = sum(_number(row.get("loss_time_min")) for row in exact_daily_rows)
    total_waiting_time = sum(_number(row.get("waiting_time_min")) for row in exact_daily_rows)
    total_downtime = sum(_number(row.get("downtime_min")) for row in exact_daily_rows)
    total_overrun_minutes = sum(_number(row.get("overrun_minutes")) for row in folder_rows)
    total_spare_time = sum(_number(row.get("spare_time_min")) for row in exact_daily_rows)
    total_unplanned_time = sum(_number(row.get("unplanned_time_min")) for row in exact_daily_rows)
    planned_available = max(total_available - total_unplanned_time, 0)
    total_utilized_time = (
        total_runtime + total_overrun_minutes + total_loss_time + total_waiting_time + total_downtime
    ) or (
        _number(summary.get("total_runtime")) + _number(summary.get("total_overrun_minutes"))
        + _number(summary.get("total_lost_time")) + _number(summary.get("total_waiting_time"))
        + _number(summary.get("total_downtime"))
    )

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
            "total_overrun_minutes": _clean_number(total_overrun_minutes or summary.get("total_overrun_minutes")),
            "total_utilized_time_min": _clean_number(total_utilized_time),
            "total_spare_time_min": _clean_number(total_spare_time or summary.get("total_buffer_time")),
            "total_unplanned_time_min": _clean_number(total_unplanned_time or summary.get("total_idle_time")),
            "average_utilization_pct": _percentage(
                total_utilized_time, total_available or _number(summary.get("total_available_capacity"))
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
            "utilization_formula": "(Runtime + Overrun + Lost Time + Wait Time + Downtime) / Available Time * 100",
        },
        "daily": exact_daily_rows,
        "folders": exact_folder_rows,
        "folder_days": exact_folder_day_rows,
        "folder_days_all": exact_folder_day_rows_all,
        "folder_day_note": folder_day_note,
        "complexity_downtime_by_code": complexity_downtime_by_code,
        "night_classification": night_classification,
        "delayed_pf": delayed_pf_rows[:500],
    }


_CHART_PIE_TERMS = [
    "pie chart", "pie graph", "percentage split", "% split", "proportion of", "proportion between",
    "share of", "composition of", "split between", "capacity split", "breakdown of capacity",
    "split of capacity",
]
# "trend" on its own implies a time-series visualisation; the rest require an explicit chart word.
_CHART_LINE_TERMS = ["trend", "trendline", "line chart", "line graph", "track the"]
# Bar chart only when the user actually asks for a chart/plot/graph of a comparison.
_CHART_EXPLICIT_WORDS = ["chart", "plot", "graph", "visualize", "visualise"]
_CHART_BAR_MODIFIERS = [
    "bar", "column", "compare", "comparison", "rank", "ranking",
    "by folder", "by tower", "per folder", "per tower", "each folder", "each tower",
]


def _detect_chart_intent(question: str) -> str | None:
    if any(term in question for term in _CHART_PIE_TERMS):
        return "pie"
    if any(term in question for term in _CHART_LINE_TERMS):
        return "line"
    # Generic explicit chart request — determine type from modifier words
    if any(word in question for word in _CHART_EXPLICIT_WORDS):
        if any(mod in question for mod in _CHART_BAR_MODIFIERS):
            return "bar"
        # "line" / "time" / "over time" / "daily" modifiers → line chart
        if any(t in question for t in ["line", "time", "daily", "over time", "day by day"]):
            return "line"
        return "bar"  # default type for bare "show me a chart of X"
    return None


_WEEKDAY_FILTER_MAP: dict[str, list[str]] = {
    "monday": ["Monday"],
    "tuesday": ["Tuesday"],
    "wednesday": ["Wednesday"],
    "thursday": ["Thursday"],
    "friday": ["Friday"],
    "saturday": ["Saturday"],
    "sunday": ["Sunday"],
    "weekend": ["Friday", "Saturday", "Sunday"],
    "weekends": ["Friday", "Saturday", "Sunday"],
    "weekday": ["Monday", "Tuesday", "Wednesday", "Thursday"],
    "weekdays": ["Monday", "Tuesday", "Wednesday", "Thursday"],
}


def _weekday_filter_label(question: str) -> str:
    # Prefer a named group ("weekends", "weekdays") if present; otherwise list all matched days.
    for keyword in ("weekends", "weekend", "weekdays", "weekday"):
        if keyword in question:
            return keyword
    matched: list[str] = []
    seen: set[str] = set()
    for keyword, days in _WEEKDAY_FILTER_MAP.items():
        if keyword in question:
            for day in days:
                if day not in seen:
                    matched.append(day)
                    seen.add(day)
    return ", ".join(matched) if matched else ""


def _apply_weekday_filter(rows: list[dict[str, Any]], question: str) -> list[dict[str, Any]]:
    target_days: set[str] = set()
    for keyword, days in _WEEKDAY_FILTER_MAP.items():
        if keyword in question:
            target_days.update(days)
    if not target_days:
        return rows
    return [row for row in rows if _clean_text(row.get("weekday")) in target_days]


def _chart_metric_spec_from_context(question: str, history: list[dict[str, Any]]) -> dict[str, Any] | None:
    """Resolve the chart metric from the current question first, then walk back through
    recent user turns in history.  Returns None (no chart) rather than defaulting to a
    metric that isn't relevant to what the user asked."""
    spec = _daily_average_metric_spec(question)
    if spec:
        return spec
    for turn in reversed((history or [])[-8:]):
        if _clean_text(turn.get("role")) != "user":
            continue
        prior = _clean_text(turn.get("content", "")).casefold()
        spec = _daily_average_metric_spec(prior)
        if spec:
            return spec
    return None


def _chart_metric_value(row: dict[str, Any], spec: dict[str, Any]) -> float:
    keys = spec.get("daily_keys") or [spec.get("daily_key")]
    return sum(_number(row.get(key)) for key in keys if key)


def _build_line_chart(
    question: str,
    exact_dashboard: dict[str, Any],
    spec: dict[str, Any] | None,
) -> dict[str, Any] | None:
    if spec is None:
        return None
    daily_rows = exact_dashboard.get("daily") or []
    if not daily_rows:
        return None
    filtered = _apply_weekday_filter(daily_rows, question)
    rows = filtered if filtered else daily_rows
    points = []
    for row in sorted(rows, key=lambda r: _clean_text(r.get("run_date"))):
        date = _clean_text(row.get("run_date"))
        if not date:
            continue
        points.append({"label": date, "value": _clean_number(_chart_metric_value(row, spec))})
    if not points:
        return None
    unit = spec.get("unit", "")
    weekday_label = _weekday_filter_label(question)
    title = f"{spec['label']} trend" + (f" — {weekday_label}" if weekday_label else " by day")
    if unit:
        title += f" ({unit})"
    return {"type": "line", "title": title, "metric_label": spec["label"], "unit": unit, "data": points[:120]}


def _build_pie_chart(
    question: str,
    exact_dashboard: dict[str, Any],
    context: dict[str, Any],
    spec: dict[str, Any] | None,
) -> dict[str, Any] | None:
    by_folder = any(term in question for term in ["by folder", "per folder", "each folder", "across folders"])
    by_tower = any(term in question for term in ["by tower", "per tower", "each tower", "across towers"])
    if by_folder or by_tower:
        if spec is None:
            return None
        rows = (exact_dashboard.get("folders") or []) if by_folder else (context.get("towers") or [])
        name_key = "resource" if by_folder else "tower"
        points = [
            {"label": _clean_text(row.get(name_key)), "value": _clean_number(_chart_metric_value(row, spec))}
            for row in rows
            if _clean_text(row.get(name_key)) and _chart_metric_value(row, spec) > 0
        ]
        if not points:
            return None
        points.sort(key=lambda p: -p["value"])
        dim_label = "folder" if by_folder else "tower"
        return {
            "type": "pie",
            "title": f"{spec['label']} share by {dim_label}",
            "metric_label": spec["label"],
            "unit": spec.get("unit", ""),
            "data": points[:15],
        }

    summary = exact_dashboard.get("summary") or {}
    slices = [
        ("Run Time", summary.get("total_runtime_min")),
        ("Lost Time", summary.get("total_loss_time_min")),
        ("Downtime", summary.get("total_downtime_min")),
        ("Wait Time", summary.get("total_waiting_time_min")),
        ("Spare Time", summary.get("total_spare_time_min")),
    ]
    points = [{"label": label, "value": _clean_number(value)} for label, value in slices if _number(value) > 0]
    if not points:
        return None
    return {"type": "pie", "title": "Capacity split (minutes)", "metric_label": "Capacity split", "unit": "min", "data": points}


def _build_bar_chart(
    question: str,
    exact_dashboard: dict[str, Any],
    context: dict[str, Any],
    spec: dict[str, Any] | None,
) -> dict[str, Any] | None:
    if _is_tower_usage_distribution_question(question):
        return _tower_usage_distribution_chart(context)

    if spec is None:
        return None
    by_tower = "tower" in question and "folder" not in question
    rows = (context.get("towers") or []) if by_tower else (exact_dashboard.get("folders") or [])
    name_key = "tower" if by_tower else "resource"
    points = [
        {"label": _clean_text(row.get(name_key)), "value": _clean_number(_chart_metric_value(row, spec))}
        for row in rows
        if _clean_text(row.get(name_key))
    ]
    if not points or not any(p["value"] for p in points):
        return None
    points.sort(key=lambda p: -p["value"])
    dim_label = "tower" if by_tower else "folder"
    return {
        "type": "bar",
        "title": f"{spec['label']} by {dim_label}",
        "metric_label": spec["label"],
        "unit": spec.get("unit", ""),
        "data": points[:25],
    }


def _build_chart_from_plan(
    plan: dict[str, Any] | None,
    context: dict[str, Any],
    question: str,
    history: list[dict[str, Any]] | None = None,
) -> dict[str, Any] | None:
    """Build a chart directly from the plan that was used to compute the answer.
    Because we re-derive the same filtered rows the answer was built from, the chart
    always matches the answer table — no keyword-guessing needed."""
    if not plan:
        return None
    source_key = _clean_text(plan.get("primary_source"))
    rows = _rows_for_plan_source(source_key, context)
    if not rows:
        return None
    filtered = _apply_plan_filters(rows, plan.get("filters") or {})
    if filtered is None:
        filtered = rows
    rows = filtered or rows
    # QU plans use "conditions"/"time_scope"/"entities" instead of the old "filters" dict
    conditions = plan.get("conditions") or []
    if conditions:
        qu_filtered = _apply_qu_conditions(rows, conditions, plan.get("condition_logic", "AND"))
        if qu_filtered is not None:
            rows = qu_filtered
    rows = _apply_qu_time_scope(rows, plan.get("time_scope") or {})
    rows = _apply_qu_entity_filters(rows, plan.get("entities") or [])

    # Prefer the metric derived from question+history over the planner's first field —
    # the planner sees only the current question, so "plot for weekends" with prior "spare
    # time trend" history would produce runtime, not spare time.
    history_spec = _chart_metric_spec_from_context(question, history or [])
    if history_spec:
        candidate = history_spec.get("daily_key") or ""
        resolved = _resolve_row_field(rows, candidate) if candidate else None
        primary_metric = resolved if resolved else None
    else:
        primary_metric = None

    if primary_metric is None:
        metric_fields = _plan_metric_fields(plan, rows, question)
        if not metric_fields:
            return None
        primary_metric = metric_fields[0]

    date_field = _resolve_row_field(rows, "run_date")
    group_by_hint = _clean_text(plan.get("group_by")).casefold()
    intent = _clean_text(plan.get("intent")).casefold()

    # Time-series: source has dates and no categorical group_by → line chart
    if date_field and group_by_hint in ("", "date", "none", "run_date"):
        date_rows = _apply_weekday_filter(
            sorted(rows, key=lambda r: _clean_text(r.get(date_field))),
            question,
        )
        if not date_rows:
            date_rows = sorted(rows, key=lambda r: _clean_text(r.get(date_field)))
        limit = plan.get("limit")
        if limit:
            sort_order = _clean_text((plan.get("sort_by") or {}).get("order", "asc")).casefold()
            date_rows = date_rows[-limit:] if sort_order == "desc" else date_rows[:limit]
        points = [
            {"label": _clean_text(r.get(date_field)), "value": _clean_number(_number(r.get(primary_metric)))}
            for r in date_rows
            if _clean_text(r.get(date_field))
        ]
        if len(points) >= 2:
            unit = _metric_suffix(primary_metric).strip()
            weekday_label = _weekday_filter_label(question)
            title = f"{_humanize_field(primary_metric)} trend" + (f" — {weekday_label}" if weekday_label else " by date")
            return {"type": "line", "title": title, "metric_label": _humanize_field(primary_metric), "unit": unit, "data": points[:120]}

    # Categorical comparison → bar chart
    group_field = _plan_group_field(plan, rows, question)
    if group_field:
        grouped = _aggregate_plan_rows(rows, [primary_metric], group_field, intent == "average")
        if grouped:
            points = [
                {"label": _plan_group_value(r, group_field), "value": _clean_number(_number(r.get(primary_metric)))}
                for r in sorted(grouped, key=lambda r: -_number(r.get(primary_metric)))
            ]
            if points:
                unit = _metric_suffix(primary_metric).strip()
                return {
                    "type": "bar",
                    "title": f"{_humanize_field(primary_metric)} by {_humanize_field(group_field)}",
                    "metric_label": _humanize_field(primary_metric),
                    "unit": unit,
                    "data": points[:25],
                }
    return None


_DATE_HEADER_TERMS = {"date", "run date", "run_date", "month", "week", "day"}
_CATEGORY_HEADER_TERMS = {"folder", "tower", "resource", "reason", "machine", "plant", "edition", "type", "complexity", "code", "category"}
_SKIP_Y_TERMS = {"rows", "row", "active nights", "nights", "count", "id", "ids", "rank"}


def _normalize_header(h: str) -> str:
    return re.sub(r"[^a-z0-9 ]", "", _clean_text(h).casefold()).strip()


def _parse_cell_number(cell: str) -> float | None:
    cleaned = re.sub(r"[^\d.\-]", "", re.sub(r",", "", _clean_text(cell)))
    try:
        return float(cleaned) if cleaned else None
    except ValueError:
        return None


def _parse_markdown_table(text: str) -> list[dict[str, str]]:
    headers: list[str] | None = None
    rows: list[dict[str, str]] = []
    for line in text.split("\n"):
        stripped = line.strip()
        if not (stripped.startswith("|") and stripped.endswith("|")):
            if headers:
                break  # end of current table
            continue
        cells = [re.sub(r"\*+", "", c).strip() for c in stripped[1:-1].split("|")]
        if headers is None:
            headers = cells
        elif all(re.fullmatch(r"[-: ]+", c) for c in cells):
            continue  # separator row
        else:
            rows.append(dict(zip(headers, cells)))
    return rows


def _pick_chart_columns(
    headers: list[str],
    rows: list[dict[str, str]],
    question: str,
    history: list[dict[str, Any]],
) -> tuple[str | None, str | None, str]:
    """Return (x_col, y_col, chart_type) choosing the most semantically relevant columns."""
    # Identify x-axis (date or category)
    x_col: str | None = None
    for h in headers:
        hn = _normalize_header(h)
        if any(t in hn for t in _DATE_HEADER_TERMS):
            x_col = h
            break
    chart_type = "line" if x_col else "bar"
    if x_col is None:
        for h in headers:
            hn = _normalize_header(h)
            if any(t in hn for t in _CATEGORY_HEADER_TERMS):
                x_col = h
                break

    # Build candidate y-columns: all headers with predominantly numeric non-zero values
    candidates: list[str] = []
    for h in headers:
        if h == x_col:
            continue
        hn = _normalize_header(h)
        if any(t in hn for t in _SKIP_Y_TERMS):
            continue
        nums = [_parse_cell_number(row.get(h, "")) for row in rows]
        valid = [n for n in nums if n is not None]
        if valid and any(abs(n) > 0 for n in valid):
            candidates.append(h)

    if not candidates:
        return x_col, None, chart_type

    # Prefer the column that best matches the metric spec derived from question + history
    spec = _chart_metric_spec_from_context(question, history)
    if spec:
        spec_label = _normalize_header(spec.get("label", ""))
        spec_key = _normalize_header(spec.get("daily_key") or "")
        for h in candidates:
            hn = _normalize_header(h)
            if spec_label and spec_label in hn:
                return x_col, h, chart_type
            if spec_key and spec_key.replace("min", "").strip() in hn:
                return x_col, h, chart_type
        # Spec is known but the column isn't in this table — returning a wrong column would
        # silently show a different metric than the user asked for. Return None so the
        # fallback path uses exact_dashboard data for the correct metric instead.
        return x_col, None, chart_type

    # No spec: fall back to the candidate with the highest average value
    def avg_val(h: str) -> float:
        nums = [_parse_cell_number(row.get(h, "")) for row in rows]
        valid = [n for n in nums if n is not None]
        return sum(valid) / len(valid) if valid else 0

    y_col = max(candidates, key=avg_val)
    return x_col, y_col, chart_type


def _build_chart_from_answer(
    answer_text: str,
    question: str,
    history: list[dict[str, Any]] | None = None,
) -> dict[str, Any] | None:
    """Parse the table in the bot's answer and build a chart directly from those values.
    This means the chart always matches exactly what is shown in the answer text."""
    rows = _parse_markdown_table(answer_text)
    if len(rows) < 2:
        return None

    headers = list(rows[0].keys())
    q = _clean_text(question).casefold()
    x_col, y_col, chart_type = _pick_chart_columns(headers, rows, q, history or [])

    if not x_col or not y_col:
        return None

    # Apply weekday filter if asked
    if any(kw in q for kw in _WEEKDAY_FILTER_MAP):
        x_header_norm = _normalize_header(x_col)
        is_date_x = any(t in x_header_norm for t in _DATE_HEADER_TERMS)
        if is_date_x:
            filtered = _apply_weekday_filter(
                [{"weekday": row.get("Weekday") or row.get("weekday", ""), **row} for row in rows],
                q,
            )
            rows = filtered if filtered else rows

    points = []
    for row in rows:
        x_val = re.sub(r"\*+", "", row.get(x_col, "")).strip()
        y_num = _parse_cell_number(row.get(y_col, ""))
        if x_val and y_num is not None:
            points.append({"label": x_val, "value": _clean_number(y_num)})

    if len(points) < 2:
        return None

    metric_label = re.sub(r"\*+", "", y_col).strip()
    spec = _chart_metric_spec_from_context(q, history or [])
    unit = spec.get("unit", "") if spec else ""

    weekday_label = _weekday_filter_label(q)
    if chart_type == "line":
        title = f"{metric_label} trend" + (f" — {weekday_label}" if weekday_label else " over time")
    else:
        x_label = re.sub(r"\*+", "", x_col).strip()
        title = f"{metric_label} by {x_label}"

    return {
        "type": chart_type,
        "title": title,
        "metric_label": metric_label,
        "unit": unit,
        "data": points[:60],
    }


def _chart_for_answer(
    answer_text: str,
    message: str,
    context: dict[str, Any],
    history: list[dict[str, Any]] | None = None,
    plan: dict[str, Any] | None = None,
) -> dict[str, Any] | None:
    """Unified chart resolver. Only produces a chart when the user explicitly requested one
    (chart intent detected in the question). Priority: plan-derived → answer-parsed → keyword."""
    q = _clean_text(message).casefold()
    if not _detect_chart_intent(q):
        return None
    try:
        if _is_tower_usage_distribution_question(q):
            distribution_chart = _tower_usage_distribution_chart(context)
            if distribution_chart:
                return distribution_chart
        answer_chart = _build_chart_from_answer(answer_text, q, history)
        if answer_chart:
            return answer_chart
        if plan:
            plan_chart = _build_chart_from_plan(plan, context, q, history)
            if plan_chart:
                return plan_chart
        return _build_chart_payload(message, context, history)
    except Exception:
        return None


def _build_chart_payload(
    message: str,
    context: dict[str, Any],
    history: list[dict[str, Any]] | None = None,
) -> dict[str, Any] | None:
    question = _clean_text(message).casefold()
    if not question:
        return None
    intent = _detect_chart_intent(question)
    if not intent:
        return None
    exact_dashboard = context.get("exact_dashboard") or {}
    spec = _chart_metric_spec_from_context(question, history or [])
    try:
        if intent == "line":
            return _build_line_chart(question, exact_dashboard, spec)
        if intent == "pie":
            return _build_pie_chart(question, exact_dashboard, context, spec)
        if intent == "bar":
            return _build_bar_chart(question, exact_dashboard, context, spec)
    except Exception:
        return None
    return None


def _try_deterministic_chat_answer(message: str, context: dict[str, Any]) -> str:
    question = _clean_text(message).casefold()
    if not question:
        return ""

    if _is_tower_usage_distribution_question(question):
        return ""

    if _is_delayed_pf_count_question(question):
        return _answer_delayed_pf_count_question(question, context)

    if _is_print_finish_lookup_question(question):
        return _answer_print_finish_lookup_question(question, context)

    if _is_night_type_time_comparison_question(question):
        return _answer_night_type_time_comparison_question(question, context)

    if _is_base_category_comparison_question(question):
        return _answer_base_category_comparison_question(question, context)

    if _is_metric_concentration_question(question):
        return _answer_metric_concentration_question(question, context)

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

    if _is_utilization_threshold_question(question):
        return _answer_utilization_threshold_question(question, context)

    return ""


_NUMERIC_CONDITION_PATTERNS = [
    # "than"/"then" are both accepted — "greater then X" is a common typo for "greater than X"
    # and the comparator must still be recognized, not silently dropped.
    (r"(?:greater than or equal to|greater then or equal to|at least|no less than|minimum of)\s*(\d+(?:\.\d+)?)", ">="),
    (r"(?:less than or equal to|less then or equal to|at most|no more than|maximum of)\s*(\d+(?:\.\d+)?)", "<="),
    (r"(?:greater than|greater then|more than|more then|over|above|exceed(?:ing|s)?|higher than|higher then)\s*(\d+(?:\.\d+)?)", ">"),
    (r"(?:less than|less then|under|below|fewer than|fewer then|lower than|lower then)\s*(\d+(?:\.\d+)?)", "<"),
    (r">=\s*(\d+(?:\.\d+)?)", ">="),
    (r"<=\s*(\d+(?:\.\d+)?)", "<="),
    (r">\s*(\d+(?:\.\d+)?)", ">"),
    (r"<\s*(\d+(?:\.\d+)?)", "<"),
]
_COMPARATOR_FUNCS = {
    ">": lambda value, threshold: value > threshold,
    "<": lambda value, threshold: value < threshold,
    ">=": lambda value, threshold: value >= threshold,
    "<=": lambda value, threshold: value <= threshold,
}
_COMPARATOR_LABELS = {">": "greater than", "<": "less than", ">=": "at least", "<=": "at most"}

# Maps question phrasing to the field name carried on delayed_pf / daily / folder rows, so a
# nested filter clause ("...where lost time is greater than 50 minutes") can be applied in Python
# instead of asking the LLM to apply a second condition on top of a count — the same failure
# class as the day-count dedup bug, just with an added filter the model silently dropped.
_FILTER_METRIC_FIELDS = [
    (["lost time", "lost time"], "loss_time_min", "Lost Time"),
    (["overrun", "minutes late", "minutes past", "past cutoff"], "overrun_minutes", "Overrun"),
    (["downtime", "down time"], "downtime_min", "Downtime"),
    (["runtime", "run time"], "runtime_min", "Run Time"),
    (["wait time", "waiting time"], "waiting_time_min", "Wait Time"),
    (["spare time", "spare"], "spare_time_min", "Spare Time"),
    (["unplanned"], "unplanned_time_min", "Unplanned Time"),
]


def _extract_numeric_condition(question: str) -> tuple[str, float] | None:
    for pattern, comparator in _NUMERIC_CONDITION_PATTERNS:
        match = re.search(pattern, question)
        if match:
            return comparator, float(match.group(1))
    return None


def _extract_filter_metric(question: str) -> tuple[str, str] | None:
    for terms, field, label in _FILTER_METRIC_FIELDS:
        if any(term in question for term in terms):
            return field, label
    return None


def _is_delayed_pf_count_question(question: str) -> bool:
    # delayed_pf has one row per delayed FOLDER per night, so a night with 2 delayed folders is 2
    # rows — asking an LLM to mentally dedupe run_date across dozens of such rows is exactly the
    # kind of counting task it gets wrong (observed: counted 4 unique days when only 3 existed).
    # Compute the distinct-day count in Python instead of letting the model count rows itself.
    has_count = any(term in question for term in ["count", "number of"]) or _asks_how_many(question)
    has_day_unit = any(term in question for term in ["day", "days", "night", "nights", "date", "dates"])
    return has_count and has_day_unit and _asks_delayed_pf(question)


def _answer_delayed_pf_count_question(question: str, context: dict[str, Any]) -> str:
    rows = context.get("delayed_pf") or (context.get("exact_dashboard") or {}).get("delayed_pf") or []
    filtered = _filter_context_rows(
        rows,
        question,
        ["run_date", "plant", "machine", "folder_name", "folder", "complexity_codes", "complexity_categories", "editions"],
    )
    rows = filtered or rows

    # Apply a nested numeric clause if the question has one, e.g. "...where lost time is greater
    # than 50 minutes" — without this, a compound question silently degraded to the unqualified
    # day count (the metric filter the user asked for was just dropped).
    filter_note = ""
    condition = _extract_numeric_condition(question)
    metric = _extract_filter_metric(question)
    if metric and not condition:
        # A metric named with no number at all ("...did we have downtime", "...with downtime")
        # means that metric was present/non-zero, not "any value including zero" — defaulting to
        # the unfiltered count here silently drops the condition the question actually asked for.
        condition = (">", 0.0)
    if condition and metric:
        comparator, threshold = condition
        field, label = metric
        compare_fn = _COMPARATOR_FUNCS[comparator]
        rows = [row for row in rows if compare_fn(_number(row.get(field)), threshold)]
        filter_note = f" where {label} is {_COMPARATOR_LABELS[comparator]} {threshold:g} minutes"

    unique_dates = sorted({_clean_text(row.get("run_date")) for row in rows if _clean_text(row.get("run_date"))})
    if not unique_dates:
        return f"**0** days had a delayed print finish{filter_note} in the current data."

    folder_row_count = len(rows)
    lines = [
        f"**{len(unique_dates)}** day(s) had at least one delayed print finish{filter_note} "
        f"(across {folder_row_count} folder/night row(s) in delayed_pf — a day with multiple delayed "
        f"folders is counted once).",
        "",
        "Dates: " + ", ".join(unique_dates),
    ]
    return "\n".join(lines)


def _is_print_finish_lookup_question(question: str) -> bool:
    has_print_finish = any(
        term in question
        for term in ["print finish", "printing finish", "finish time", "finished print", "finished printing"]
    )
    # Bare "print finish" means the actual finish time for every night, NOT the delayed subset —
    # if the question signals delay/lateness, or asks for a rate/comparison rather than a plain
    # per-day lookup, let the delayed_pf / comparison paths handle it instead.
    is_delay_or_comparison = any(
        term in question
        for term in [
            "delayed", "late finish", "overrun", "threshold", "cross", "compliance",
            "how often", "frequency", "compare", "comparison", " vs ", "versus", "rate",
        ]
    )
    return has_print_finish and not is_delay_or_comparison


def _answer_print_finish_lookup_question(question: str, context: dict[str, Any]) -> str:
    wants_folder = "folder" in question
    exact = context.get("exact_dashboard") or {}
    if wants_folder:
        rows = exact.get("folder_days_all") or exact.get("folder_days") or context.get("folder_days") or []
        rows = [row for row in rows if _clean_text(row.get("print_finish_time"))]
        if not rows:
            return ""
        rows = sorted(rows, key=lambda row: (row.get("run_date", ""), row.get("folder", "")))
        lines = [
            "| Date | Folder | Print Finish Time | Edition |",
            "| --- | --- | --- | --- |",
        ]
        for row in rows:
            lines.append(
                "| "
                + " | ".join([
                    _clean_text(row.get("run_date")),
                    _clean_text(row.get("folder")),
                    _clean_text(row.get("print_finish_time")),
                    _clean_text(row.get("last_edition")) or _clean_text(row.get("last_edition_name")) or "—",
                ])
                + " |"
            )
        return "\n".join(lines)

    rows = exact.get("daily") or context.get("daily") or []
    rows = [row for row in rows if _clean_text(row.get("print_finish_time"))]
    if not rows:
        return ""
    rows = sorted(rows, key=lambda row: row.get("run_date", ""))
    lines = [
        "| Date | Last Print Finish Time | Last Edition | Folder |",
        "| --- | --- | --- | --- |",
    ]
    for row in rows:
        lines.append(
            "| "
            + " | ".join([
                _clean_text(row.get("run_date")),
                _clean_text(row.get("print_finish_time")),
                _clean_text(row.get("last_edition")) or _clean_text(row.get("last_edition_name")) or "—",
                _clean_text(row.get("last_folder")) or "—",
            ])
            + " |"
        )
    return "\n".join(lines)


def _is_base_category_comparison_question(question: str) -> bool:
    has_category_pair = (
        ("snp" in question and "gnp" in question)
        or ("standard" in question and "glossy" in question)
        or ("non-uv" in question and "uv" in question)
        or ("non uv" in question and "uv" in question)
    )
    has_comparison = any(
        term in question
        for term in ["compare", "comparison", "versus", " vs ", "more", "less", "difference", "by how much", "carry"]
    )
    has_time_metric = any(
        term in question
        for term in [
            "loss", "lost", "changeover", "change over", "downtime", "down time",
            "runtime", "run time", "wait", "spare", "utilized", "utilised",
        ]
    )
    return has_category_pair and has_comparison and has_time_metric


def _is_night_type_time_comparison_question(question: str) -> bool:
    has_night_pair = (
        ("uv" in question and ("non-uv" in question or "non uv" in question))
        or ("gnp" in question and "snp" in question and "night" in question)
        or ("glossy" in question and "standard" in question and "night" in question)
    )
    has_comparison = any(
        term in question
        for term in [
            "compare", "comparison", "versus", " vs ", "more", "less",
            "difference", "by how much", "extra", "adds", "added",
        ]
    )
    has_time_metric = any(
        term in question
        for term in [
            "loss", "lost", "changeover", "change over", "downtime", "down time",
            "runtime", "run time", "wait", "spare", "utilized", "utilised",
        ]
    )
    return has_night_pair and has_comparison and has_time_metric


def _answer_night_type_time_comparison_question(question: str, context: dict[str, Any]) -> str:
    exact = context.get("exact_dashboard") or {}
    rows = exact.get("daily") or context.get("daily") or []
    if not rows:
        return ""

    metric_specs = _base_category_metric_specs(question)
    if not metric_specs:
        metric_specs = [("loss_time_min", "Lost Time")]

    buckets = {
        "uv": _new_night_type_bucket("UV / GNP night"),
        "non_uv": _new_night_type_bucket("Non-UV / SNP night"),
    }

    for row in rows:
        run_date = _clean_text(row.get("run_date"))
        if not run_date:
            continue
        is_uv = bool(row.get("uv_night") or row.get("gnp_night"))
        night_type = _clean_text(row.get("night_type")).casefold()
        if "snp" in night_type or "non-uv" in night_type or "non uv" in night_type:
            is_uv = False
        elif "gnp" in night_type or "uv" in night_type:
            is_uv = True

        bucket = buckets["uv" if is_uv else "non_uv"]
        bucket["night_count"] += 1
        bucket["dates"].add(run_date)
        for metric_key, _ in metric_specs:
            if metric_key == "utilized_time_min":
                value = (
                    _number(row.get("runtime_min"))
                    + _number(row.get("overrun_minutes_min"))
                    + _number(row.get("loss_time_min"))
                    + _number(row.get("waiting_time_min"))
                    + _number(row.get("downtime_min"))
                )
            else:
                value = _number(row.get(metric_key))
            bucket[metric_key] += value

    uv_count = buckets["uv"]["night_count"]
    non_uv_count = buckets["non_uv"]["night_count"]
    if uv_count <= 0 or non_uv_count <= 0:
        return "Not available in the current data. The selected window needs both UV/GNP nights and non-UV/SNP nights for this comparison."

    lines = [
        (
            f"**Verdict:** Compared on average per night, UV/GNP nights are measured against "
            f"non-UV/SNP nights across **{uv_count} UV** and **{non_uv_count} non-UV** nights."
        ),
        "",
        "| Metric | UV/GNP avg per night | Non-UV/SNP avg per night | Extra on UV night | UV/GNP total | Non-UV/SNP total |",
        "| --- | --- | --- | --- | --- | --- |",
    ]

    for metric_key, label in metric_specs:
        uv_total = _number(buckets["uv"].get(metric_key))
        non_uv_total = _number(buckets["non_uv"].get(metric_key))
        uv_avg = uv_total / uv_count if uv_count else 0.0
        non_uv_avg = non_uv_total / non_uv_count if non_uv_count else 0.0
        delta = uv_avg - non_uv_avg
        lines.append(
            "| "
            + " | ".join([
                label,
                _format_chat_minutes(uv_avg),
                _format_chat_minutes(non_uv_avg),
                _format_signed_chat_minutes(delta),
                _format_chat_minutes(uv_total),
                _format_chat_minutes(non_uv_total),
            ])
            + " |"
        )

    lines.extend([
        "",
        "Definition: a UV/GNP night is any date where at least one folder ran GNP or GNP Complex editions (C5-C15). If no C5-C15 edition ran, it is treated as a non-UV/SNP night.",
    ])
    return "\n".join(lines)


def _new_night_type_bucket(label: str) -> dict[str, Any]:
    return {
        "label": label,
        "dates": set(),
        "night_count": 0,
        "runtime_min": 0.0,
        "loss_time_min": 0.0,
        "change_over_time_min": 0.0,
        "downtime_min": 0.0,
        "waiting_time_min": 0.0,
        "spare_time_min": 0.0,
        "utilized_time_min": 0.0,
    }


def _answer_base_category_comparison_question(question: str, context: dict[str, Any]) -> str:
    exact = context.get("exact_dashboard") or {}
    rows = exact.get("folder_days_all") or exact.get("folder_days") or []
    if not rows:
        return ""

    metric_specs = _base_category_metric_specs(question)
    if not metric_specs:
        metric_specs = [("loss_time_min", "Lost Time")]

    buckets = {
        "SNP": _new_base_category_bucket("SNP / standard", "C1-C4, including SNP Complex"),
        "GNP": _new_base_category_bucket("GNP / glossy", "C5-C15, including GNP Complex"),
    }

    for row in rows:
        segments = row.get("runtime_segments") or []
        if not isinstance(segments, list) or not segments:
            continue

        segment_total = sum(_number(segment.get("minutes")) for segment in segments if isinstance(segment, dict))
        if segment_total <= 0:
            continue

        category_runtime = {"SNP": 0.0, "GNP": 0.0}
        for segment in segments:
            if not isinstance(segment, dict):
                continue
            category = _base_category_for_segment(segment)
            if category in category_runtime:
                category_runtime[category] += _number(segment.get("minutes"))

        run_date = _clean_text(row.get("run_date"))
        for category, runtime_minutes in category_runtime.items():
            if runtime_minutes <= 0:
                continue
            share = runtime_minutes / segment_total
            bucket = buckets[category]
            bucket["runtime_min"] += runtime_minutes
            bucket["folder_day_count"] += 1
            if run_date:
                bucket["dates"].add(run_date)
            for metric_key, _ in metric_specs:
                if metric_key == "runtime_min":
                    bucket[metric_key] += runtime_minutes
                elif metric_key == "utilized_time_min":
                    utilized = (
                        _number(row.get("runtime_min"))
                        + _number(row.get("overrun_minutes"))
                        + _number(row.get("loss_time_min"))
                        + _number(row.get("waiting_time_min"))
                        + _number(row.get("downtime_min"))
                    )
                    bucket[metric_key] += utilized * share
                else:
                    bucket[metric_key] += _number(row.get(metric_key)) * share

    if not buckets["SNP"]["folder_day_count"] and not buckets["GNP"]["folder_day_count"]:
        return "Not available in the current data."

    denominator_label = "folder-day" if any(term in question for term in ["per run", "per folder-day", "per folder day"]) else "night"
    lines = [
        "Using current dashboard filters and printing-window metrics.",
        "SNP/standard includes C1-C4; GNP/glossy includes C5-C15. Complex variants are included by default.",
        "If a folder-day contains both categories, time is allocated by runtime share. Wait, spare, and unplanned time are excluded from lost time.",
        "",
    ]

    headers = ["Category", "Nights", "Folder-days", "Runtime"]
    for _, label in metric_specs:
        if label != "Run Time":
            headers.extend([f"{label} total", f"{label}/{denominator_label}"])
    lines.append("| " + " | ".join(headers) + " |")
    lines.append("| " + " | ".join(["---"] * len(headers)) + " |")

    for key in ["SNP", "GNP"]:
        bucket = buckets[key]
        denominator = _base_category_denominator(bucket, denominator_label)
        values = [
            bucket["label"],
            str(len(bucket["dates"])),
            str(bucket["folder_day_count"]),
            _format_chat_minutes(bucket["runtime_min"]),
        ]
        for metric_key, label in metric_specs:
            if label == "Run Time":
                continue
            total_value = bucket.get(metric_key, 0.0)
            avg_value = total_value / denominator if denominator > 0 else 0.0
            values.extend([_format_chat_minutes(total_value), _format_chat_minutes(avg_value)])
        lines.append("| " + " | ".join(values) + " |")

    comparison_metric = _primary_base_category_metric(metric_specs)
    if comparison_metric:
        metric_key, label = comparison_metric
        snp_avg = _base_category_average(buckets["SNP"], metric_key, denominator_label)
        gnp_avg = _base_category_average(buckets["GNP"], metric_key, denominator_label)
        delta = gnp_avg - snp_avg
        direction = "more" if delta > 0 else "less" if delta < 0 else "the same"
        if direction == "the same":
            lines.append(f"\nConclusion: GNP/glossy and SNP/standard have the same {label.lower()} per {denominator_label}.")
        else:
            pct = _percentage(abs(delta), snp_avg) if snp_avg > 0 else 0
            lines.append(
                f"\nConclusion: GNP/glossy carries {_format_chat_minutes(abs(delta))} {direction} "
                f"{label.lower()} per {denominator_label} than SNP/standard"
                f"{f' ({pct}% difference)' if pct else ''}."
            )

    if len(metric_specs) > 1:
        extra_lines = []
        for metric_key, label in metric_specs:
            if comparison_metric and metric_key == comparison_metric[0]:
                continue
            snp_avg = _base_category_average(buckets["SNP"], metric_key, denominator_label)
            gnp_avg = _base_category_average(buckets["GNP"], metric_key, denominator_label)
            delta = gnp_avg - snp_avg
            if abs(delta) > 0:
                direction = "more" if delta > 0 else "less"
                extra_lines.append(
                    f"{label}: GNP/glossy is {_format_chat_minutes(abs(delta))} {direction} per {denominator_label}."
                )
        if extra_lines:
            lines.append("Additional comparison: " + " ".join(extra_lines))

    return "\n".join(lines)


def _base_category_metric_specs(question: str) -> list[tuple[str, str]]:
    specs: list[tuple[str, str]] = []
    has_loss = "loss" in question or "lost" in question
    has_changeover = "changeover" in question or "change over" in question
    if has_loss or has_changeover:
        specs.append(("loss_time_min", "Lost Time"))
    if has_changeover:
        specs.append(("change_over_time_min", "Changeover Time"))
    if "downtime" in question or "down time" in question:
        specs.append(("downtime_min", "Downtime"))
    if "runtime" in question or "run time" in question:
        specs.append(("runtime_min", "Run Time"))
    if "wait" in question:
        specs.append(("waiting_time_min", "Wait Time"))
    if "spare" in question:
        specs.append(("spare_time_min", "Spare Time"))
    if "utilized" in question or "utilised" in question:
        specs.append(("utilized_time_min", "Utilized Time"))

    deduped: list[tuple[str, str]] = []
    for spec in specs:
        if spec not in deduped:
            deduped.append(spec)
    return deduped


def _new_base_category_bucket(label: str, included_codes: str) -> dict[str, Any]:
    return {
        "label": label,
        "included_codes": included_codes,
        "dates": set(),
        "folder_day_count": 0,
        "runtime_min": 0.0,
        "loss_time_min": 0.0,
        "change_over_time_min": 0.0,
        "downtime_min": 0.0,
        "waiting_time_min": 0.0,
        "spare_time_min": 0.0,
        "utilized_time_min": 0.0,
    }


def _base_category_for_segment(segment: dict[str, Any]) -> str:
    code_number = re_fullmatch_complexity(segment.get("complexity_code"))
    if code_number:
        number = int(code_number)
        if 1 <= number <= 4:
            return "SNP"
        if 5 <= number <= 15:
            return "GNP"

    text = " ".join([
        _clean_text(segment.get("type")),
        _clean_text(segment.get("category")),
        _clean_text(segment.get("label")),
    ]).casefold()
    if "gnp" in text or "glossy" in text or "uv" in text:
        return "GNP"
    if "snp" in text or "standard" in text:
        return "SNP"
    return ""


def _base_category_denominator(bucket: dict[str, Any], denominator_label: str) -> int:
    if denominator_label == "folder-day":
        return int(bucket.get("folder_day_count") or 0)
    return len(bucket.get("dates") or [])


def _base_category_average(bucket: dict[str, Any], metric_key: str, denominator_label: str) -> float:
    denominator = _base_category_denominator(bucket, denominator_label)
    if denominator <= 0:
        return 0.0
    return _number(bucket.get(metric_key)) / denominator


def _primary_base_category_metric(metric_specs: list[tuple[str, str]]) -> tuple[str, str] | None:
    for metric_key, label in metric_specs:
        if metric_key == "loss_time_min":
            return metric_key, label
    return metric_specs[0] if metric_specs else None


def _is_metric_concentration_question(question: str) -> bool:
    has_distribution = any(
        term in question
        for term in ["spread", "concentrated", "concentration", "clustered", "cluster", "localized", "localised"]
    )
    return has_distribution and _concentration_metric_spec(question) is not None


def _answer_metric_concentration_question(question: str, context: dict[str, Any]) -> str:
    spec = _concentration_metric_spec(question)
    if not spec:
        return ""

    metric_key, label, definition_note = spec
    exact = context.get("exact_dashboard") or {}
    folder_rows = exact.get("folders") or context.get("folders") or []
    rows = []
    for row in folder_rows:
        resource = _clean_text(row.get("resource") or row.get("folder"))
        value = _number(row.get(metric_key))
        if resource and value > 0:
            rows.append({"resource": resource, "value": value})

    if not rows:
        return f"No {label.lower()} is present in the current dashboard context."

    rows.sort(key=lambda row: (-row["value"], row["resource"]))
    total = sum(row["value"] for row in rows)
    top = rows[0]
    top3 = rows[:3]
    top_share = _percentage(top["value"], total)
    top3_share = _percentage(sum(row["value"] for row in top3), total)
    active_folder_count = len(rows)
    classification = _concentration_label(top_share, top3_share, active_folder_count)
    rule_hit = _concentration_rule_hit(top_share, top3_share, active_folder_count)

    lines = [
        "Verdict",
        "",
        "| Metric | Pattern | Total | Folders with value | Top folder | Top folder share | Top 3 share | Rule hit |",
        "| --- | --- | --- | --- | --- | --- | --- | --- |",
        (
            f"| {label} | {classification} | {_format_chat_minutes(total)} | {active_folder_count} | "
            f"{top['resource']} | {top_share}% | {top3_share}% | {rule_hit} |"
        ),
        "",
        "Top contributors",
        "",
        "| Rank | Folder | Minutes | Share | Cumulative share |",
        "| --- | --- | --- | --- | --- |",
    ]
    cumulative = 0.0
    for index, row in enumerate(rows[:5], start=1):
        cumulative += row["value"]
        lines.append(
            f"| {index} | {row['resource']} | {_format_chat_minutes(row['value'])} | "
            f"{_percentage(row['value'], total)}% | {_percentage(cumulative, total)}% |"
        )
    lines.extend([
        "",
        "Definition",
        "",
        "| Term | Applied meaning |",
        "| --- | --- |",
        f"| {label} | {definition_note} |",
    ])
    return "\n".join(lines)


def _concentration_metric_spec(question: str) -> tuple[str, str, str] | None:
    if any(term in question for term in ["unscheduled", "un-scheduled", "unplanned", "un-planned", "idle capacity"]):
        return (
            "unplanned_time_min",
            "Unplanned Time",
            "Unscheduled time is treated as Unplanned Time, not Lost Time.",
        )
    if "downtime" in question or "down time" in question:
        return ("downtime_min", "Downtime", "Downtime is unplanned stoppage during active production.")
    if "wait" in question:
        return ("waiting_time_min", "Wait Time", "Wait Time is separate from Lost Time.")
    if "spare" in question:
        return ("spare_time_min", "Spare Time", "Spare Time is remaining available capacity.")
    if "runtime" in question or "run time" in question:
        return ("runtime_min", "Run Time", "Run Time is productive print time.")
    if "utilized" in question or "utilised" in question:
        return (
            "runtime_min",
            "Run Time",
            "For concentration by folder, runtime is used as the primary utilization driver.",
        )
    if "loss" in question or "lost" in question:
        return (
            "loss_time_min",
            "Lost Time",
            "Lost Time is changeover + LPR-to-start + reflong; it excludes Wait Time and Unplanned Time.",
        )
    return None


def _concentration_label(top_share: float, top3_share: float, active_folder_count: int) -> str:
    if active_folder_count <= 1:
        return "fully concentrated"
    if top_share >= 50 or top3_share >= 80:
        return "highly concentrated"
    if top_share >= 35 or top3_share >= 65:
        return "moderately concentrated"
    return "spread across folders"


def _concentration_rule_hit(top_share: float, top3_share: float, active_folder_count: int) -> str:
    if active_folder_count <= 1:
        return "Only one folder has non-zero minutes"
    if top_share >= 50:
        return "Top folder >= 50%"
    if top3_share >= 80:
        return "Top 3 >= 80%"
    if top_share >= 35:
        return "Top folder >= 35%"
    if top3_share >= 65:
        return "Top 3 >= 65%"
    return "No concentration threshold crossed"


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


def _answer_gnp_snp_folder_question(question: str, context: dict[str, Any]) -> str:
    analysis = context.get("gnp_snp_folder_analysis") or {}
    exact = context.get("exact_dashboard") or {}

    if _asks_folder_wise_average_spare(question):
        rows = exact.get("folders") or []
        answer_rows = []
        for row in rows:
            active_nights = int(_number(row.get("active_nights")))
            if active_nights <= 0:
                continue
            answer_rows.append({
                "folder": row.get("resource"),
                "active_nights": active_nights,
                "avg_spare_time_min": _clean_number(_number(row.get("spare_time_min")) / active_nights),
            })
        answer_rows.sort(key=lambda row: (-_number(row.get("avg_spare_time_min")), row.get("folder", "")))
        return _markdown_table(
            ["Folder", "Active nights", "Avg spare time (min)"],
            [
                [row["folder"], row["active_nights"], row["avg_spare_time_min"]]
                for row in answer_rows
            ],
        )

    if "finish" in question and ("4 am" in question or "04:00" in question or "beyond" in question or "delay" in question):
        rows = analysis.get("delayed_finish_complexity") or []
        if not rows:
            return "No print-finish rows beyond the cutoff were found in the selected period."
        return _markdown_table(
            ["Date", "Folder", "Finish", "Overrun", "Product mix", "Complexity", "Largest components"],
            [
                [
                    row.get("run_date"),
                    row.get("folder"),
                    row.get("print_finish_time") or row.get("estimated_print_finish_time"),
                    row.get("overrun_minutes"),
                    row.get("product_mix"),
                    ", ".join(row.get("complexity_codes") or []),
                    ", ".join(f"{c.get('label')}: {c.get('minutes')} min" for c in (row.get("largest_components") or [])[:3]),
                ]
                for row in rows[:80]
            ],
        )

    if not any(term in question for term in ["gnp", "snp", "uv", "glossy", "standard", "web break"]):
        return ""

    if "web break" in question:
        rows = analysis.get("web_break_gnp_snp_tower_comparison") or []
        if not rows:
            return "No matching web-break attribution rows were found for the named towers in the selected plant/period."
        return (
            "Web-break events are not stored with product type, so they cannot be split directly into GNP vs SNP events. "
            "The table below compares attributed web-break events with each tower's GNP/SNP runtime mix.\n\n"
            + _markdown_table(
                ["Tower", "Events", "Minutes", "GNP runtime", "SNP runtime", "Can split by product?"],
                [
                    [
                        row.get("tower"),
                        row.get("attributed_event_count"),
                        row.get("attributed_minutes"),
                        row.get("gnp_runtime_min"),
                        row.get("snp_runtime_min"),
                        "Yes" if row.get("can_split_web_break_by_product_type") else "No",
                    ]
                    for row in rows
                ],
            )
        )

    if "minimum" in question and ("three" in question or "3" in question) and "gnp" in question:
        rows = analysis.get("nights_with_min_3_gnp_folders") or []
        if not rows:
            return "No nights had at least three folders running GNP editions in the selected period."
        avg_spare = _clean_number(_average([_number(row.get("avg_spare_time_min")) for row in rows]))
        if "correlation" in question or "finish" in question or "4 am" in question or "04:00" in question:
            delayed_rows = analysis.get("delayed_finish_complexity") or []
            min3_dates = {_clean_text(row.get("run_date")) for row in rows}
            delayed_on_min3 = [row for row in delayed_rows if _clean_text(row.get("run_date")) in min3_dates]
            return (
                f"**{len(rows)}** nights had at least three GNP folders. "
                f"Average spare time across those GNP folder groups was **{avg_spare} min**. "
                f"Delayed print finish occurred on **{len(_sorted_unique(row.get('run_date') for row in delayed_on_min3))}** of those nights.\n\n"
                + _markdown_table(
                    ["Date", "GNP folders", "Avg spare (min)", "Folders"],
                    [[row.get("run_date"), row.get("gnp_folder_count"), row.get("avg_spare_time_min"), ", ".join(row.get("folders") or [])] for row in rows[:30]],
                )
            )
        return (
            f"Across **{len(rows)}** nights with at least three GNP folders, the average spare time was **{avg_spare} min**.\n\n"
            + _markdown_table(
                ["Date", "GNP folders", "Avg spare (min)", "Total spare (min)"],
                [[row.get("run_date"), row.get("gnp_folder_count"), row.get("avg_spare_time_min"), row.get("total_spare_time_min")] for row in rows],
            )
        )

    if "break" in question and "loss" in question:
        rows = analysis.get("gnp_loss_breakdown_by_folder") or []
        if not rows:
            return "No GNP folder loss rows were found in the selected period."
        return _markdown_table(
            ["Folder", "GNP folder-days", "Total loss", "Avg loss", "Changeover", "LPR-to-start", "Reflong"],
            [
                [
                    row.get("folder"),
                    row.get("gnp_folder_day_count"),
                    row.get("total_loss_time_min"),
                    row.get("avg_loss_time_min"),
                    row.get("change_over_time_min"),
                    row.get("lpr_to_start_min"),
                    row.get("reflong_time_min"),
                ]
                for row in rows
            ],
        )

    comparison = analysis.get("comparison_by_product_type") or []
    if comparison and (
        "compare" in question
        or "average" in question
        or "correlation" in question
        or "waiting time" in question
        or "lost time" in question
        or "downtime" in question
        or "reflong" in question
        or "lpr" in question
    ):
        selected_fields = ["folder_day_count"]
        if "spare" in question:
            selected_fields.append("avg_spare_time_min")
        if "loss" in question:
            selected_fields.append("avg_loss_time_min")
        if "wait" in question:
            selected_fields.append("avg_waiting_time_min")
        if "lpr" in question or "print start" in question:
            selected_fields.append("avg_lpr_to_start_min")
        if "reflong" in question:
            selected_fields.append("avg_reflong_time_min")
        if "downtime" in question:
            selected_fields.append("avg_downtime_min")
        if "correlation" in question or "finish" in question or "4 am" in question or "04:00" in question:
            selected_fields.extend(["delayed_folder_day_pct", "avg_overrun_min"])
        if len(selected_fields) == 1:
            selected_fields.extend(["avg_loss_time_min", "avg_waiting_time_min", "avg_downtime_min"])
        headers = ["Product type"] + [_humanize_field(field) for field in selected_fields]
        return _markdown_table(
            headers,
            [
                [row.get("product_type"), *[_format_plan_metric_value(field, row.get(field)) for field in selected_fields]]
                for row in comparison
            ],
        )

    return ""


def _asks_folder_wise_average_spare(question: str) -> bool:
    return (
        "spare" in question
        and "average" in question
        and ("folder wise" in question or "folder-wise" in question or "per folder" in question or "folder" in question)
        and "gnp" not in question
        and "snp" not in question
    )


def _markdown_table(headers: list[str], rows: list[list[Any]]) -> str:
    if not rows:
        return "No matching rows found."
    def cell(value: Any) -> str:
        if isinstance(value, float):
            value = _clean_number(value)
        text = _clean_text(value)
        return text.replace("|", "/")
    header_line = "| " + " | ".join(cell(h) for h in headers) + " |"
    sep_line = "| " + " | ".join("---" for _ in headers) + " |"
    row_lines = ["| " + " | ".join(cell(v) for v in row) + " |" for row in rows]
    return "\n".join([header_line, sep_line, *row_lines])


def _fallback_answer_from_context(
    message: str,
    context: dict[str, Any],
    plan: dict[str, Any] | None = None,
) -> str:
    question = _clean_text(message).casefold()
    if not question:
        return ""

    planned = _execute_qu_plan(plan, message, context) or _answer_from_plan(plan, message, context)
    if planned:
        return planned

    if _is_tower_usage_distribution_question(question):
        return _answer_tower_usage_distribution_question(context)

    if _asks_complexity_downtime(question):
        return _answer_complexity_downtime_question(context)

    gnp_snp_answer = _answer_gnp_snp_folder_question(question, context)
    if gnp_snp_answer:
        return gnp_snp_answer

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


def _answer_from_plan(plan: dict[str, Any] | None, message: str, context: dict[str, Any]) -> str:
    if not isinstance(plan, dict) or not plan:
        return ""

    source_key = _clean_text(plan.get("primary_source"))
    rows = _rows_for_plan_source(source_key, context)
    if not rows:
        return ""

    filtered_rows = _apply_plan_filters(rows, plan.get("filters") or {})
    question = _clean_text(message).casefold()
    if filtered_rows is None:
        # A numeric filter named a field that doesn't exist on this source (e.g. the planner picked
        # a table without the metric it meant to filter on) — reporting an unfiltered count here
        # would look like an answer but silently ignore the condition. Defer instead of guessing.
        return ""
    rows = filtered_rows
    if not rows:
        if _plan_wants_components_breakdown(question):
            return f"**0** matching rows from {source_key} for the given filter — no time-component breakdown to show."
        return "No rows match the requested filters in the current dashboard context."

    average_filter = _average_comparison_filter(question, plan, rows, message)
    if average_filter:
        rows = _apply_average_comparison_filter(rows, average_filter)
        if not rows:
            return "No rows match the requested average comparison in the current dashboard context."
        if _question_asks_for_date_list(question, rows):
            date_field = _resolve_row_field(rows, "run_date") or _resolve_row_field(rows, "date")
            dates = _sorted_unique(row.get(date_field) for row in rows) if date_field else []
            if not dates:
                return "No matching dates found in the current dashboard context."
            title = _average_comparison_title(average_filter)
            return title + "\n\n" + "\n".join(f"- {date}" for date in dates)

    # A nested "count/list X where <condition> ... and what are the key components" question needs
    # both a row count AND a breakdown handed back together — the generic count/average/grouped
    # branches below each return one or the other, so this combo gets its own early path instead of
    # falling through to a single-number answer that silently drops the "components" half.
    if _plan_wants_components_breakdown(question):
        count_unit_field = _plan_count_unit_field(question, rows)
        if count_unit_field:
            distinct_values = sorted({
                _clean_text(row.get(count_unit_field)) for row in rows if _clean_text(row.get(count_unit_field))
            })
            headline = (
                f"**{len(distinct_values)}** distinct {_humanize_field(count_unit_field).lower()}(s) "
                f"matched from {source_key}."
            )
        else:
            headline = f"**{len(rows)}** matching row(s) from {source_key}."
        breakdown = _plan_components_breakdown(question, rows, context)
        return headline + breakdown if breakdown else headline

    metric_fields = _plan_metric_fields(plan, rows, message)
    group_field = _plan_group_field(plan, rows, message)
    intent = _clean_text(plan.get("intent")).casefold()
    output_format = _clean_text(plan.get("output_format")).casefold()
    computation = _clean_text(plan.get("computation")).casefold()
    if intent in {"trend", "prediction"}:
        return ""
    # A "comparison" intent asking for a rate/frequency/percentage needs a denominator that
    # usually isn't in primary_source's matched rows alone (e.g. "how often" = matched-row count
    # ÷ total count per group, where the total often lives in a different table) — the count-only
    # shortcut below can only return the numerator, which looks like an answer but silently omits
    # the actual comparison. Defer to the LLM (which gets the full AGENT PLAN + context) instead.
    wants_rate = any(
        term in computation for term in ["percentage", "frequency", "ratio", "rate", "%", "how often"]
    ) or any(term in question for term in ["percentage", "frequency", "ratio", "how often"])
    if intent == "comparison" and wants_rate:
        return ""
    wants_average = (
        intent == "average"
        or "average" in computation
        or "average" in question
        or re.search(r"\bavg\b", computation)
        or re.search(r"\bavg\b", question)
        or ("divide" in computation and any(term in computation for term in ["number of", "count of", "row", "code"]))
    )
    wants_count = intent == "count" or (not metric_fields and any(term in computation for term in ["count", "how many"]))
    wants_ranking = intent == "ranking" or output_format == "ranked_list"
    wants_comparison = intent == "comparison" or output_format == "comparison" or group_field

    if wants_count:
        if group_field:
            counts: dict[str, int] = {}
            for row in rows:
                key = _plan_group_value(row, group_field)
                counts[key] = counts.get(key, 0) + 1
            ranked = sorted(counts.items(), key=lambda item: (-item[1], item[0]))
            return _format_plan_count_answer(source_key, group_field, ranked)
        # If the source has multiple rows per day/night (e.g. one row per delayed folder per
        # night), "how many days/nights" must dedupe by date — counting raw rows over-counts any
        # day with more than one matching row.
        count_unit_field = _plan_count_unit_field(question, rows)
        if count_unit_field:
            distinct_values = {
                _clean_text(row.get(count_unit_field)) for row in rows if _clean_text(row.get(count_unit_field))
            }
            return (
                f"**{len(distinct_values)}** distinct {_humanize_field(count_unit_field).lower()}(s) "
                f"from {source_key} (across {len(rows)} matching row(s))."
            )
        return f"Count from {source_key}: {len(rows)}"

    if not metric_fields:
        return ""

    if group_field or wants_comparison or wants_ranking:
        group_field = group_field or _default_group_field(rows)
        if not group_field:
            return ""
        grouped = _aggregate_plan_rows(rows, metric_fields, group_field, wants_average)
        if not grouped:
            return ""
        # A single group is never a real comparison/breakdown — it's just a totals row with a
        # confusing category header. Let the full LLM call produce a better explanation instead
        # (e.g. "what is the percentage split of capacity?" → pie-chart style prose, not a 1-row
        # "by plant" table).
        if len(grouped) <= 1:
            return ""
        if wants_ranking and metric_fields:
            metric = metric_fields[0]
            grouped = sorted(grouped, key=lambda row: -_number(row.get(metric)))
        return _format_plan_grouped_answer(source_key, metric_fields, group_field, grouped, wants_average)

    totals = {
        metric: _clean_number(sum(_number(row.get(metric)) for row in rows))
        for metric in metric_fields
    }
    if wants_average:
        totals = {
            metric: _clean_number(_number(value) / len(rows)) if rows else 0
            for metric, value in totals.items()
    }
    label = "Average" if wants_average else "Total"
    parts = [
        f"{_humanize_field(metric)}: {_format_plan_metric_value(metric, value)}"
        for metric, value in totals.items()
    ]
    return f"{label} from {source_key}: " + " | ".join(parts)


def _rows_for_plan_source(source_key: str, context: dict[str, Any]) -> list[dict[str, Any]]:
    key = source_key.casefold()
    exact = context.get("exact_dashboard") or {}
    source_map: dict[str, Any] = {
        "exact_dashboard.folders": exact.get("folders"),
        "exact_dashboard.folder_days": exact.get("folder_days_all") or exact.get("folder_days"),
        "exact_dashboard.daily": exact.get("daily"),
        "folders": context.get("folders"),
        "towers": context.get("towers"),
        "tower_runtime_mix": context.get("tower_runtime_mix"),
        "tower_runtime_segments": _tower_runtime_segment_context_rows(
            context.get("tower_days_all") or context.get("tower_days") or [],
            "",
            limit=None,
        ),
        "tower_days": context.get("tower_days_all") or context.get("tower_days"),
        "tower_usage_distribution": context.get("tower_usage_distribution"),
        "tower_weekday_summary": context.get("tower_weekday_summary"),
        "tower_month_summary": context.get("tower_month_summary"),
        "daily_efficiency": context.get("daily_efficiency"),
        "tower_downtime_runs": context.get("tower_downtime_runs_all") or context.get("tower_downtime_runs"),
        "downtime_by_folder": context.get("downtime_by_folder"),
        "delayed_pf": context.get("delayed_pf"),
        "editions_by_date": context.get("editions_by_date"),
        "editions_by_folder": context.get("editions_by_folder"),
        "editions_by_tower": context.get("editions_by_tower"),
        "book_details": context.get("book_details"),
        "complexity_by_code": context.get("complexity_by_code"),
        "complexity_downtime_by_code": context.get("complexity_downtime_by_code"),
        "complexity_vs_loss": context.get("complexity_vs_loss"),
        "tower_downtime_reason_attribution.by_tower": (context.get("tower_downtime_reason_attribution") or {}).get("by_tower"),
        "tower_downtime_reason_attribution.by_tower_reason": (context.get("tower_downtime_reason_attribution") or {}).get("by_tower_reason"),
        "gnp_snp_folder_analysis": (context.get("gnp_snp_folder_analysis") or {}).get("comparison_by_product_type"),
        "gnp_snp_folder_analysis.comparison_by_product_type": (context.get("gnp_snp_folder_analysis") or {}).get("comparison_by_product_type"),
        "gnp_snp_folder_analysis.gnp_loss_breakdown_by_folder": (context.get("gnp_snp_folder_analysis") or {}).get("gnp_loss_breakdown_by_folder"),
        "gnp_snp_folder_analysis.nights_with_min_3_gnp_folders": (context.get("gnp_snp_folder_analysis") or {}).get("nights_with_min_3_gnp_folders"),
        "gnp_snp_folder_analysis.delayed_finish_complexity": (context.get("gnp_snp_folder_analysis") or {}).get("delayed_finish_complexity"),
        "gnp_snp_folder_analysis.web_break_gnp_snp_tower_comparison": (context.get("gnp_snp_folder_analysis") or {}).get("web_break_gnp_snp_tower_comparison"),
        "loss_time.all_days": (context.get("loss_time") or {}).get("all_days"),
    }
    downtime_by_reason = context.get("downtime_by_reason") or {}
    source_map["downtime_by_reason"] = downtime_by_reason.get("top_reasons") or downtime_by_reason.get("by_machine_folder")
    rows = source_map.get(key)
    if isinstance(rows, list):
        return [row for row in rows if isinstance(row, dict)]
    if isinstance(rows, dict):
        return [rows]
    return []


def _apply_plan_filters(rows: list[dict[str, Any]], filters: Any) -> list[dict[str, Any]] | None:
    """Returns None (not []) when a numeric comparator filter names a field the chosen source
    doesn't actually have — silently dropping that filter would make every row "pass" and report
    a confidently wrong, over-broad count/list instead of falling through to the next answer path.
    Equality/contains filters keep the old skip-if-unresolved behavior other questions rely on."""
    if not isinstance(filters, dict) or not filters:
        return rows
    selected = rows
    for field, expected in filters.items():
        if expected in (None, "", [], {}):
            continue
        is_numeric_filter = isinstance(expected, dict) and "op" in expected and "value" in expected
        field_name = _resolve_row_field(selected, _clean_text(field))
        if not field_name:
            if is_numeric_filter:
                return None
            continue
        # Numeric comparator filter, e.g. {"op": ">", "value": 50} for "lost time greater than 50
        # minutes" — the planner schema teaches the model to emit this shape for threshold
        # questions instead of the plain equality match below, which can't express ">"/"<" at all.
        if is_numeric_filter:
            op = _clean_text(expected.get("op"))
            if op not in _COMPARATOR_FUNCS:
                return None
            selected = [
                row
                for row in selected
                if _compare_condition_values(
                    field_name,
                    row.get(field_name),
                    expected.get("value"),
                    op,
                )
            ]
            continue
        expected_values = expected if isinstance(expected, list) else [expected]
        expected_texts = [_clean_text(value).casefold() for value in expected_values if _clean_text(value)]
        if not expected_texts:
            continue
        selected = [
            row for row in selected
            if any(expected_text in _row_value_text(row.get(field_name)).casefold() for expected_text in expected_texts)
        ]
    return selected


def _average_comparison_filter(
    question: str,
    plan: dict[str, Any],
    rows: list[dict[str, Any]],
    message: str,
) -> dict[str, Any] | None:
    if not rows or not _mentions_average_comparison(question):
        return None

    comparator = _average_comparison_operator(question)
    if not comparator:
        return None

    metric_fields = _mentioned_metric_fields(plan, rows, message)
    if not metric_fields:
        return None

    thresholds: dict[str, float] = {}
    for field in metric_fields:
        values = [_number(row.get(field)) for row in rows if row.get(field) is not None]
        if values:
            thresholds[field] = _average(values)

    if not thresholds:
        return None

    joiner = "any" if re.search(r"\b(?:any|or|either)\b", question) else "all"
    return {"operator": comparator, "thresholds": thresholds, "joiner": joiner}


def _mentions_average_comparison(question: str) -> bool:
    return bool(
        re.search(r"\b(?:below|under|less than|lower than|above|over|more than|greater than|higher than)\b", question)
        and re.search(r"\b(?:average|avg|mean)\b", question)
    )


def _average_comparison_operator(question: str) -> str:
    if re.search(r"\b(?:below|under|less than|lower than)\b", question):
        return "<"
    if re.search(r"\b(?:above|over|more than|greater than|higher than)\b", question):
        return ">"
    return ""


def _mentioned_metric_fields(
    plan: dict[str, Any],
    rows: list[dict[str, Any]],
    message: str,
) -> list[str]:
    fields: list[str] = []
    for field in _plan_metric_fields(plan, rows, message):
        if field not in fields:
            fields.append(field)

    question = _clean_text(message).casefold()
    metric_aliases = [
        ("runtime_min", ["runtime", "run time", "run"]),
        ("runtime_minutes", ["runtime", "run time", "run"]),
        ("downtime_min", ["downtime", "down time"]),
        ("allocated_downtime_min", ["downtime", "down time"]),
        ("loss_time_min", ["lost time", "lost time", "loss"]),
        ("lost_time_min", ["lost time", "lost time", "loss"]),
        ("waiting_time_min", ["wait time", "waiting time", "wait"]),
        ("spare_time_min", ["spare time", "spare"]),
        ("unplanned_time_min", ["unplanned time", "unplanned"]),
        ("utilization_pct", ["utilization", "utilisation"]),
    ]
    for wanted_field, aliases in metric_aliases:
        if not any(alias in question for alias in aliases):
            continue
        resolved = _resolve_row_field(rows, wanted_field)
        if resolved and _field_has_any_numeric_values(rows, resolved) and resolved not in fields:
            fields.append(resolved)

    return fields


def _field_has_any_numeric_values(rows: list[dict[str, Any]], field: str) -> bool:
    return any(row.get(field) is not None and isfinite(_number(row.get(field))) for row in rows)


def _apply_average_comparison_filter(rows: list[dict[str, Any]], average_filter: dict[str, Any]) -> list[dict[str, Any]]:
    compare_fn = _COMPARATOR_FUNCS.get(_clean_text(average_filter.get("operator")))
    thresholds = average_filter.get("thresholds") or {}
    if not compare_fn or not thresholds:
        return rows

    require_all = average_filter.get("joiner") != "any"
    selected = []
    for row in rows:
        comparisons = [
            compare_fn(_number(row.get(field)), threshold)
            for field, threshold in thresholds.items()
        ]
        if comparisons and (all(comparisons) if require_all else any(comparisons)):
            selected.append(row)
    return selected


def _question_asks_for_date_list(question: str, rows: list[dict[str, Any]]) -> bool:
    if not (_resolve_row_field(rows, "run_date") or _resolve_row_field(rows, "date")):
        return False
    return bool(
        re.search(r"\b(?:identify|list|show|which|what)\b", question)
        and re.search(r"\b(?:date|dates|day|days|night|nights)\b", question)
    )


def _average_comparison_title(average_filter: dict[str, Any]) -> str:
    comparator = _COMPARATOR_LABELS.get(_clean_text(average_filter.get("operator")), "compared with")
    metric_labels = [_humanize_field(field) for field in (average_filter.get("thresholds") or {}).keys()]
    if len(metric_labels) == 1:
        metric_text = metric_labels[0]
    else:
        metric_text = ", ".join(metric_labels[:-1]) + f" and {metric_labels[-1]}"
    return f"Dates where {metric_text} were {comparator} their average:"


def _plan_wants_components_breakdown(question: str) -> bool:
    return any(term in question for term in ["component", "components", "breakdown", "break down"])


_PLAN_COUNT_UNIT_FIELDS = [
    (["day", "days", "night", "nights", "date", "dates"], "run_date"),
    (["folder", "folders"], "folder"),
    (["tower", "towers"], "tower"),
    (["edition", "editions"], "editions"),
    (["plant", "plants"], "plant"),
    (["reason", "reasons"], "reason"),
]


def _plan_count_unit_field(question: str, rows: list[dict[str, Any]]) -> str:
    # Source tables here are often one row per (folder, night) or (tower, night) etc. — "how many
    # X" must dedupe on whatever unit X names, not just dates, or a unit with several matching
    # rows (e.g. a folder with multiple delayed nights) gets over-counted.
    # "times" means occasions ("what times have we worked with...") — matched as a whole word so
    # it doesn't fire on "lost time"/"run time"/etc.
    if re.search(r"\btimes\b", question):
        resolved = _resolve_row_field(rows, "run_date")
        if resolved:
            return resolved
    for terms, field in _PLAN_COUNT_UNIT_FIELDS:
        if any(term in question for term in terms):
            resolved = _resolve_row_field(rows, field)
            if resolved:
                return resolved
    return ""


def _date_count_unit(question: str) -> str:
    if re.search(r"\b(?:night|nights)\b", question):
        return "night" if re.search(r"\bnight\b", question) else "nights"
    if re.search(r"\b(?:date|dates)\b", question) and not re.search(r"\b(?:day|days)\b", question):
        return "date" if re.search(r"\bdate\b", question) else "dates"
    return "day" if re.search(r"\bday\b", question) else "days"


def _date_count_evidence_fields(
    plan: dict[str, Any],
    rows: list[dict[str, Any]],
) -> list[tuple[str, str]]:
    fields: list[tuple[str, str]] = []

    def add(candidate: str, label: str) -> None:
        resolved = _resolve_row_field(rows, candidate)
        if resolved and all(existing != resolved for existing, _ in fields):
            fields.append((resolved, label))

    source = _clean_text(plan.get("primary_source")).casefold()
    entity_types = {
        _clean_text(entity.get("type")).casefold()
        for entity in (plan.get("entities") or [])
        if isinstance(entity, dict)
    }
    if "plant" in entity_types:
        add("plant", "Plant")
        add("plant_name", "Plant")
    if "machine" in entity_types or source in {
        "exact_dashboard.folder_days",
        "tower_days",
    }:
        add("machine", "Machine")
    if "folder" in entity_types or source == "exact_dashboard.folder_days":
        if _resolve_row_field(rows, "folder_name"):
            add("folder_name", "Folder")
        else:
            add("folder", "Folder")
    if "tower" in entity_types or source == "tower_days":
        if _resolve_row_field(rows, "tower_name"):
            add("tower_name", "Tower")
        else:
            add("tower", "Tower")

    for condition in plan.get("conditions") or []:
        if not isinstance(condition, dict):
            continue
        field = _clean_text(condition.get("field"))
        if field and field not in {"run_date", "date"}:
            add(field, _humanize_field(field))
    for metric in plan.get("metrics") or []:
        field = _clean_text(metric.get("field") if isinstance(metric, dict) else metric)
        if field and field not in {"run_date", "date"}:
            add(field, _humanize_field(field))
    return fields


def _build_date_count_evidence(
    plan: dict[str, Any],
    question: str,
    selected_rows: list[dict[str, Any]],
    source_rows: list[dict[str, Any]],
) -> dict[str, Any] | None:
    if not _plan_counts_dates(plan, question):
        return None
    date_field = _resolve_row_field(source_rows or selected_rows, "run_date") or _resolve_row_field(
        source_rows or selected_rows, "date"
    )
    if not date_field:
        return None

    evidence_fields = _date_count_evidence_fields(plan, source_rows or selected_rows)
    projected_rows: list[list[str]] = []
    seen_rows: set[tuple[str, ...]] = set()
    for row in sorted(
        selected_rows,
        key=lambda item: (
            _clean_text(item.get(date_field)),
            *[_row_value_text(item.get(field)) for field, _ in evidence_fields],
        ),
    ):
        date_value = _clean_text(row.get(date_field))
        if not date_value:
            continue
        projected = [date_value, *[_row_value_text(row.get(field)) for field, _ in evidence_fields]]
        row_key = tuple(projected)
        if row_key not in seen_rows:
            seen_rows.add(row_key)
            projected_rows.append(projected)

    dates = sorted({row[0] for row in projected_rows if row and row[0]})
    unit = _date_count_unit(question)
    singular = unit.rstrip("s")
    display_unit = singular if len(dates) == 1 else singular + "s"
    headline = f"**{len(dates)} distinct {display_unit}** matched the requested conditions."
    headers = ["Date", *[label for _, label in evidence_fields]]
    if projected_rows:
        answer = _markdown_table(headers, projected_rows) + "\n\n" + headline
    else:
        answer = headline + "\n\nNo matching dates were found."

    required_values = sorted({
        value
        for row in projected_rows
        for value in row[1:]
        if value
    })
    return {
        "distinct_date_count": len(dates),
        "unit": singular,
        "dates": dates,
        "columns": headers,
        "rows": projected_rows,
        "required_values": required_values,
        "answer": answer,
    }


def _date_count_evidence_for_plan(
    plan: dict[str, Any] | None,
    question: str,
    context: dict[str, Any],
) -> dict[str, Any] | None:
    if not isinstance(plan, dict) or not plan:
        return None
    source_rows = _rows_for_plan_source(_clean_text(plan.get("primary_source")), context)
    if not source_rows or not _plan_counts_dates(plan, _clean_text(question).casefold()):
        return None
    selected_rows = _apply_qu_conditions(
        source_rows,
        plan.get("conditions") or [],
        _clean_text(plan.get("condition_logic") or "AND").upper(),
    )
    if selected_rows is None:
        return None
    selected_rows = _apply_qu_time_scope(selected_rows, plan.get("time_scope") or {})
    selected_rows = _apply_qu_entity_filters(selected_rows, plan.get("entities") or [])
    return _build_date_count_evidence(
        plan,
        _clean_text(question).casefold(),
        selected_rows,
        source_rows,
    )


def _date_count_answer_is_complete(answer: str, evidence: dict[str, Any]) -> bool:
    expected_dates = set(evidence.get("dates") or [])
    answer_dates = set(re.findall(r"\b20\d{2}-\d{2}-\d{2}\b", answer))
    if answer_dates != expected_dates:
        return False
    count = int(evidence.get("distinct_date_count") or 0)
    unit = re.escape(_clean_text(evidence.get("unit") or "day"))
    count_pattern = re.compile(
        rf"(?:\*\*)?{count}(?:\*\*)?\s+(?:distinct\s+)?{unit}s?\b",
        flags=re.IGNORECASE,
    )
    if not count_pattern.search(answer):
        return False
    return all(value in answer for value in (evidence.get("required_values") or []))


def _plan_components_breakdown(question: str, rows: list[dict[str, Any]], context: dict[str, Any]) -> str:
    if not rows:
        return ""

    top_level_fields = [
        ("runtime_min", "Run Time"),
        ("loss_time_min", "Lost Time"),
        ("downtime_min", "Downtime"),
        ("waiting_time_min", "Wait Time"),
        ("spare_time_min", "Spare Time"),
    ]
    resolved_fields = [(f, _resolve_row_field(rows, f), label) for f, label in top_level_fields]
    resolved_fields = [(f, resolved, label) for f, resolved, label in resolved_fields if resolved]

    lines: list[str] = []
    has_content = False

    # Show a per-row table when there are few enough matched rows for it to be readable —
    # this is the common case for "which nights..." questions, and shows zero-activity rows
    # (e.g. a fully unplanned night) instead of silently omitting them from a totals-only summary.
    date_field = _resolve_row_field(rows, "run_date")
    if date_field and len(rows) <= 30 and resolved_fields:
        sorted_rows = sorted(rows, key=lambda r: _clean_text(r.get(date_field)))
        header = ["Date"] + [label for _, _, label in resolved_fields]
        lines.append("")
        lines.append("| " + " | ".join(header) + " |")
        lines.append("| " + " | ".join("---" for _ in header) + " |")
        for row in sorted_rows:
            cells = [_clean_text(row.get(date_field))] + [
                f"{_number(row.get(resolved)):g}" for _, resolved, _ in resolved_fields
            ]
            lines.append("| " + " | ".join(cells) + " |")
        has_content = True

    totals_lines = ["", "Key time components (totals across matched rows):"]
    for field, resolved, label in resolved_fields:
        total = sum(_number(row.get(resolved)) for row in rows)
        if total <= 0:
            continue
        totals_lines.append(f"- **{label}**: {total:g} min total")
    if len(totals_lines) > 2:
        lines.extend(totals_lines)
        has_content = True

    if date_field:
        loss_components_by_date = {
            _clean_text(r.get("run_date")): r.get("loss_components") or {}
            for r in (context.get("loss_time") or {}).get("all_days") or []
        }
        component_totals: dict[str, float] = {}
        for row in rows:
            date = _clean_text(row.get(date_field))
            for key, minutes in loss_components_by_date.get(date, {}).items():
                component_totals[key] = component_totals.get(key, 0.0) + _number(minutes)
        if component_totals:
            component_labels = dict(LOSS_COMPONENTS)
            lines.append("")
            lines.append("Lost Time sub-components:")
            for key, minutes in sorted(component_totals.items(), key=lambda kv: -kv[1]):
                lines.append(f"- **{component_labels.get(key, key)}**: {minutes:g} min")
            has_content = True

    return "\n".join(lines) if has_content else ""


def _plan_metric_fields(plan: dict[str, Any], rows: list[dict[str, Any]], message: str) -> list[str]:
    fields: list[str] = []
    for metric in plan.get("metrics") or []:
        field = _resolve_row_field(rows, _clean_text(metric))
        if field and field not in fields and _field_has_numeric_values(rows, field):
            fields.append(field)

    if fields:
        return fields

    question = _clean_text(message).casefold()
    metric_aliases = [
        ("runtime_min", ["runtime", "run time", "run"]),
        ("runtime_minutes", ["runtime", "run time", "run"]),
        ("downtime_min", ["downtime", "down time"]),
        ("allocated_downtime_min", ["downtime", "down time"]),
        ("loss_time_min", ["lost time", "lost time", "loss"]),
        ("lost_time_min", ["lost time", "lost time", "loss"]),
        ("waiting_time_min", ["wait time", "waiting time", "wait"]),
        ("spare_time_min", ["spare time", "spare"]),
        ("utilization_pct", ["utilization", "utilisation"]),
        ("print_order", ["print order", "po", "copies"]),
        ("edition_count", ["edition"]),
        ("event_count", ["event", "incident", "count"]),
        ("count", ["event", "incident", "count"]),
        ("total_minutes", ["minutes", "time"]),
    ]
    for field, aliases in metric_aliases:
        resolved = _resolve_row_field(rows, field)
        if resolved and _field_has_numeric_values(rows, resolved) and any(alias in question for alias in aliases):
            fields.append(resolved)
            break
    return fields


def _plan_group_field(plan: dict[str, Any], rows: list[dict[str, Any]], message: str) -> str:
    group_by = _clean_text(plan.get("group_by"))
    computation = _clean_text(plan.get("computation")).casefold()
    question = _clean_text(message).casefold()
    source = _clean_text(plan.get("primary_source")).casefold()

    candidates: list[str] = []
    if group_by and group_by != "none":
        candidates.append(group_by)

    # Night classification (GNP/UV vs SNP/non-UV) and tower UV status are checked BEFORE the
    # generic complexity-code heuristic below: "GNP nights" means night_type, not a breakdown by
    # individual C-code, even though the question also contains "gnp"/"snp".
    if "night" in question and any(term in question for term in ["gnp", "snp", "uv"]):
        candidates.append("night_type")
    if "tower" in question and any(term in question for term in ["uv", "non-uv", "non uv"]):
        candidates.append("uv_tower")

    for phrase in ["group by", "grouped by", "per", "by"]:
        match = re.search(rf"{phrase}\s+([a-zA-Z_ ]+)", computation)
        if match:
            candidates.append(match.group(1).strip())
    if "type" in computation or "category" in computation or source == "complexity_by_code":
        candidates.extend(["type", "category", "complexity"])
    if any(term in question for term in ["snp", "gnp", "complex"]):
        candidates.extend(["type", "complexity", "complexity_code"])

    for candidate in candidates:
        field = _resolve_row_field(rows, candidate)
        # Reject list-valued fields (complexity_codes, editions, ...) as group keys — each unique
        # combination would become its own group, which is never what a comparison/breakdown
        # question actually wants, even when a fuzzy candidate match resolves to one of them.
        if field and not _is_list_valued_field(rows, field):
            return field
    return ""


def _is_list_valued_field(rows: list[dict[str, Any]], field: str) -> bool:
    samples = [row.get(field) for row in rows[:20] if field in row]
    if not samples:
        return False
    return sum(1 for value in samples if isinstance(value, list)) > len(samples) / 2


def _resolve_row_field(rows: list[dict[str, Any]], wanted: str) -> str:
    if not rows or not wanted:
        return ""
    available = list(rows[0].keys())
    wanted_norm = _normalize_field_name(wanted)
    aliases = _field_aliases(wanted_norm)
    for key in available:
        if _normalize_field_name(key) == wanted_norm:
            return key
    for alias in aliases:
        for key in available:
            if _normalize_field_name(key) == alias:
                return key
    for key in available:
        key_norm = _normalize_field_name(key)
        if wanted_norm and (wanted_norm in key_norm or key_norm in wanted_norm):
            return key
    return ""


def _field_aliases(field: str) -> list[str]:
    aliases = {
        "folder": ["folder", "foldername", "resource"],
        "foldername": ["folder", "foldername", "resource"],
        "tower": ["tower", "towername", "resource"],
        "date": ["date", "rundate", "reportdate"],
        "rundate": ["date", "rundate", "reportdate"],
        "plant": ["plant", "plantname"],
        "type": ["type", "category", "complexitytype"],
        "complexity": ["complexity", "complexitycode", "code", "type"],
        "runtime": ["runtime", "runtimemin", "runtimeminutes", "totalruntimemnts"],
        "runtime_min": ["runtime", "runtimemin", "runtimeminutes", "totalruntimemnts"],
        "downtime": ["downtime", "downtimemin", "totaldowntime", "allocateddowntimemin"],
        "loss": ["loss", "losstime", "losstimemin", "losttime", "losttimemin"],
        "count": ["count", "eventcount", "incidentcount", "downtimecount", "editioncount"],
    }
    return aliases.get(field, [])


def _normalize_field_name(value: str) -> str:
    return re.sub(r"[^a-z0-9]+", "", _clean_text(value).casefold())


def _field_has_numeric_values(rows: list[dict[str, Any]], field: str) -> bool:
    for row in rows:
        value = row.get(field)
        if isinstance(value, bool) or value is None or value == "":
            continue
        try:
            if isfinite(float(value)):
                return True
        except (TypeError, ValueError):
            continue
    return False


def _row_value_text(value: Any) -> str:
    if isinstance(value, list):
        return " ".join(_clean_text(item) for item in value)
    return _clean_text(value)


def _default_group_field(rows: list[dict[str, Any]]) -> str:
    for field in ["type", "category", "resource", "folder", "tower", "run_date", "plant", "reason", "code"]:
        resolved = _resolve_row_field(rows, field)
        if resolved:
            return resolved
    return ""


def _plan_group_value(row: dict[str, Any], group_field: str) -> str:
    value = row.get(group_field)
    if isinstance(value, list):
        return ", ".join(_clean_text(item) for item in value if _clean_text(item)) or "Unknown"
    return _clean_text(value) or "Unknown"


def _aggregate_plan_rows(
    rows: list[dict[str, Any]],
    metric_fields: list[str],
    group_field: str,
    average: bool,
) -> list[dict[str, Any]]:
    groups: dict[str, dict[str, Any]] = {}
    for row in rows:
        key = _plan_group_value(row, group_field)
        bucket = groups.setdefault(key, {"group": key, "row_count": 0})
        bucket["row_count"] += 1
        for metric in metric_fields:
            bucket[metric] = bucket.get(metric, 0.0) + _number(row.get(metric))

    result = []
    for bucket in groups.values():
        out = {"group": bucket["group"], "row_count": bucket["row_count"]}
        for metric in metric_fields:
            value = bucket.get(metric, 0.0)
            if average:
                value = value / bucket["row_count"] if bucket["row_count"] else 0.0
            out[metric] = _clean_number(value)
        result.append(out)
    return sorted(result, key=lambda row: row["group"])


def _format_plan_grouped_answer(
    source_key: str,
    metric_fields: list[str],
    group_field: str,
    rows: list[dict[str, Any]],
    average: bool,
) -> str:
    label = "Average" if average else "Total"
    metric_labels = ", ".join(_humanize_field(metric) for metric in metric_fields)
    headers = [_humanize_field(group_field), *[_humanize_field(metric) for metric in metric_fields]]
    include_row_count = average and any(_number(row.get("row_count")) != 1 for row in rows)
    if include_row_count:
        headers.append("Rows")
    lines = [
        f"{label} {metric_labels} by {_humanize_field(group_field)}:",
        "",
        "| " + " | ".join(headers) + " |",
        "| " + " | ".join(["---"] * len(headers)) + " |",
    ]
    for row in rows[:20]:
        values = [str(row.get("group"))]
        values.extend(_format_plan_metric_value(metric, row.get(metric)) for metric in metric_fields)
        if include_row_count:
            values.append(str(row.get("row_count")))
        lines.append("| " + " | ".join(values) + " |")
    if len(rows) > 20:
        lines.append(f"\n*... {len(rows) - 20} more groups omitted.*")
    return "\n".join(lines)


def _format_plan_count_answer(source_key: str, group_field: str, counts: list[tuple[str, int]]) -> str:
    lines = [
        f"Count by {_humanize_field(group_field)} from {source_key}:",
        "",
        f"| {_humanize_field(group_field)} | Count |",
        "| --- | --- |",
    ]
    for group, count in counts[:20]:
        lines.append(f"| {group} | {count} |")
    if len(counts) > 20:
        lines.append(f"\n*... {len(counts) - 20} more groups omitted.*")
    return "\n".join(lines)


def _humanize_field(field: str) -> str:
    text = re.sub(r"[_\s]+", " ", _clean_text(field)).strip()
    text = re.sub(r"\bmin\b", "minutes", text)
    text = text.replace("pct", "%")
    return text.title() if text else "Value"


def _metric_suffix(metric: str) -> str:
    metric_norm = _normalize_field_name(metric)
    if "pct" in metric_norm or "percentage" in metric_norm:
        return "%"
    if "time" in metric_norm or "runtime" in metric_norm or "downtime" in metric_norm or "minute" in metric_norm or metric_norm.endswith("min"):
        return " min"
    return ""


def _format_plan_metric_value(metric: str, value: Any) -> str:
    numeric = _number(value)
    suffix = _metric_suffix(metric)
    if suffix == " min":
        return _format_chat_minutes(numeric)
    if suffix == "%":
        return f"{_clean_number(numeric)}%"
    return str(_clean_number(numeric))


def _format_chat_minutes(value: Any) -> str:
    return f"{int(round(_number(value)))} min"


def _format_signed_chat_minutes(value: Any) -> str:
    numeric = _number(value)
    sign = "+" if numeric > 0 else ""
    return f"{sign}{int(round(numeric))} min"


def _asks_complexity_downtime(question: str) -> bool:
    return "complex" in question and any(term in question for term in ["downtime", "down time", "stoppage"])


def _answer_complexity_downtime_question(context: dict[str, Any]) -> str:
    rows = context.get("complexity_downtime_by_code") or []
    rows = sorted(rows, key=lambda row: -_number(row.get("allocated_downtime_min")))[:8]
    if not rows:
        return "Not available in the current data."
    lines = [
        "Complexity downtime by C-code:",
        "",
        "| Rank | Code | Downtime | Downtime Rows | Runtime |",
        "| --- | --- | --- | --- | --- |",
    ]
    for index, row in enumerate(rows, start=1):
        lines.append(
            f"| {index} | {row.get('code')} | {_format_chat_minutes(row.get('allocated_downtime_min'))} | "
            f"{row.get('downtime_row_count')} | {_format_chat_minutes(row.get('runtime_min'))} |"
        )
    return "\n".join(lines)


def _asks_delayed_pf(question: str) -> bool:
    # Bare "print finish" is NOT included here — that phrase alone asks for the actual finish time
    # (every night has one), not the delayed subset. Only route to delayed_pf when the question
    # explicitly signals lateness/delay/threshold.
    return any(
        term in question
        for term in ["delayed pf", "delayed print", "pf threshold", "threshold", "overrun", "late finish", "crossed"]
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

    lines = [
        f"**{len(filtered or rows)}** delayed print finish rows found in the current data. Top overruns:",
        "",
        "| Rank | Date | Plant | Folder | Overrun | Cutoff | Finish | Complexity | Editions |",
        "| --- | --- | --- | --- | --- | --- | --- | --- | --- |",
    ]
    for index, row in enumerate(selected, start=1):
        complexities = ", ".join(_string_list(row.get("complexity_codes"))) or "-"
        editions_list = _string_list(row.get("editions"))
        editions = ", ".join(editions_list[:3])
        if len(editions_list) > 3:
            editions += " …"
        lines.append(
            f"| {index} | {row.get('run_date')} | {row.get('plant')} | {row.get('folder')} | "
            f"{_format_chat_minutes(row.get('overrun_minutes'))} | {row.get('pf_cutoff_time')} | "
            f"{row.get('estimated_print_finish_time')} | {complexities} | {editions or '-'} |"
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

    lines = [
        f"Edition list by {label}:",
        "",
        f"| {label.title()} | Editions | Count |",
        "| --- | --- | --- |",
    ]
    for row in selected:
        editions = _string_list(row.get("editions"))
        edition_text = ", ".join(editions[:12])
        if len(editions) > 12:
            edition_text += f" (+{len(editions) - 12} more)"
        lines.append(f"| {row.get(name_key)} | {edition_text or 'none'} | {len(editions)} |")
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
    lines = [
        "Downtime reasons by event count:",
        "",
        "| Rank | Reason | Events | Total Time | Machines/Folders Affected |",
        "| --- | --- | --- | --- | --- |",
    ]
    for index, row in enumerate(selected, start=1):
        lines.append(
            f"| {index} | {row.get('reason')} | {row.get('count')} | "
            f"{_format_chat_minutes(row.get('total_minutes'))} | {row.get('affected_machine_folders')} |"
        )
    return "\n".join(lines)


def _asks_summary_metric(question: str) -> bool:
    metric_terms = [
        "runtime", "run time", "downtime", "down time", "lost time", "lost time", "wait time",
        "waiting time", "spare", "unplanned", "utilized", "utilised", "utilization", "utilisation", "available", "mot",
    ]
    # A flat plant-wide total is the WRONG shape of answer for any question that's actually asking
    # for a breakdown/trend/pattern — answering with one number there would be confidently wrong,
    # not just imprecise, so this matcher must not fire for those (let the LLM/planner handle them).
    breakdown_terms = [
        "each", "every", "per ", "per-", "by folder", "by tower", "by date", "by day", "by week",
        "weekday", "week day", "day of week", "day-of-week", "trend", "pattern", "breakdown",
        "compare", "comparison", "tabular", "table", "over time",
    ]
    if any(term in question for term in breakdown_terms):
        return False
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
        parts.append(f"MOT (Run + Down): {_format_chat_minutes(mot)}")
    if "runtime" in question or "run time" in question:
        parts.append(f"Run Time: {_format_chat_minutes(summary.get('total_runtime_min'))}")
    if "downtime" in question or "down time" in question:
        parts.append(f"Downtime: {_format_chat_minutes(summary.get('total_downtime_min'))}")
    if "lost time" in question or "lost time" in question:
        parts.append(f"Lost Time: {_format_chat_minutes(summary.get('total_loss_time_min'))}")
    if "wait time" in question or "waiting time" in question or re.search(r"\bwait\b", question):
        parts.append(f"Wait Time: {_format_chat_minutes(summary.get('total_waiting_time_min'))}")
    if "spare capacity" in question:
        parts.append(f"Spare Capacity: {summary.get('spare_capacity_pct')}%")
    elif "spare" in question:
        parts.append(f"Spare Time: {_format_chat_minutes(summary.get('total_spare_time_min'))}")
    if "unplanned" in question:
        parts.append(f"Unplanned Time: {_format_chat_minutes(summary.get('total_unplanned_time_min'))}")
    if "utilized time" in question or "utilised time" in question:
        utilized_time = (
            _number(summary.get("total_utilized_time_min"))
            or _number(summary.get("total_runtime_min"))
            + _number(summary.get("total_overrun_minutes"))
            + _number(summary.get("total_loss_time_min"))
            + _number(summary.get("total_waiting_time_min"))
            + _number(summary.get("total_downtime_min"))
        )
        parts.append(
            f"Utilized Time: {_format_chat_minutes(utilized_time)} "
            "(Runtime + Overrun + Lost Time + Wait Time + Downtime)"
        )
    elif "utilization" in question or "utilisation" in question:
        parts.append(f"Utilisation: {summary.get('average_utilization_pct')}%")
    if "available" in question:
        parts.append(f"Available Capacity: {_format_chat_minutes(summary.get('total_available_capacity_min'))}")

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
    if "lost time" in question or "lost time" in question:
        return {"label": "Lost Time", "daily_key": "loss_time_min", "summary_key": "total_loss_time_min", "unit": "min"}
    if "wait time" in question or "waiting time" in question or re.search(r"\bwait\b", question):
        return {"label": "Wait Time", "daily_key": "waiting_time_min", "summary_key": "total_waiting_time_min", "unit": "min"}
    if "spare" in question:
        return {"label": "Spare Time", "daily_key": "spare_time_min", "summary_key": "total_spare_time_min", "unit": "min"}
    if "utilized time" in question or "utilised time" in question:
        return {
            "label": "Utilized Time",
            "daily_keys": ["runtime_min", "overrun_minutes_min", "loss_time_min", "waiting_time_min", "downtime_min"],
            "summary_keys": [
                "total_runtime_min", "total_overrun_minutes", "total_loss_time_min",
                "total_waiting_time_min", "total_downtime_min",
            ],
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
    average_display = (
        f"{_clean_number(average_value)}{spec['unit']}"
        if spec["unit"] == "%"
        else _format_chat_minutes(average_value)
    )
    total_display = (
        f"{_clean_number(total)}{spec['unit']}"
        if spec["unit"] == "%"
        else _format_chat_minutes(total)
    )
    return (
        f"Average {spec['label']} per day: {average_display} "
        f"({spec['label']} total {total_display} / {days} production days)."
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
    average_display = (
        f"{_clean_number(average_value)}{spec['unit']}"
        if spec["unit"] == "%"
        else _format_chat_minutes(average_value)
    )
    total_display = (
        f"{_clean_number(total)}{spec['unit']}"
        if spec["unit"] == "%"
        else _format_chat_minutes(total)
    )
    return (
        f"Average {spec['label']} per day per {folder_label[:-1]}: {average_display} "
        f"({spec['label']} total {total_display} / "
        f"{days} production days / {folder_count} {folder_label})."
    )


def _is_tower_downtime_frequency_question(question: str) -> bool:
    has_tower = any(term in question for term in ["tower", "towers"])
    has_downtime = any(term in question for term in ["downtime", "down time", "web break", "web-break", "break"])
    has_frequency = any(term in question for term in ["most often", "appear", "frequency", "frequent", "count", "instances", "events"])
    return has_tower and has_downtime and has_frequency


def _is_tower_usage_distribution_question(question: str) -> bool:
    has_tower = "tower" in question or "towers" in question
    has_used = any(term in question for term in ["used", "utilised", "utilized", "active", "running"])
    has_day_count = any(term in question for term in ["number of days", "days", "day_count", "y axis", "y-axis"])
    has_distribution = any(term in question for term in ["bar chart", "chart", "histogram", "distribution", "x axis", "x-axis"])
    has_towers_used_phrase = "number of towers used" in question or "towers used" in question
    return has_tower and has_used and has_distribution and (has_day_count or has_towers_used_phrase)


def _answer_tower_usage_distribution_question(context: dict[str, Any]) -> str:
    distribution = context.get("tower_usage_distribution") or _build_tower_usage_distribution(
        context.get("tower_availability") or {}
    )
    if not distribution:
        return "Not available in the current data."

    rows = [
        "| Towers used | Number of days |",
        "| --- | --- |",
    ]
    for row in distribution:
        rows.append(f"| {int(_number(row.get('towers_used')))} | {int(_number(row.get('day_count')))} |")
    return "\n".join(rows)


def _tower_usage_distribution_chart(context: dict[str, Any]) -> dict[str, Any] | None:
    distribution = context.get("tower_usage_distribution") or _build_tower_usage_distribution(
        context.get("tower_availability") or {}
    )
    points = [
        {
            "label": str(int(_number(row.get("towers_used")))),
            "value": int(_number(row.get("day_count"))),
        }
        for row in distribution
        if _number(row.get("day_count")) > 0
    ]
    if not points:
        return None
    return {
        "type": "bar",
        "title": "Number of days by towers used",
        "metric_label": "Number of days",
        "unit": "days",
        "data": points,
    }


def _is_tower_count_question(question: str) -> bool:
    has_tower = "tower" in question or "towers" in question
    asks_count = any(term in question for term in ["total", "count", "number of"]) or _asks_how_many(question)
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
    match = re.search(r"(\d+(?:\.\d+)?)\s*%", question)
    if match:
        return float(match.group(1))
    match = re.search(r"(\d+(?:\.\d+)?)\s*percent", question)
    if match:
        return float(match.group(1))
    return 0.0


def _is_utilization_threshold_question(question: str) -> bool:
    # No "tower" requirement here — this is the plant/night-level counterpart to
    # _is_tower_availability_threshold_question above (which requires "tower").
    has_night_scope = any(term in question for term in ["night", "nights", "day", "days"])
    has_capacity_term = any(
        term in question
        for term in ["capacity", "utilization", "utilisation", "utilized", "utilised"]
    )
    has_percent = "%" in question or "percent" in question
    return (
        has_night_scope
        and has_capacity_term
        and has_percent
        and "tower" not in question
        and _extract_numeric_condition(question) is not None
    )


def _answer_utilization_threshold_question(question: str, context: dict[str, Any]) -> str:
    exact_dashboard = context.get("exact_dashboard") or {}
    daily_rows = exact_dashboard.get("daily") or []
    condition = _extract_numeric_condition(question)
    if not condition or not daily_rows:
        return ""

    comparator, threshold = condition
    compare_fn = _COMPARATOR_FUNCS[comparator]
    matched = sorted(
        (row for row in daily_rows if compare_fn(_number(row.get("utilization_pct")), threshold)),
        key=lambda r: _clean_text(r.get("run_date")),
    )

    summary_line = (
        f"**{len(matched)}** night(s) had utilization {_COMPARATOR_LABELS[comparator]} {threshold:g}% "
        f"out of {len(daily_rows)} total nights in the current data."
    )
    if not matched:
        return summary_line

    # loss_components (changeover / late-start / reflong) live on the separate loss_time.all_days
    # table, keyed by run_date — join in here so "key time components" covers the loss breakdown,
    # not just the top-level runtime/loss/downtime/wait/spare split already on exact_dashboard.daily.
    loss_components_by_date = {
        _clean_text(row.get("run_date")): row.get("loss_components") or {}
        for row in (context.get("loss_time") or {}).get("all_days") or []
    }

    component_totals: dict[str, float] = {}
    table_lines = [
        "| Date | Utilization % | Run Time | Lost Time | Downtime | Wait Time | Spare Time |",
        "| --- | --- | --- | --- | --- | --- | --- |",
    ]
    for row in matched:
        date = _clean_text(row.get("run_date"))
        for key, minutes in loss_components_by_date.get(date, {}).items():
            component_totals[key] = component_totals.get(key, 0.0) + _number(minutes)
        table_lines.append(
            f"| {date} | {_number(row.get('utilization_pct')):g}% | "
            f"{_number(row.get('runtime_min')):g} | {_number(row.get('loss_time_min')):g} | "
            f"{_number(row.get('downtime_min')):g} | {_number(row.get('waiting_time_min')):g} | "
            f"{_number(row.get('spare_time_min')):g} |"
        )

    lines = [summary_line, "", *table_lines]

    if component_totals:
        component_labels = dict(LOSS_COMPONENTS)
        lines.append("")
        lines.append("Lost Time components across these nights:")
        for key, minutes in sorted(component_totals.items(), key=lambda kv: -kv[1]):
            lines.append(f"- **{component_labels.get(key, key)}**: {minutes:g} min")

    return "\n".join(lines)


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
            "",
            "| Rank | Tower | Attributed Events | Attributed Time | Matching Runs | Dates |",
            "| --- | --- | --- | --- | --- | --- |",
        ]
        for index, row in enumerate(ranked, start=1):
            dates = row.get("matching_dates") or []
            date_text = ", ".join(dates[:5])
            if len(dates) > 5:
                date_text += f" (+{len(dates) - 5} more)"
            lines.append(
                f"| {index} | {row.get('tower')} | {row.get('attributed_event_count')} | "
                f"{_format_chat_minutes(row.get('attributed_minutes'))} | {row.get('matching_tower_run_count')} | "
                f"{date_text or '-'} |"
            )
        note = attribution.get("attribution_note")
        if note:
            lines.append(f"\n*Note: {note}*")
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
            lines = [
                "Top individual towers by runs with downtime:",
                "",
                "| Rank | Tower | Runs | Downtime | Dates |",
                "| --- | --- | --- | --- | --- |",
            ]
            for index, row in enumerate(ranked, start=1):
                dates = sorted(row["dates"])
                date_text = ", ".join(dates[:5])
                if len(dates) > 5:
                    date_text += f" (+{len(dates) - 5} more)"
                lines.append(
                    f"| {index} | {row['tower']} | {row['run_count']} | "
                    f"{_format_chat_minutes(row['downtime_min'])} | {date_text or '-'} |"
                )
            return "\n".join(lines)

    ranked_towers = sorted(
        [row for row in tower_rows if _number(row.get("downtime_run_count")) > 0],
        key=lambda row: (-_number(row.get("downtime_run_count")), -_number(row.get("downtime_min")), _clean_text(row.get("tower"))),
    )[:5]
    if ranked_towers:
        lines = [
            "Top individual towers by downtime-run count:",
            "",
            "| Rank | Tower | Runs | Downtime |",
            "| --- | --- | --- | --- |",
        ]
        for index, row in enumerate(ranked_towers, start=1):
            lines.append(
                f"| {index} | {row.get('tower')} | {row.get('downtime_run_count')} | "
                f"{_format_chat_minutes(row.get('downtime_min'))} |"
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
            overrun_total = sum(_number(row.get("overrun_minutes")) for row in details)
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
            overrun_total = _number(daily.get("overrun_minutes"))
        runtime_segments = _runtime_segments_for_rows(details)
        is_gnp_night = any(_is_gnp_segment(segment) for segment in runtime_segments)
        delayed_rows = [row for row in details if _number(row.get("overrun_minutes")) > 0]
        max_overrun = max([_number(row.get("overrun_minutes")) for row in delayed_rows], default=0.0)
        last_finish = _last_print_finish_for_rows(details)

        rows.append({
            "run_date": run_date,
            "weekday": _weekday_label(run_date),
            "month": _month_label(run_date),
            "night_type": "GNP/UV" if is_gnp_night else "SNP/non-UV",
            "gnp_night": is_gnp_night,
            "uv_night": is_gnp_night,
            # The latest print finish across ALL folders that night, and which edition/folder it was —
            # this is a real observed finish time, not derived from delayed_pf (which only covers the
            # subset of finishes that crossed the compliance window).
            "print_finish_time": last_finish["print_finish_time"],
            "last_edition": last_finish["last_edition"],
            "last_edition_name": last_finish["last_edition_name"],
            "last_folder": last_finish["last_folder"],
            "active_folders": _clean_number(active_folders),
            "capacity_folders": _clean_number(capacity_folders),
            "available_capacity_min": _clean_number(available),
            "runtime_min": _clean_number(runtime),
            "loss_time_min": _clean_number(loss_time),
            "waiting_time_min": _clean_number(waiting_time),
            "downtime_min": _clean_number(downtime),
            "spare_time_min": _clean_number(spare_time),
            "unplanned_time_min": _clean_number(unplanned_time),
            "utilization_pct": _utilization_pct(runtime, overrun_total, available, waiting_time, loss_time, downtime),
            "spare_capacity_pct": _percentage(spare_time, max(available - unplanned_time, 0)),
            "delayed_pf_count": len(delayed_rows),
            "overrun_minutes_min": _clean_number(overrun_total),
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
        overrun_total = sum(_number(row.get("overrun_minutes")) for row in details)
        spare_time = sum(_number(row.get("buffer_time")) for row in details)
        unplanned_time = sum(_number(row.get("idle_time")) for row in details) + missing_days * CAPACITY_MINUTES_PER_FOLDER_DAY
        active_runtime = sum(_number(row.get("runtime")) for row in active_rows)
        active_waiting_time = sum(_number(row.get("waiting_time")) for row in active_rows)
        active_loss_time = sum(_loss_time_minutes(row) for row in active_rows)
        active_overrun = sum(_number(row.get("overrun_minutes")) for row in active_rows)
        active_downtime = sum(_number(row.get("downtime")) for row in active_rows)
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
            "utilization_pct": _utilization_pct(runtime, overrun_total, possible_capacity, waiting_time, loss_time, downtime),
            "active_day_utilization_pct": _utilization_pct(
                active_runtime, active_overrun, active_capacity, active_waiting_time, active_loss_time, active_downtime
            ),
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

    gnp_night_lookup = _gnp_night_lookup(folder_rows)
    rows = [_exact_folder_day_row(row, gnp_night_lookup) for row in selected_rows[:limit]]
    return rows, note


def _folder_day_context_limit(matching: bool) -> int:
    """Row cap for the LLM-facing folder_days view. Defaults to effectively unlimited — a fixed
    1500/2000-row cap here used to silently truncate longer date ranges and produce wrong
    aggregates/counts even when the question's own relevance filter (matching_rows) had already
    selected exactly the rows needed. CAPACITY_CHAT_MAX_FOLDER_DAY_ROWS still lets an operator
    reintroduce a cap (e.g. for cost control) without a code change."""
    configured = _get_env("CAPACITY_CHAT_MAX_FOLDER_DAY_ROWS")
    if configured:
        try:
            value = int(configured)
            if value <= 0:
                return 1_000_000
            return max(value, 1)
        except ValueError:
            pass
    return 1_000_000


def _exact_folder_day_row(
    row: dict[str, Any],
    gnp_night_lookup: dict[str, bool] | None = None,
) -> dict[str, Any]:
    available = _available_minutes(row)
    unplanned_time = _number(row.get("idle_time"))
    spare_time = _number(row.get("buffer_time"))
    runtime = _number(row.get("runtime"))
    loss_time = _loss_time_minutes(row)
    downtime = _number(row.get("downtime"))
    changeover_time = _number(row.get("change_over_time"))
    late_start_time = _number(row.get("late_start_time"))
    reflong_time = _number(row.get("reflong_related_downtime"))
    waiting_time = _number(row.get("waiting_time"))
    overrun = _number(row.get("overrun_minutes"))
    runtime_segments = _runtime_segment_rows(row)
    product_flags = _folder_product_flags(runtime_segments)
    machine, folder_name = _split_machine_folder(_clean_text(row.get("folder")))
    cutoff_minutes = _pf_compliance_minutes(row.get("plant_name"))
    run_date = _clean_text(row.get("run_date"))
    # Preserve the separate plant-wide classification for questions explicitly about the plant.
    # Product flags for this folder-night are derived independently from runtime_segments above.
    is_gnp_night = (gnp_night_lookup or {}).get(run_date, False)
    return {
        "run_date": run_date,
        "weekday": _weekday_label(row.get("run_date")),
        "month": _month_label(row.get("run_date")),
        "plant": _clean_text(row.get("plant_name")),
        "machine": machine,
        "folder_name": folder_name,
        "folder": _display_resource_name(row.get("folder")),
        "active_night": _is_active_folder_row(row),
        "plant_night_type": "GNP/UV" if is_gnp_night else "SNP/non-UV",
        "plant_gnp_night": is_gnp_night,
        **product_flags,
        "available_capacity_min": _clean_number(available),
        "runtime_min": _clean_number(runtime),
        "loss_time_min": _clean_number(loss_time),
        "change_over_time_min": _clean_number(changeover_time),
        "late_start_time_min": _clean_number(late_start_time),
        "reflong_time_min": _clean_number(reflong_time),
        "waiting_time_min": _clean_number(row.get("waiting_time")),
        "waiting_time_pct": _percentage(_number(row.get("waiting_time")), available),
        "downtime_min": _clean_number(downtime),
        "spare_time_min": _clean_number(spare_time),
        "unplanned_time_min": _clean_number(unplanned_time),
        "utilization_pct": _utilization_pct(runtime, overrun, available, waiting_time, loss_time, downtime),
        "spare_capacity_pct": _percentage(spare_time, max(available - unplanned_time, 0)),
        "delayed_print_finish": overrun > 0,
        "overrun_minutes": _clean_number(overrun),
        "pf_cutoff_time": _format_clock_time(cutoff_minutes),
        # print_finish_time is the ACTUAL clock time printing ended that night, populated for every
        # active night regardless of whether it was delayed. estimated_print_finish_time below is the
        # older, delayed-only field (cutoff + overrun) — kept for backward compatibility, but
        # print_finish_time is the one to use for "when did printing finish" questions in general.
        "print_finish_time": _print_finish_clock_time(row.get("actual_print_finish_time")),
        "last_edition": _clean_text(row.get("last_edition")),
        "last_edition_name": _clean_text(row.get("last_edition_name")),
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


def _book_detail_matches_question(row: dict[str, Any], question_text: str) -> bool:
    candidates = [
        row.get("Edition"),
        row.get("Edition Name"),
        row.get("Folder"),
        row.get("Machine"),
        row.get("Plant Name"),
        row.get("Report Date"),
    ]
    return any(_candidate_matches_question(_clean_text(c), question_text) for c in candidates if c)


def _select_book_details_for_llm(
    book_details: list[dict[str, Any]],
    question: str,
    limit: int | None,
) -> list[dict[str, Any]]:
    """Prioritize book_details rows relevant to the question instead of an arbitrary slice.

    Falls back to most-recent-first (by Report Date) when nothing matches, since the stored
    order is chronological-ascending — a plain [:limit] slice would otherwise drop recent data
    first, which is the opposite of what most questions care about.
    """
    if not book_details or limit is None or len(book_details) <= limit:
        return book_details or []

    question_text = _clean_text(question).casefold()
    if question_text:
        matching = [row for row in book_details if _book_detail_matches_question(row, question_text)]
        if matching:
            return matching[:limit]

    return sorted(book_details, key=lambda row: _clean_text(row.get("Report Date")), reverse=True)[:limit]


def _date_label(value: str) -> str:
    if not value:
        return ""
    try:
        parsed = datetime.strptime(value, "%Y-%m-%d")
        return f"{parsed.strftime('%b')} {parsed.day}"
    except ValueError:
        return value


def _weekday_label(value: str) -> str:
    """Full weekday name (e.g. 'Monday') for a run_date — precomputed so the LLM never has
    to derive day-of-week from raw date strings itself for 'weekday wise' / day-of-week questions."""
    if not value:
        return ""
    try:
        return datetime.strptime(_clean_text(value), "%Y-%m-%d").strftime("%A")
    except ValueError:
        return ""


def _month_label(value: str) -> str:
    """Sortable 'YYYY-MM' month for a run_date — precomputed so the LLM never has to derive or
    refuse a month/month-on-month grouping itself from raw date strings."""
    if not value:
        return ""
    try:
        return datetime.strptime(_clean_text(value), "%Y-%m-%d").strftime("%Y-%m")
    except ValueError:
        return ""


def _print_finish_clock_time(value: Any) -> str:
    """'HH:MM' clock time from a capacity.py 'YYYY-MM-DD HH:MM' print-finish timestamp string."""
    text = _clean_text(value)
    if not text or " " not in text:
        return ""
    return text.split(" ")[-1]


def _last_print_finish_for_rows(rows: list[dict[str, Any]]) -> dict[str, Any]:
    """Across a set of folder-day rows for one night, find whichever folder/edition finished
    printing latest — this is the PLANT-level "last print finish of the night", distinct from any
    single folder's own finish time. Returns clock time, full timestamp, and which edition it was.
    """
    candidates = [row for row in rows if _clean_text(row.get("actual_print_finish_time"))]
    if not candidates:
        return {
            "print_finish_time": "",
            "print_finish_timestamp": "",
            "last_edition": "",
            "last_edition_name": "",
            "last_folder": "",
        }
    last_row = max(candidates, key=lambda row: _clean_text(row.get("actual_print_finish_time")))
    timestamp = _clean_text(last_row.get("actual_print_finish_time"))
    return {
        "print_finish_time": _print_finish_clock_time(timestamp),
        "print_finish_timestamp": timestamp,
        "last_edition": _clean_text(last_row.get("last_edition")) or _clean_text(last_row.get("last_edition_name")),
        "last_edition_name": _clean_text(last_row.get("last_edition_name")),
        "last_folder": _display_resource_name(last_row.get("folder")),
    }


_WEEKDAY_ORDER = ["Monday", "Tuesday", "Wednesday", "Thursday", "Friday", "Saturday", "Sunday"]


def _tower_weekday_summary(tower_day_rows: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """Per-tower per-weekday averages, pre-aggregated from tower_days.

    tower_days itself gets row-capped for large multi-plant/multi-month datasets to stay within the
    model's context limit, which would silently under-sample some towers/weekdays. This table is a
    complete aggregate (every tower x every weekday it has data for) computed before any capping, so
    it stays small (towers x 7 at most) and accurate regardless of dataset size — use it instead of
    raw tower_days for any 'weekday wise' / day-of-week tower pattern question.
    """
    buckets: dict[tuple[str, str], dict[str, Any]] = {}
    for row in tower_day_rows:
        tower = _clean_text(row.get("tower"))
        weekday = _clean_text(row.get("weekday"))
        if not tower or not weekday:
            continue
        key = (tower, weekday)
        bucket = buckets.setdefault(key, {
            "tower": tower,
            "tower_name": row.get("tower_name"),
            "weekday": weekday,
            "night_count": 0,
            "runtime_min": 0.0,
            "downtime_min": 0.0,
            "loss_time_min": 0.0,
            "waiting_time_min": 0.0,
        })
        bucket["night_count"] += 1
        for key_name in ("runtime_min", "downtime_min", "loss_time_min", "waiting_time_min"):
            bucket[key_name] += _number(row.get(key_name))

    summary = []
    for bucket in buckets.values():
        count = bucket["night_count"]
        summary.append({
            "tower": bucket["tower"],
            "tower_name": bucket["tower_name"],
            "weekday": bucket["weekday"],
            "night_count": count,
            "avg_runtime_min": _clean_number(bucket["runtime_min"] / count) if count else 0,
            "avg_downtime_min": _clean_number(bucket["downtime_min"] / count) if count else 0,
            "avg_loss_time_min": _clean_number(bucket["loss_time_min"] / count) if count else 0,
            "avg_waiting_time_min": _clean_number(bucket["waiting_time_min"] / count) if count else 0,
        })

    summary.sort(key=lambda r: (
        r["tower"],
        _WEEKDAY_ORDER.index(r["weekday"]) if r["weekday"] in _WEEKDAY_ORDER else 7,
    ))
    return summary


def _tower_month_summary(tower_day_rows: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """Per-tower per-month totals/averages, pre-aggregated from tower_days.

    Same rationale as _tower_weekday_summary: tower_days is row-capped on large multi-plant/multi-month
    datasets, so grouping it directly for a month-on-month tower question could silently miss whole
    towers or months. This table is a complete aggregate (every tower x every month it has data for)
    computed before any capping, so it stays small (towers x number of months) and accurate regardless
    of dataset size — use it instead of raw tower_days for any tower-level month-on-month question.
    """
    buckets: dict[tuple[str, str], dict[str, Any]] = {}
    for row in tower_day_rows:
        tower = _clean_text(row.get("tower"))
        month = _clean_text(row.get("month"))
        if not tower or not month:
            continue
        key = (tower, month)
        bucket = buckets.setdefault(key, {
            "tower": tower,
            "tower_name": row.get("tower_name"),
            "month": month,
            "night_count": 0,
            "runtime_min": 0.0,
            "downtime_min": 0.0,
            "loss_time_min": 0.0,
            "waiting_time_min": 0.0,
        })
        bucket["night_count"] += 1
        for key_name in ("runtime_min", "downtime_min", "loss_time_min", "waiting_time_min"):
            bucket[key_name] += _number(row.get(key_name))

    summary = []
    for bucket in buckets.values():
        count = bucket["night_count"]
        summary.append({
            "tower": bucket["tower"],
            "tower_name": bucket["tower_name"],
            "month": bucket["month"],
            "night_count": count,
            "total_runtime_min": _clean_number(bucket["runtime_min"]),
            "total_downtime_min": _clean_number(bucket["downtime_min"]),
            "total_loss_time_min": _clean_number(bucket["loss_time_min"]),
            "total_waiting_time_min": _clean_number(bucket["waiting_time_min"]),
        })

    summary.sort(key=lambda r: (r["tower"], r["month"]))
    return summary


def _daily_efficiency_summary(tower_day_rows: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """Per-date plant-wide efficiency, derived from segment-level PO and runtime.

    Aggregates tower_runtime_segments data by run_date so the LLM and QU layer can answer
    questions like 'how many days was efficiency below 90%' without having to group the raw
    segment table themselves.  Fields:
      run_date, total_po, total_runtime_min, total_dt_min, actual_speed_cph,
      committed_speed_cph (weighted avg), efficiency_pct.
    """
    buckets: dict[str, dict[str, Any]] = {}
    for row in tower_day_rows:
        run_date = _clean_text(row.get("run_date"))
        if not run_date:
            continue
        dt_min = _number(row.get("downtime") or row.get("downtime_min"))
        bucket = buckets.setdefault(run_date, {
            "run_date": run_date,
            "total_po": 0.0,
            "total_runtime_min": 0.0,
            "total_dt_min": 0.0,
            "committed_speed_weighted": 0.0,
            "committed_speed_weight": 0.0,
        })
        bucket["total_dt_min"] += dt_min
        # tower_day_rows have already-processed segments (committed_speed_cph, not committed_speed)
        for seg in (row.get("runtime_segments") or []):
            minutes = _number(seg.get("minutes"))
            po = _number(seg.get("print_order"))
            committed = _number(seg.get("committed_speed_cph"))
            bucket["total_po"] += po
            bucket["total_runtime_min"] += minutes
            if committed > 0:
                bucket["committed_speed_weighted"] += committed * minutes
                bucket["committed_speed_weight"] += minutes

    result = []
    for bucket in buckets.values():
        total_po = bucket["total_po"]
        runtime_h = bucket["total_runtime_min"] / 60
        dt_h = bucket["total_dt_min"] / 60
        committed = (
            bucket["committed_speed_weighted"] / bucket["committed_speed_weight"]
            if bucket["committed_speed_weight"] > 0 else 0.0
        )
        actual = total_po / (runtime_h + dt_h) if (runtime_h + dt_h) > 0 else 0.0
        efficiency = _clean_number((actual / committed) * 100) if committed > 0 else None
        result.append({
            "run_date": bucket["run_date"],
            "total_po": _clean_number(total_po),
            "total_runtime_min": _clean_number(bucket["total_runtime_min"]),
            "total_dt_min": _clean_number(bucket["total_dt_min"]),
            "actual_speed_cph": _clean_number(actual),
            "committed_speed_cph": _clean_number(committed),
            "efficiency_pct": efficiency,
        })

    result.sort(key=lambda r: r["run_date"])
    return result


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
            "source_print_order": _clean_number(segment.get("source_print_order")),
            "committed_speed_cph": _clean_number(segment.get("committed_speed")),
            "actual_speed_cph": _clean_number(segment.get("actual_speed") or segment.get("effective_speed")),
            "speed_cph": _clean_number(segment.get("actual_speed") or segment.get("effective_speed")),
            "efficiency_pct": _clean_number(segment.get("speed_efficiency")),
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
                    "source_print_order": 0.0,
                    "speed_weighted_total": 0.0,
                    "speed_weight_minutes": 0.0,
                    "committed_speed_weighted_total": 0.0,
                    "committed_speed_weight_minutes": 0.0,
                },
            )
            minutes = _number(segment.get("minutes"))
            speed = _number(segment.get("speed_cph"))
            committed_speed = _number(segment.get("committed_speed_cph"))
            bucket["minutes"] += minutes
            bucket["print_order"] += _number(segment.get("print_order"))
            bucket["source_print_order"] += _number(segment.get("source_print_order"))
            if speed > 0:
                bucket["speed_weighted_total"] += speed * minutes
                bucket["speed_weight_minutes"] += minutes
            if committed_speed > 0:
                bucket["committed_speed_weighted_total"] += committed_speed * minutes
                bucket["committed_speed_weight_minutes"] += minutes

    result = []
    for bucket in buckets.values():
        speed = (
            bucket["speed_weighted_total"] / bucket["speed_weight_minutes"]
            if bucket["speed_weight_minutes"] > 0
            else _speed_from_print_order(bucket["print_order"], bucket["minutes"])
        )
        committed_speed = (
            bucket["committed_speed_weighted_total"] / bucket["committed_speed_weight_minutes"]
            if bucket["committed_speed_weight_minutes"] > 0
            else 0
        )
        result.append({
            "complexity_code": bucket.get("complexity_code"),
            "category": bucket.get("category"),
            "type": bucket.get("type"),
            "is_complex": bucket.get("is_complex"),
            "minutes": _clean_number(bucket["minutes"]),
            "print_order": _clean_number(bucket["print_order"]),
            "source_print_order": _clean_number(bucket["source_print_order"]),
            "committed_speed_cph": _clean_number(committed_speed),
            "actual_speed_cph": _clean_number(speed),
            "speed_cph": _clean_number(speed),
            "efficiency_pct": _clean_number((speed / committed_speed) * 100) if committed_speed > 0 else 0,
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


def _folder_product_flags(segments: list[dict[str, Any]]) -> dict[str, Any]:
    """Return product flags derived only from this folder-night's own runtime segments."""
    code_numbers = [
        int(code)
        for code in (re_fullmatch_complexity(segment.get("complexity_code")) for segment in segments)
        if code
    ]
    has_gnp = any(5 <= code <= 15 for code in code_numbers) or any(
        _is_gnp_segment(segment) for segment in segments
    )
    has_gnp_complex = any(9 <= code <= 15 for code in code_numbers)
    has_snp = any(1 <= code <= 4 for code in code_numbers) or any(
        _is_snp_segment(segment) for segment in segments
    )
    product_types = []
    if has_snp:
        product_types.append("SNP")
    if has_gnp:
        product_types.append("GNP")
    if has_gnp_complex:
        product_types.append("GNP Complex")
    return {
        "folder_has_gnp": has_gnp,
        "folder_has_gnp_complex": has_gnp_complex,
        "folder_has_snp": has_snp,
        "folder_has_snp_only": has_snp and not has_gnp,
        "folder_product_types": product_types,
    }


def _has_gnp_runtime(rows: list[dict[str, Any]]) -> bool:
    return any(_is_gnp_segment(segment) for row in rows for segment in _runtime_segment_rows(row))


def _build_gnp_snp_folder_analysis(
    folder_rows: list[dict[str, Any]],
    tower_day_rows: list[dict[str, Any]],
    tower_reason_attribution: dict[str, Any],
) -> dict[str, Any]:
    """Small precomputed tables for recurring GNP-vs-SNP natural-language questions.

    Metrics are split by runtime-segment share when a folder/night has both GNP and SNP
    runtime. This keeps the table compact while still letting the model calculate from data
    rather than infer from an arbitrary sample of raw folder rows.
    """
    product_buckets: dict[str, dict[str, Any]] = {}
    gnp_folder_buckets: dict[str, dict[str, Any]] = {}
    gnp_dates: dict[str, dict[str, Any]] = {}
    delayed_complexity_rows: list[dict[str, Any]] = []

    def product_bucket(product_type: str) -> dict[str, Any]:
        return product_buckets.setdefault(product_type, {
            "product_type": product_type,
            "folder_day_count": 0,
            "runtime_min": 0.0,
            "spare_time_min": 0.0,
            "loss_time_min": 0.0,
            "waiting_time_min": 0.0,
            "lpr_to_start_min": 0.0,
            "reflong_time_min": 0.0,
            "change_over_time_min": 0.0,
            "downtime_min": 0.0,
            "overrun_min": 0.0,
            "delayed_folder_day_count": 0,
        })

    for row in folder_rows or []:
        if not _is_active_folder_row(row):
            continue
        segments = _runtime_segment_rows(row)
        gnp_runtime = sum(_number(seg.get("minutes")) for seg in segments if _is_gnp_segment(seg))
        snp_runtime = sum(_number(seg.get("minutes")) for seg in segments if _is_snp_segment(seg))
        total_classified_runtime = gnp_runtime + snp_runtime
        if total_classified_runtime <= 0:
            continue

        run_date = _clean_text(row.get("run_date"))
        folder = _display_resource_name(row.get("folder"))
        runtime = _number(row.get("runtime"))
        loss_time = _loss_time_minutes(row)
        waiting_time = _number(row.get("waiting_time"))
        lpr_to_start = _number(row.get("late_start_time"))
        reflong_time = _number(row.get("reflong_related_downtime"))
        change_over_time = _number(row.get("change_over_time"))
        downtime = _number(row.get("downtime"))
        spare_time = _number(row.get("buffer_time"))
        overrun = _number(row.get("overrun_minutes"))
        row_codes = _complexity_codes_for_segments(segments)
        row_categories = _complexity_categories_for_segments(segments)

        product_shares = []
        if gnp_runtime > 0:
            product_shares.append(("GNP", gnp_runtime / total_classified_runtime, gnp_runtime))
        if snp_runtime > 0:
            product_shares.append(("SNP", snp_runtime / total_classified_runtime, snp_runtime))

        for product_type, share, product_runtime in product_shares:
            bucket = product_bucket(product_type)
            bucket["folder_day_count"] += 1
            bucket["runtime_min"] += product_runtime
            bucket["spare_time_min"] += spare_time * share
            bucket["loss_time_min"] += loss_time * share
            bucket["waiting_time_min"] += waiting_time * share
            bucket["lpr_to_start_min"] += lpr_to_start * share
            bucket["reflong_time_min"] += reflong_time * share
            bucket["change_over_time_min"] += change_over_time * share
            bucket["downtime_min"] += downtime * share
            bucket["overrun_min"] += overrun * share
            if overrun > 0:
                bucket["delayed_folder_day_count"] += 1

        if gnp_runtime > 0:
            date_bucket = gnp_dates.setdefault(run_date, {
                "run_date": run_date,
                "folders": set(),
                "spare_time_min": 0.0,
            })
            if folder:
                date_bucket["folders"].add(folder)
            date_bucket["spare_time_min"] += spare_time

            folder_bucket = gnp_folder_buckets.setdefault(folder, {
                "folder": folder,
                "gnp_folder_day_count": 0,
                "loss_time_min": 0.0,
                "change_over_time_min": 0.0,
                "lpr_to_start_min": 0.0,
                "reflong_time_min": 0.0,
            })
            folder_bucket["gnp_folder_day_count"] += 1
            folder_bucket["loss_time_min"] += loss_time
            folder_bucket["change_over_time_min"] += change_over_time
            folder_bucket["lpr_to_start_min"] += lpr_to_start
            folder_bucket["reflong_time_min"] += reflong_time

        if overrun > 0:
            delayed_complexity_rows.append({
                "run_date": run_date,
                "folder": folder,
                "print_finish_time": _print_finish_clock_time(row.get("actual_print_finish_time")),
                "estimated_print_finish_time": _format_clock_time(_pf_compliance_minutes(row.get("plant_name")) + overrun),
                "overrun_minutes": _clean_number(overrun),
                "product_mix": "/".join(product for product, _, _ in product_shares),
                "has_gnp": gnp_runtime > 0,
                "has_snp": snp_runtime > 0,
                "complexity_codes": row_codes,
                "complexity_categories": row_categories,
                "editions": _editions_for_rows([row]),
                "largest_components": _largest_delayed_pf_components(row),
            })

    comparison_rows = []
    for product_type in ("GNP", "SNP"):
        bucket = product_buckets.get(product_type)
        if not bucket:
            continue
        count = max(int(bucket["folder_day_count"]), 1)
        delayed_count = int(bucket["delayed_folder_day_count"])
        comparison_rows.append({
            "product_type": product_type,
            "folder_day_count": int(bucket["folder_day_count"]),
            "total_runtime_min": _clean_number(bucket["runtime_min"]),
            "avg_spare_time_min": _clean_number(bucket["spare_time_min"] / count),
            "avg_loss_time_min": _clean_number(bucket["loss_time_min"] / count),
            "avg_waiting_time_min": _clean_number(bucket["waiting_time_min"] / count),
            "avg_lpr_to_start_min": _clean_number(bucket["lpr_to_start_min"] / count),
            "avg_reflong_time_min": _clean_number(bucket["reflong_time_min"] / count),
            "avg_downtime_min": _clean_number(bucket["downtime_min"] / count),
            "avg_change_over_time_min": _clean_number(bucket["change_over_time_min"] / count),
            "delayed_folder_day_count": delayed_count,
            "delayed_folder_day_pct": _percentage(delayed_count, bucket["folder_day_count"]),
            "avg_overrun_min": _clean_number(bucket["overrun_min"] / count),
        })

    gnp_loss_breakdown = []
    for bucket in gnp_folder_buckets.values():
        count = max(int(bucket["gnp_folder_day_count"]), 1)
        gnp_loss_breakdown.append({
            "folder": bucket["folder"],
            "gnp_folder_day_count": int(bucket["gnp_folder_day_count"]),
            "total_loss_time_min": _clean_number(bucket["loss_time_min"]),
            "avg_loss_time_min": _clean_number(bucket["loss_time_min"] / count),
            "change_over_time_min": _clean_number(bucket["change_over_time_min"]),
            "lpr_to_start_min": _clean_number(bucket["lpr_to_start_min"]),
            "reflong_time_min": _clean_number(bucket["reflong_time_min"]),
        })
    gnp_loss_breakdown.sort(key=lambda row: (-_number(row.get("total_loss_time_min")), row.get("folder", "")))

    nights_with_min_3 = []
    for bucket in gnp_dates.values():
        folders = sorted(bucket["folders"])
        count = len(folders)
        if count < 3:
            continue
        nights_with_min_3.append({
            "run_date": bucket["run_date"],
            "gnp_folder_count": count,
            "total_spare_time_min": _clean_number(bucket["spare_time_min"]),
            "avg_spare_time_min": _clean_number(bucket["spare_time_min"] / count) if count else 0,
            "folders": folders,
        })
    nights_with_min_3.sort(key=lambda row: row["run_date"])

    delayed_complexity_rows.sort(key=lambda row: (-_number(row.get("overrun_minutes")), row.get("run_date", ""), row.get("folder", "")))

    web_break_rows = _build_web_break_gnp_snp_tower_comparison(tower_day_rows, tower_reason_attribution)

    return {
        "definition": (
            "Base GNP includes C5-C15 and base SNP includes C1-C4. "
            "When a folder/night contains both, row-level non-runtime metrics are allocated by classified runtime share."
        ),
        "comparison_by_product_type": comparison_rows,
        "gnp_loss_breakdown_by_folder": gnp_loss_breakdown,
        "nights_with_min_3_gnp_folders": nights_with_min_3,
        "delayed_finish_complexity": delayed_complexity_rows,
        "web_break_gnp_snp_tower_comparison": web_break_rows,
        "correlation_summary": _gnp_snp_delay_correlation_summary(comparison_rows),
    }


def _build_web_break_gnp_snp_tower_comparison(
    tower_day_rows: list[dict[str, Any]],
    tower_reason_attribution: dict[str, Any],
) -> list[dict[str, Any]]:
    target_aliases = {
        "colorman c pu5": ["colorman c", "pu 5"],
        "colorman d pu5": ["colorman d", "pu 5"],
        "groman b pu4": ["groman b", "pu 4"],
        "geoman b pu4": ["geoman b", "pu 4"],
    }
    def normalized(value: str) -> str:
        return re.sub(r"[^a-z0-9]+", " ", _clean_text(value).casefold()).strip()

    product_runtime_by_tower: dict[str, dict[str, Any]] = {}
    for row in tower_day_rows or []:
        tower = _clean_text(row.get("tower"))
        if not tower:
            continue
        bucket = product_runtime_by_tower.setdefault(tower, {
            "GNP": 0.0,
            "SNP": 0.0,
            "gnp_tower_day_count": 0,
            "snp_tower_day_count": 0,
        })
        has_gnp = False
        has_snp = False
        for segment in row.get("runtime_segments") or []:
            minutes = _number(segment.get("minutes"))
            if _is_gnp_segment(segment):
                bucket["GNP"] += minutes
                has_gnp = True
            elif _is_snp_segment(segment):
                bucket["SNP"] += minutes
                has_snp = True
        if has_gnp:
            bucket["gnp_tower_day_count"] += 1
        if has_snp:
            bucket["snp_tower_day_count"] += 1

    rows = []
    for attr in (tower_reason_attribution.get("by_tower_reason") or []):
        reason = _clean_text(attr.get("reason")).casefold()
        if "web break" not in reason:
            continue
        tower = _clean_text(attr.get("tower"))
        tower_norm = normalized(tower)
        if not any(all(part in tower_norm for part in parts) for parts in target_aliases.values()):
            continue
        event_count = int(attr.get("attributed_event_count") or 0)
        total_minutes = _number(attr.get("attributed_minutes"))
        runtime_bucket = product_runtime_by_tower.get(tower, {})
        gnp_runtime = _number(runtime_bucket.get("GNP"))
        snp_runtime = _number(runtime_bucket.get("SNP"))
        rows.append({
            "tower": tower,
            "reason": attr.get("reason"),
            "attributed_event_count": event_count,
            "attributed_minutes": _clean_number(total_minutes),
            "avg_minutes_per_event": _clean_number(total_minutes / event_count) if event_count else 0,
            "gnp_runtime_min": _clean_number(gnp_runtime),
            "snp_runtime_min": _clean_number(snp_runtime),
            "gnp_tower_day_count": int(runtime_bucket.get("gnp_tower_day_count") or 0),
            "snp_tower_day_count": int(runtime_bucket.get("snp_tower_day_count") or 0),
            "can_split_web_break_by_product_type": False,
            "matching_note": (
                "Web break reason rows are attributed to the tower at plant/machine/folder level. "
                "They can be compared with the tower's GNP/SNP runtime mix, but the event itself is not stored with product type."
            ),
        })
    rows.sort(key=lambda row: row.get("tower", ""))
    return rows


def _gnp_snp_delay_correlation_summary(comparison_rows: list[dict[str, Any]]) -> dict[str, Any]:
    by_type = {row.get("product_type"): row for row in comparison_rows}
    gnp = by_type.get("GNP") or {}
    snp = by_type.get("SNP") or {}
    if not gnp or not snp:
        return {}
    return {
        "metric": "delayed folder-day rate and average overrun by product type",
        "gnp_delayed_folder_day_pct": gnp.get("delayed_folder_day_pct"),
        "snp_delayed_folder_day_pct": snp.get("delayed_folder_day_pct"),
        "gnp_avg_overrun_min": gnp.get("avg_overrun_min"),
        "snp_avg_overrun_min": snp.get("avg_overrun_min"),
        "interpretation_rule": "Use this as an association/correlation signal, not proof of root cause.",
    }


def _gnp_night_lookup(folder_rows: list[dict[str, Any]]) -> dict[str, bool]:
    """Map run_date -> True if that night had at least one GNP/GNP Complex edition (a GNP/UV night).

    Factored out of _build_gnp_night_classification so other per-row tables (like delayed_pf) can
    attach night_type directly to each row without needing a separate join against a nights table.
    """
    rows_by_date: dict[str, list[dict[str, Any]]] = {}
    for row in folder_rows:
        run_date = _clean_text(row.get("run_date"))
        if run_date:
            rows_by_date.setdefault(run_date, []).append(row)
    return {
        run_date: any(_is_gnp_segment(segment) for segment in _runtime_segments_for_rows(date_rows))
        for run_date, date_rows in rows_by_date.items()
    }


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
    gnp_night_lookup = _gnp_night_lookup(folder_rows)
    rows = []
    for row in folder_rows:
        overrun = _number(row.get("overrun_minutes"))
        if overrun <= 0:
            continue
        runtime_segments = _runtime_segment_rows(row)
        product_flags = _folder_product_flags(runtime_segments)
        plant = _clean_text(row.get("plant_name"))
        cutoff_minutes = _pf_compliance_minutes(plant)
        machine, folder_name = _split_machine_folder(_clean_text(row.get("folder")))
        run_date = _clean_text(row.get("run_date"))
        is_gnp_night = gnp_night_lookup.get(run_date, False)
        rows.append({
            "run_date": run_date,
            # Attached directly so GNP-vs-SNP-night questions don't need a join against the
            # separate night-classification table — that join isn't something the deterministic
            # plan-answering path can actually perform across two source tables.
            "night_type": "GNP/UV" if is_gnp_night else "SNP/non-UV",
            "gnp_night": is_gnp_night,
            **product_flags,
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
            "weekday": _weekday_label(row.get("run_date")),
            "month": _month_label(row.get("run_date")),
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

    # matching_dates/folders/editions are display-only lists — no consumer ever shows more than a
    # handful, so cap them at the source instead of letting them grow unbounded per row.
    by_tower_reason_rows = [
        {
            "tower": entry["tower"],
            "reason": entry["reason"],
            "attributed_event_count": entry["attributed_event_count"],
            "attributed_minutes": _clean_number(entry["attributed_minutes"]),
            "matching_tower_run_count": entry["matching_tower_run_count"],
            "matching_dates": sorted(entry["matching_dates"])[-8:],
            "folders": sorted(entry["folders"])[:10],
            "editions": sorted(entry["editions"])[:10],
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
            "reasons": sorted(entry["reasons"])[:10],
            "matching_dates": sorted(entry["matching_dates"])[-8:],
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


def _build_tower_usage_distribution(availability: dict[str, Any]) -> list[dict[str, Any]]:
    buckets: dict[int, dict[str, Any]] = {}
    for row in availability.get("active_towers_by_day") or []:
        towers_used = int(_number(row.get("active_towers")))
        bucket = buckets.setdefault(towers_used, {
            "towers_used": towers_used,
            "day_count": 0,
            "dates": [],
        })
        bucket["day_count"] += 1
        run_date = _clean_text(row.get("run_date"))
        if run_date:
            bucket["dates"].append(run_date)

    return [
        {
            "towers_used": value["towers_used"],
            "day_count": value["day_count"],
            "dates": value["dates"],
        }
        for value in sorted(buckets.values(), key=lambda item: item["towers_used"])
    ]


def _build_tower_runtime_mix(tower_day_rows: list[dict[str, Any]]) -> list[dict[str, Any]]:
    buckets: dict[tuple[str, str], dict[str, Any]] = {}

    for row in tower_day_rows or []:
        tower_type_key = "gnp_uv" if row.get("uv_tower") else "non_uv"
        tower_type = "GNP/UV tower" if row.get("uv_tower") else "Non-GNP/non-UV tower"
        tower = _clean_text(row.get("tower"))
        run_date = _clean_text(row.get("run_date"))
        tower_day_key = f"{run_date}||{tower}" if run_date and tower else ""

        for segment in row.get("runtime_segments") or []:
            minutes = _number(segment.get("minutes"))
            if minutes <= 0:
                continue
            if _is_snp_segment(segment):
                product_type = "SNP"
            elif _is_gnp_segment(segment):
                product_type = "GNP"
            else:
                product_type = "Unknown"

            for bucket_product_type in (product_type, "All"):
                bucket = buckets.setdefault(
                    (tower_type_key, bucket_product_type),
                    {
                        "tower_type_key": tower_type_key,
                        "tower_type": tower_type,
                        "product_type": bucket_product_type,
                        "runtime_min": 0.0,
                        "tower_days": set(),
                        "towers": set(),
                    },
                )
                bucket["runtime_min"] += minutes
                if tower_day_key:
                    bucket["tower_days"].add(tower_day_key)
                if tower:
                    bucket["towers"].add(tower)

    totals_by_tower_type = {
        tower_type_key: bucket["runtime_min"]
        for (tower_type_key, product_type), bucket in buckets.items()
        if product_type == "All"
    }

    rows = []
    product_sort = {"All": 0, "SNP": 1, "GNP": 2, "Unknown": 3}
    for bucket in buckets.values():
        denominator = totals_by_tower_type.get(bucket["tower_type_key"], 0.0)
        rows.append({
            "tower_type_key": bucket["tower_type_key"],
            "tower_type": bucket["tower_type"],
            "product_type": bucket["product_type"],
            "runtime_min": _clean_number(bucket["runtime_min"]),
            "share_of_tower_type_runtime_pct": _percentage(bucket["runtime_min"], denominator),
            "tower_day_count": len(bucket["tower_days"]),
            "tower_count": len(bucket["towers"]),
            "towers": sorted(bucket["towers"]),
        })

    return sorted(
        rows,
        key=lambda row: (
            0 if row["tower_type_key"] == "gnp_uv" else 1,
            product_sort.get(row["product_type"], 9),
        ),
    )


def _largest_delayed_pf_components(row: dict[str, Any]) -> list[dict[str, Any]]:
    components = [
        ("runtime", "Run Time", _number(row.get("runtime"))),
        ("loss_time", "Lost Time", _loss_time_minutes(row)),
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
        non_wait_lost_time = sum(_loss_time_minutes(row) for row in rows)
        downtime = sum(_number(row.get("downtime")) for row in rows)
        overrun_total = sum(_number(row.get("overrun_minutes")) for row in rows)
        buffer_time = sum(_number(row.get("buffer_time")) for row in rows)
        if buffer_time <= 0 and rows:
            buffer_time = max(active_capacity - runtime - waiting_time - lost_time_raw - downtime, 0)
        active_days = len({row.get("run_date") for row in rows if row.get("run_date")})
        unplanned_days = max(production_days - active_days, 0)
        unplanned_time = max(possible_capacity - active_capacity, 0)
        daily_utilization_percentages = _daily_folder_utilization_percentages(rows, dates)
        variability = pstdev(daily_utilization_percentages) if len(daily_utilization_percentages) > 1 else 0.0
        utilization = _utilization_pct(runtime, overrun_total, possible_capacity, waiting_time, non_wait_lost_time, downtime)
        active_day_utilization = _utilization_pct(runtime, overrun_total, active_capacity, waiting_time, non_wait_lost_time, downtime)
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
                "Treat spare time and unplanned time as entirely separate facts: spare (buffer_time) is leftover capacity on active nights; unplanned is capacity on nights with no scheduled activity. NEVER combine them. Waiting time is separate from lost time — do not include waiting in lost time figures. "
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
        f"and lost time is {summary.get('total_loss_time_minutes', 0)} minutes."
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


def _daily_folder_utilization_percentages(rows: list[dict[str, Any]], dates: list[str]) -> list[float]:
    rows_by_day = {
        _clean_text(row.get("run_date")): row
        for row in rows
        if row.get("run_date")
    }
    percentages = []
    for day in dates:
        row = rows_by_day.get(day, {})
        percentages.append(
            _utilization_pct(
                _number(row.get("runtime")),
                _number(row.get("overrun_minutes")),
                CAPACITY_MINUTES_PER_FOLDER_DAY,
                _number(row.get("waiting_time")),
                _loss_time_minutes(row),
                _number(row.get("downtime")),
            )
        )
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


def _is_snp_segment(segment: dict[str, Any]) -> bool:
    code_number = re_fullmatch_complexity(segment.get("complexity_code"))
    if code_number and 1 <= int(code_number) <= 4:
        return True

    text = " ".join(
        [
            _clean_text(segment.get("type")),
            _clean_text(segment.get("category")),
            _clean_text(segment.get("label")),
            _clean_text(segment.get("key")),
        ]
    ).casefold()
    return "snp" in text and "gnp" not in text


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


def _utilization_pct(runtime: float, overrun: float, available: float, waiting: float, loss: float, downtime: float) -> float:
    """Capacity Utilization % = (Runtime + Overrun + Lost Time + Wait Time + Downtime) / Available Time * 100, folder-level only."""
    utilized_time = float(runtime) + float(overrun) + float(loss) + float(waiting) + float(downtime)
    return _percentage(utilized_time, available)


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
    base: dict[str, Any] = {"messages": messages}
    if model:
        base["model"] = model

    # temperature=0 (greedy decoding) attempted first so the same question always produces the
    # same plan — the QU decomposer's output drives which real numbers get computed downstream, so
    # sampling variance here was a direct cause of different answers to the same question. The
    # no-temperature payloads are kept as a fallback only, for any deployment that rejects an
    # explicit temperature (400) rather than ignoring it.
    payloads = [
        {**base, "temperature": 0, "max_completion_tokens": 1800, "response_format": {"type": "json_object"}},
        {**base, "temperature": 0, "max_completion_tokens": 1800},
        {**base, "max_completion_tokens": 1800, "response_format": {"type": "json_object"}},
        {**base, "max_completion_tokens": 1800},
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
                except TimeoutError as exc:
                    last_error = exc
                    break
                except URLError as exc:
                    last_error = exc
                    break

    if last_error:
        raise last_error
    raise RuntimeError("LLM request failed.")


async def _call_chat_completion_async(
    endpoint: str,
    api_key: str,
    messages: list[dict[str, str]],
    cancellation_event: asyncio.Event | None = None,
) -> str:
    url = _build_chat_completion_url(endpoint)
    urls = [url]
    if not _get_env("AZURE_API_VERSION") and "api-version=" in url:
        urls.append(_without_api_version(url))

    model = _get_env("AZURE_MODEL") or _get_env("AZURE_OPENAI_MODEL") or _get_env("AZURE_DEPLOYMENT")
    base: dict[str, Any] = {"messages": messages}
    if model:
        base["model"] = model

    # temperature=0 (greedy decoding) attempted first so the same question always produces the
    # same plan — the QU decomposer's output drives which real numbers get computed downstream, so
    # sampling variance here was a direct cause of different answers to the same question. The
    # no-temperature payloads are kept as a fallback only, for any deployment that rejects an
    # explicit temperature (400) rather than ignoring it.
    payloads = [
        {**base, "temperature": 0, "max_completion_tokens": 1800, "response_format": {"type": "json_object"}},
        {**base, "temperature": 0, "max_completion_tokens": 1800},
        {**base, "max_completion_tokens": 1800, "response_format": {"type": "json_object"}},
        {**base, "max_completion_tokens": 1800},
    ]
    auth_modes = ["api-key", "bearer"]
    last_error: Exception | None = None

    for request_url in urls:
        for payload in payloads:
            for auth_mode in auth_modes:
                try:
                    _raise_if_chat_cancelled(cancellation_event)
                    response = await _post_json_async(request_url, payload, api_key, auth_mode)
                    text = _extract_llm_text(response)
                    if text:
                        return text
                    raise RuntimeError("LLM response did not contain text content.")
                except httpx.HTTPStatusError as exc:
                    last_error = exc
                    if exc.response.status_code in {400, 401, 403, 404}:
                        continue
                    raise
                except httpx.TimeoutException as exc:
                    last_error = exc
                    break
                except httpx.RequestError as exc:
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
    response_budget = (
        CHAT_REASONING_RESPONSE_MAX_TOKENS
        if reasoning_effort and reasoning_effort != "none"
        else CHAT_RESPONSE_MAX_TOKENS
    )

    if _should_try_responses_api(endpoint, model):
        response_url = _build_responses_url(endpoint)
        response_urls = [response_url]
        if not _get_env("AZURE_API_VERSION") and "api-version=" in response_url:
            response_urls.append(_without_api_version(response_url))

        for request_url in response_urls:
            for payload in _responses_payloads(messages, model, reasoning_effort, max_output_tokens=response_budget):
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
                    except TimeoutError as exc:
                        last_error = exc
                        break
                    except URLError as exc:
                        last_error = exc
                        break

    chat_url = _build_chat_completion_url(endpoint)
    chat_urls = [chat_url]
    if not _get_env("AZURE_API_VERSION") and "api-version=" in chat_url:
        chat_urls.append(_without_api_version(chat_url))

    for request_url in chat_urls:
        for payload in _chat_completion_payloads(messages, model, reasoning_effort, max_tokens=response_budget):
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
                except TimeoutError as exc:
                    last_error = exc
                    break
                except URLError as exc:
                    last_error = exc
                    break

    if last_error:
        raise last_error
    raise RuntimeError("LLM request failed.")


async def _call_plain_chat_completion_async(
    endpoint: str,
    api_key: str,
    messages: list[dict[str, str]],
    cancellation_event: asyncio.Event | None = None,
) -> str:
    """Cancellable chat-only variant of _call_plain_chat_completion."""
    model = _get_env("AZURE_MODEL") or _get_env("AZURE_OPENAI_MODEL") or _get_env("AZURE_DEPLOYMENT")
    reasoning_effort = _select_reasoning_effort(messages, model)

    auth_modes = ["api-key", "bearer"]
    last_error: Exception | None = None
    response_budget = (
        CHAT_REASONING_RESPONSE_MAX_TOKENS
        if reasoning_effort and reasoning_effort != "none"
        else CHAT_RESPONSE_MAX_TOKENS
    )

    if _should_try_responses_api(endpoint, model):
        response_url = _build_responses_url(endpoint)
        response_urls = [response_url]
        if not _get_env("AZURE_API_VERSION") and "api-version=" in response_url:
            response_urls.append(_without_api_version(response_url))

        for request_url in response_urls:
            for payload in _responses_payloads(messages, model, reasoning_effort, max_output_tokens=response_budget):
                for auth_mode in auth_modes:
                    try:
                        _raise_if_chat_cancelled(cancellation_event)
                        response = await _post_json_async(request_url, payload, api_key, auth_mode)
                        text = _extract_llm_text(response)
                        if text:
                            return text
                        raise RuntimeError("LLM response did not contain text content.")
                    except httpx.HTTPStatusError as exc:
                        last_error = exc
                        if exc.response.status_code in {400, 401, 403, 404}:
                            continue
                        raise
                    except httpx.TimeoutException as exc:
                        last_error = exc
                        break
                    except httpx.RequestError as exc:
                        last_error = exc
                        break

    chat_url = _build_chat_completion_url(endpoint)
    chat_urls = [chat_url]
    if not _get_env("AZURE_API_VERSION") and "api-version=" in chat_url:
        chat_urls.append(_without_api_version(chat_url))

    for request_url in chat_urls:
        for payload in _chat_completion_payloads(messages, model, reasoning_effort, max_tokens=response_budget):
            for auth_mode in auth_modes:
                try:
                    _raise_if_chat_cancelled(cancellation_event)
                    response = await _post_json_async(request_url, payload, api_key, auth_mode)
                    text = _extract_llm_text(response)
                    if text:
                        return text
                    raise RuntimeError("LLM response did not contain text content.")
                except httpx.HTTPStatusError as exc:
                    last_error = exc
                    if exc.response.status_code in {400, 401, 403, 404}:
                        continue
                    raise
                except httpx.TimeoutException as exc:
                    last_error = exc
                    break
                except httpx.RequestError as exc:
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
    return "/responses" in url


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
    }
    if instructions:
        base_payload["instructions"] = instructions
    if model:
        base_payload["model"] = model

    payloads: list[dict[str, Any]] = []
    low_verbosity_payload = {**base_payload, "text": {"verbosity": "low"}}
    if reasoning_effort and reasoning_effort != "none":
        _append_unique_payload(payloads, {
            **low_verbosity_payload,
            "reasoning": {"effort": reasoning_effort},
        })
        _append_unique_payload(payloads, {
            **base_payload,
            "reasoning": {"effort": reasoning_effort},
        })
    else:
        # Greedy decoding (temperature=0) attempted first so repeated identical questions get
        # repeatable numeric answers. Reasoning-effort payloads above never get a temperature —
        # reasoning-family models generally reject it — so this branch only fires for the
        # non-reasoning / effort="none" case. The original default-temperature payloads remain as
        # a fallback for any deployment that 400s on an explicit temperature.
        _append_unique_payload(payloads, {**low_verbosity_payload, "temperature": 0})
        _append_unique_payload(payloads, {**base_payload, "temperature": 0})
    _append_unique_payload(payloads, low_verbosity_payload)
    _append_unique_payload(payloads, base_payload)
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

    payloads: list[dict[str, Any]] = []
    if _is_reasoning_model(model) and reasoning_effort and reasoning_effort != "none":
        _append_unique_payload(payloads, {
            **base_payload,
            "reasoning_effort": reasoning_effort,
            "max_completion_tokens": max_tokens,
        })

    # Greedy decoding (temperature=0) attempted first for reproducible numeric answers; the
    # no-temperature payload remains as a fallback for deployments that reject an explicit
    # temperature. Newer GPT/o-series deployments reject max_tokens, so use max_completion_tokens
    # consistently.
    _append_unique_payload(payloads, {**base_payload, "temperature": 0, "max_completion_tokens": max_tokens})
    _append_unique_payload(payloads, {**base_payload, "max_completion_tokens": max_tokens})
    return payloads


def _append_unique_payload(payloads: list[dict[str, Any]], payload: dict[str, Any]) -> None:
    if payload not in payloads:
        payloads.append(payload)


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


def _should_use_chat_planner(message: str) -> bool:
    configured = _get_env("CAPACITY_CHAT_USE_PLANNER")
    if configured:
        return configured.strip().casefold() not in {"0", "false", "no", "off"}
    # Always attempt the planner (a cheap, deterministic JSON-mode call) before falling back to a
    # single free-text LLM call, instead of gating the attempt on a keyword heuristic. Keyword
    # gating was the actual root cause of wrong counts on phrasing we hadn't anticipated — a typo
    # ("how may days" instead of "how many"), or an implicit comparator ("did we have downtime"
    # meaning >0 with no number stated at all) — every such miss skipped the planner entirely and
    # went straight to the LLM, which is exactly where the miscounted answers came from.
    # _answer_from_plan returns "" whenever it can't compute a real answer from real fields, so
    # this can never produce a wrong answer — worst case it's one wasted round trip before the
    # existing fallback path runs, same as today.
    return bool(_clean_text(message))


def _select_reasoning_effort(messages: list[dict[str, str]], model: str) -> str:
    configured = _get_env("CAPACITY_CHAT_REASONING_EFFORT") or _get_env("OPENAI_REASONING_EFFORT")
    if configured:
        return _normalize_reasoning_effort(configured, model)
    if not _is_reasoning_model(model):
        return ""

    latest_user = _latest_user_message(messages)

    if _question_needs_reasoning(latest_user):
        return "medium"
    return "none"


def _latest_user_message(messages: list[dict[str, str]]) -> str:
    for message in reversed(messages):
        if message.get("role") == "user":
            return _clean_text(message.get("content"))
    return ""


def _question_needs_reasoning(message: str) -> bool:
    text = _clean_text(message).casefold()
    if not text:
        return False

    reasoning_terms = [
        "trend", "interpolate", "interpolation", "forecast", "predict", "projection",
        "why", "reason", "root cause", "cause", "correlation", "relationship",
        "compare", "comparison", "difference", "variance", "pattern", "identify",
        "best", "worst", "optimize", "recommend", "should", "impact",
        "explain", "derive", "estimate", "analyze", "analyse",
    ]
    return any(term in text for term in reasoning_terms)


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


async def _post_json_async(url: str, payload: dict[str, Any], api_key: str, auth_mode: str) -> dict[str, Any]:
    headers = {
        "Content-Type": "application/json",
        "Accept": "application/json",
    }
    if auth_mode == "bearer":
        headers["Authorization"] = f"Bearer {api_key}"
    else:
        headers["api-key"] = api_key

    data = json.dumps(payload, ensure_ascii=True).encode("utf-8")
    try:
        async with httpx.AsyncClient(timeout=REQUEST_TIMEOUT_SECONDS) as client:
            response = await client.post(url, content=data, headers=headers)
            response.raise_for_status()
            return response.json()
    except httpx.TimeoutException:
        raise
    except httpx.TransportError as exc:
        message = str(exc).casefold()
        if "certificate_verify_failed" not in message and "certificate verify failed" not in message:
            raise

        async with httpx.AsyncClient(timeout=REQUEST_TIMEOUT_SECONDS, verify=False) as client:
            response = await client.post(url, content=data, headers=headers)
            response.raise_for_status()
            return response.json()


def _is_ssl_certificate_verification_error(exc: URLError) -> bool:
    reason = getattr(exc, "reason", exc)
    if isinstance(reason, ssl.SSLCertVerificationError):
        return True

    message = str(reason).lower()
    return "certificate_verify_failed" in message or "certificate verify failed" in message


def _is_timeout_error(exc: BaseException) -> bool:
    if isinstance(exc, (TimeoutError, httpx.TimeoutException)):
        return True

    reason = getattr(exc, "reason", None)
    if isinstance(reason, TimeoutError):
        return True

    message = str(reason or exc).casefold()
    return "timed out" in message or "timeout" in message


def _chat_error_kind(exc: BaseException) -> str:
    if _is_timeout_error(exc):
        return "timeout"
    if isinstance(exc, HTTPError):
        return f"http_{exc.code}"
    if isinstance(exc, URLError):
        return "network"
    return exc.__class__.__name__


def _chat_debug_enabled() -> bool:
    return (_get_env("CAPACITY_CHAT_DEBUG") or "").strip().casefold() in {"1", "true", "yes", "on"}


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
