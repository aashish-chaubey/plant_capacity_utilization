from __future__ import annotations

from datetime import date, datetime, time, timedelta
from math import isfinite
from numbers import Real
import re
import warnings
from typing import Any

import pandas as pd


warnings.filterwarnings(
    "ignore",
    message="Workbook contains no default style, apply openpyxl's default",
    category=UserWarning,
    module="openpyxl.styles.stylesheet",
)

CAPACITY_MINUTES_PER_FOLDER_DAY = 240.0

GENERAL_SHEET = "General"
BOOK_WISE_SHEET = "Book Wise Details"
DOWN_TIME_SHEET = "Down Time"
REPORT_DATE_COLUMN = "Report Date"


def parse_general(workbook: pd.ExcelFile) -> pd.DataFrame:
    """Read issue-level tower assignments from the General sheet when present."""
    try:
        df = _read_sheet(workbook, GENERAL_SHEET)
    except ValueError:
        return pd.DataFrame(columns=["IssueID", "Towers used"])

    df["IssueID"] = df["IssueID"].apply(_clean_text)
    df["Towers used"] = df["Towers used"].apply(_clean_text)
    return df


def parse_book_wise_details(workbook: pd.ExcelFile) -> pd.DataFrame:
    """Read and normalize Book Wise Details rows needed for capacity calculations.

    Report Date is derived only from Issue Date.
    Run Date is parsed but not used for capacity/date grouping.
    """
    df = _read_sheet(workbook, BOOK_WISE_SHEET)

    # Parse all date and time columns FIRST to ensure no NaT values later
    df["Issue Date"] = df["Issue Date"].apply(_parse_date_value)
    df["Run Date"] = df["Run Date"].apply(_parse_date_value)
    df["Start Date"] = df["Start Date"].apply(_parse_date_value)
    df["End Date"] = df["End Date"].apply(_parse_date_value)
    df["Start Time"] = df["Start Time"].apply(_parse_time_value)
    df["End Time"] = df["End Time"].apply(_parse_time_value)

    # All reporting/grouping is based only on Issue Date.
    df[REPORT_DATE_COLUMN] = df["Issue Date"]

    df["Machine"] = df["Machine"].apply(_clean_text)
    df["Folder"] = df["Folder"].apply(_clean_text)
    df["Issue Id"] = df["Issue Id"].apply(_clean_text)
    df["Reflong"] = df["Reflong"].apply(_clean_text)
    df["Complexities"] = df.get(
        "Complexities",
        pd.Series("", index=df.index),
    ).apply(_clean_text)
    df["Print Order"] = df.get(
        "Print Order",
        pd.Series(0, index=df.index),
    ).apply(_parse_count_value)
    df["Total Run Time (mnts)"] = df["Total Run Time (mnts)"].apply(_parse_minutes_value)
    df["Total Downtime"] = df["Total Downtime"].apply(_parse_minutes_value)
    df["Change Over Time (mins)"] = df.get(
        "Change Over Time (mins)",
        pd.Series(0, index=df.index),
    ).apply(_parse_minutes_value)

    # Parse Last Tiff timestamp
    df["Last Tiff DateTime"] = df.get("Last Tiff", pd.Series()).apply(_parse_datetime_value)

    # Fallback date must be Issue Date, not Run Date.
    df["Start DateTime"] = df.apply(
        lambda row: _combine_date_time(row["Start Date"], row["Start Time"], row["Issue Date"]),
        axis=1,
    )
    df["End DateTime"] = df.apply(
        lambda row: _combine_date_time(row["End Date"], row["End Time"], row["Issue Date"]),
        axis=1,
    )

    return df


def parse_down_time(workbook: pd.ExcelFile) -> pd.DataFrame:
    """Read and normalize Down Time rows needed for downtime/reflong classification."""
    df = _read_sheet(workbook, DOWN_TIME_SHEET)
    df["Run Date"] = df["Run Date"].apply(_parse_date_value)
    df["Machine"] = df["Machine"].apply(_clean_text)
    df["Folder"] = df["Folder"].apply(_clean_text)
    df["IssueID"] = df["IssueID"].apply(_clean_text)
    df["Related"] = df["Related"].apply(_clean_text)
    df["Reason"] = df["Reason"].apply(_clean_text)
    df["Total Downtime"] = df["Total Downtime"].apply(_parse_minutes_value)
    return df


def calculate_folder_day_metrics(book_df: pd.DataFrame, down_time_df: pd.DataFrame) -> pd.DataFrame:
    """Calculate one 240-minute capacity unit for each Issue Date + Machine + Folder.

    Strict first step:
    - Keep only Book Wise Details rows that overlap the Issue Date 00:00-04:00 window.
    - Discard all other print jobs before deriving active folders.
    - All downstream calculations use only this interval-filtered data.
    """
    keys = [REPORT_DATE_COLUMN, "Machine", "Folder"]

    # Step 1: basic usable rows.
    active_rows = book_df[_has_capacity_keys(book_df)].copy()

    if active_rows.empty:
        return _empty_folder_day_metrics()

    # Step 2: strict interval filter FIRST.
    # Only rows overlapping Issue Date 00:00-04:00 survive.
    interval_editions = _filter_interval_editions(active_rows)

    if interval_editions.empty:
        return _empty_folder_day_metrics()

    # Step 3: active folders are derived only from interval rows.
    # This prevents folders/jobs outside 00:00-04:00 from appearing.
    active_units = (
        interval_editions[keys]
        .drop_duplicates()
        .sort_values(keys)
        .reset_index(drop=True)
    )

    # Step 4: runtime/lost/buffer calculations use only interval rows.
    calculated_metrics = _calculate_interval_metrics_by_folder_day(interval_editions)
    runtime_segments = _calculate_runtime_segments_by_folder_day(interval_editions)

    # Step 5: downtime is also limited to issues present in interval rows.
    down_grouped = _aggregate_down_time_by_capacity_unit_filtered(
        book_df,
        down_time_df,
        interval_editions,
    )

    metrics = (
        active_units.merge(calculated_metrics, on=keys, how="left")
        .merge(runtime_segments, on=keys, how="left")
        .merge(down_grouped, on=keys, how="left")
    )

    numeric_columns = [
        "gross_runtime",
        "runtime",
        "late_start_time",
        "waiting_time",
        "change_over_time",
        "natural_buffer_time",
        "total_downtime",
        "reflong_related_downtime",
    ]

    for column in numeric_columns:
        if column not in metrics:
            metrics[column] = 0.0
        metrics[column] = pd.to_numeric(metrics[column], errors="coerce").fillna(0.0)

    metrics["available_capacity"] = CAPACITY_MINUTES_PER_FOLDER_DAY

    # Normal downtime excludes reflong-related downtime.
    metrics["downtime"] = (
        metrics["total_downtime"] - metrics["reflong_related_downtime"]
    ).clip(lower=0)

    # Net runtime after subtracting all downtime.
    metrics["runtime"] = (
        metrics["gross_runtime"] - metrics["total_downtime"]
    ).clip(lower=0)
    metrics["runtime_segments"] = metrics.apply(
        lambda row: _scale_runtime_segments(row.get("runtime_segments"), row["runtime"]),
        axis=1,
    )

    calculated_lost_time = (
        metrics["waiting_time"]
        + metrics["late_start_time"]
        + metrics["change_over_time"]
        + metrics["reflong_related_downtime"]
    ).clip(lower=0)
    remaining_capacity = (
        metrics["available_capacity"] - metrics["runtime"] - metrics["downtime"]
    ).clip(lower=0)
    metrics["lost_time"] = calculated_lost_time.clip(upper=remaining_capacity)

    # Strict railguard:
    # runtime + downtime + lost_time + buffer_time must equal 240.
    fixed_used_minutes = (
        metrics["runtime"]
        + metrics["downtime"]
        + metrics["lost_time"]
    )

    metrics["buffer_time"] = (
        metrics["available_capacity"] - fixed_used_minutes
    ).clip(lower=0, upper=CAPACITY_MINUTES_PER_FOLDER_DAY)

    return metrics[
        [
            REPORT_DATE_COLUMN,
            "Machine",
            "Folder",
            "available_capacity",
            "gross_runtime",
            "runtime",
            "lost_time",
            "downtime",
            "buffer_time",
            "change_over_time",
            "waiting_time",
            "reflong_related_downtime",
            "late_start_time",
            "runtime_segments",
        ]
    ]


def _merge_print_intervals(
    intervals: list[tuple[pd.Timestamp, pd.Timestamp]]
) -> list[tuple[pd.Timestamp, pd.Timestamp]]:
    """Merge overlapping or touching print intervals.

    Examples:
    - 00:10-00:30 and 00:10-00:30 becomes 00:10-00:30.
    - 00:19-00:58 and 00:19-01:02 becomes 00:19-01:02.
    - 00:10-00:30 and 00:30-00:45 becomes 00:10-00:45.
    """
    valid_intervals = [
        (pd.Timestamp(start), pd.Timestamp(end))
        for start, end in intervals
        if pd.notna(start) and pd.notna(end) and pd.Timestamp(end) > pd.Timestamp(start)
    ]

    if not valid_intervals:
        return []

    valid_intervals.sort(key=lambda item: item[0])

    merged: list[tuple[pd.Timestamp, pd.Timestamp]] = []

    for start, end in valid_intervals:
        if not merged:
            merged.append((start, end))
            continue

        previous_start, previous_end = merged[-1]

        if start <= previous_end:
            merged[-1] = (previous_start, max(previous_end, end))
        else:
            merged.append((start, end))

    return merged


def _filter_interval_editions(book_df: pd.DataFrame) -> pd.DataFrame:
    """Keep only editions that overlap the Issue Date 00:00-04:00 window.

    A row is included only if it overlaps the Issue Date window.

    Examples:
    - 03:45 to 05:30 contributes only 15 minutes.
    - 23:50 previous day to 00:20 Issue Date contributes 20 minutes.
    - 04:00 to 04:30 contributes 0 minutes and is excluded.
    """
    df = book_df[
        (book_df[REPORT_DATE_COLUMN].notna()) &
        (book_df["Issue Date"].notna()) &
        (book_df["Machine"].ne("")) &
        (book_df["Folder"].ne("")) &
        (book_df["Start DateTime"].notna()) &
        (book_df["End DateTime"].notna())
    ].copy()

    interval_columns = [
        "Window Start",
        "Window End",
        "Effective Start DateTime",
        "Effective End DateTime",
    ]

    if df.empty:
        return pd.DataFrame(columns=[*df.columns, *interval_columns])

    kept_rows = []

    for _, row in df.iterrows():
        issue_date = row["Issue Date"]
        start_dt = pd.Timestamp(row["Start DateTime"])
        end_dt = pd.Timestamp(row["End DateTime"])

        if issue_date is None or pd.isna(start_dt) or pd.isna(end_dt):
            continue

        if end_dt <= start_dt:
            continue

        window_start = pd.Timestamp(datetime.combine(issue_date, time(0, 0)))
        window_end = pd.Timestamp(datetime.combine(issue_date, time(4, 0)))

        effective_start = max(start_dt, window_start)
        effective_end = min(end_dt, window_end)

        if effective_start < effective_end:
            row = row.copy()
            row["Window Start"] = window_start
            row["Window End"] = window_end
            row["Effective Start DateTime"] = effective_start
            row["Effective End DateTime"] = effective_end
            kept_rows.append(row)

    if not kept_rows:
        return pd.DataFrame(columns=[*df.columns, *interval_columns])

    return pd.DataFrame(kept_rows)


def _aggregate_down_time_by_capacity_unit_filtered(
    book_df: pd.DataFrame,
    down_time_df: pd.DataFrame,
    interval_editions: pd.DataFrame,
) -> pd.DataFrame:
    """Aggregate downtime only for editions in the Issue Date 00:00-04:00 interval.

    Down Time does not have Issue Date, so Report Date is mapped from Book Wise Details
    using IssueID + Machine + Folder.

    Reflong is classified only for the exact matched interval edition group.
    """
    keys = [REPORT_DATE_COLUMN, "Machine", "Folder"]

    down_rows = down_time_df[
        (down_time_df["IssueID"].ne("")) &
        (down_time_df["Machine"].ne("")) &
        (down_time_df["Folder"].ne(""))
    ].copy()

    if down_rows.empty or interval_editions.empty:
        return pd.DataFrame(columns=[*keys, "total_downtime", "reflong_related_downtime"])

    interval_lookup = (
        interval_editions[
            interval_editions["Issue Id"].ne("")
            & interval_editions[REPORT_DATE_COLUMN].notna()
            & interval_editions["Machine"].ne("")
            & interval_editions["Folder"].ne("")
        ]
        .assign(
            lookup_key=lambda df: list(
                zip(
                    df["Issue Id"],
                    df["Machine"],
                    df["Folder"],
                )
            ),
            reflong_yes=lambda df: (
                df["Reflong"]
                .fillna("")
                .astype(str)
                .str.strip()
                .str.casefold()
                .eq("yes")
            ),
        )
        .groupby("lookup_key", dropna=False)
        .agg(
            report_date=(REPORT_DATE_COLUMN, "first"),
            reflong_yes=("reflong_yes", "any"),
        )
        .to_dict(orient="index")
    )

    down_rows["lookup_key"] = list(
        zip(
            down_rows["IssueID"],
            down_rows["Machine"],
            down_rows["Folder"],
        )
    )

    down_rows = down_rows[down_rows["lookup_key"].isin(interval_lookup.keys())].copy()

    if down_rows.empty:
        return pd.DataFrame(columns=[*keys, "total_downtime", "reflong_related_downtime"])

    down_rows[REPORT_DATE_COLUMN] = down_rows["lookup_key"].map(
        lambda key: interval_lookup.get(key, {}).get("report_date")
    )

    down_rows["book_reflong_yes"] = down_rows["lookup_key"].map(
        lambda key: interval_lookup.get(key, {}).get("reflong_yes", False)
    )

    down_rows = down_rows[down_rows[REPORT_DATE_COLUMN].notna()].copy()

    if down_rows.empty:
        return pd.DataFrame(columns=[*keys, "total_downtime", "reflong_related_downtime"])

    down_rows["related_starts_reflong"] = (
        down_rows["Related"]
        .fillna("")
        .astype(str)
        .str.strip()
        .str.casefold()
        .str.startswith("reflong")
    )

    down_rows["reflong_related_downtime"] = down_rows["Total Downtime"].where(
        down_rows["book_reflong_yes"] & down_rows["related_starts_reflong"],
        0.0,
    )

    return (
        down_rows.groupby(keys, dropna=False)
        .agg(
            total_downtime=("Total Downtime", "sum"),
            reflong_related_downtime=("reflong_related_downtime", "sum"),
        )
        .reset_index()
    )


def _aggregate_down_time_by_tower(
    down_time_df: pd.DataFrame,
    issue_tower_lookup: dict[str, list[str]],
    issue_machine_lookup: dict[str, str],
    issue_date_lookup: dict[str, date],
    issue_reflong_lookup: dict[str, bool],
) -> dict[tuple[date, str, str], dict[str, float]]:
    totals: dict[tuple[date, str, str], dict[str, float]] = {}

    if down_time_df.empty:
        return totals

    for _, row in down_time_df.iterrows():
        issue_id = _clean_text(row.get("IssueID"))
        towers = issue_tower_lookup.get(issue_id, [])
        machine = issue_machine_lookup.get(issue_id, "")
        report_date = issue_date_lookup.get(issue_id)

        if not issue_id or not towers or not machine or not report_date:
            continue

        total_downtime = _parse_minutes_value(row.get("Total Downtime"))
        if total_downtime <= 0:
            continue

        is_reflong_related = (
            issue_reflong_lookup.get(issue_id, False)
            and _clean_text(row.get("Related")).casefold().startswith("reflong")
        )

        for tower in towers:
            key = (report_date, machine, tower)
            totals.setdefault(key, {"downtime": 0.0, "reflong_related_downtime": 0.0})

            if is_reflong_related:
                totals[key]["reflong_related_downtime"] += total_downtime
            else:
                totals[key]["downtime"] += total_downtime

    return totals


def _calculate_changeover_minutes(
    intervals: list[tuple[pd.Timestamp, pd.Timestamp]]
) -> float:
    change_over_time = 0.0

    for i in range(len(intervals) - 1):
        current_end = intervals[i][1]
        next_start = intervals[i + 1][0]
        gap = (next_start - current_end).total_seconds() / 60

        if gap > 0:
            change_over_time += gap

    return change_over_time


def _calculate_interval_metrics_by_folder_day(book_df: pd.DataFrame) -> pd.DataFrame:
    """Calculate gross runtime, late start, change-over, waiting time, and natural buffer.

    Runtime is based on merged non-overlapping print intervals so concurrent jobs are
    counted only once.

    Waiting time is calculated from the Last Tiff timestamp to the print start time.
    For the first edition, waiting_time includes time from window start to Last Tiff arrival.
    For subsequent editions, waiting_time is the gap between previous edition end and Last Tiff.
    
    Change-over is the gap from Last Tiff (for subsequent editions) or from print start
    (for first edition with LPR).

    Final net runtime is calculated later after downtime is subtracted.
    """
    keys = [REPORT_DATE_COLUMN, "Machine", "Folder"]

    df = book_df.copy()

    if df.empty:
        return pd.DataFrame(
            columns=[
                *keys,
                "gross_runtime",
                "runtime",
                "late_start_time",
                "waiting_time",
                "change_over_time",
                "natural_buffer_time",
            ]
        )

    results = []

    for (report_date, machine, folder), group in df.groupby(keys, dropna=False):
        # Sort by Effective Start DateTime but preserve original index for Last Tiff mapping
        group = group.sort_values("Effective Start DateTime")
        
        window_start = group.iloc[0]["Window Start"]
        window_end = group.iloc[0]["Window End"]

        raw_intervals = list(
            zip(
                group["Effective Start DateTime"],
                group["Effective End DateTime"],
            )
        )

        merged_intervals = _merge_print_intervals(raw_intervals)

        if not merged_intervals:
            results.append(
                {
                    REPORT_DATE_COLUMN: report_date,
                    "Machine": machine,
                    "Folder": folder,
                    "gross_runtime": 0.0,
                    "runtime": 0.0,
                    "late_start_time": 0.0,
                    "waiting_time": 0.0,
                    "change_over_time": 0.0,
                    "natural_buffer_time": CAPACITY_MINUTES_PER_FOLDER_DAY,
                }
            )
            continue

        first_start = merged_intervals[0][0]
        last_end = merged_intervals[-1][1]

        # Get the correct Last Tiff times for each edition, handling Reflong logic
        last_tiff_times = _extract_last_tiff_times(group)

        # Calculate waiting time and adjust late_start_time and change_over_time
        waiting_time, late_start_minutes, adjusted_change_over = _calculate_waiting_and_timing(
            group, merged_intervals, window_start, last_tiff_times
        )

        gross_runtime = 0.0

        for start_dt, end_dt in merged_intervals:
            duration_minutes = (end_dt - start_dt).total_seconds() / 60

            if duration_minutes > 0:
                gross_runtime += duration_minutes

        natural_buffer_time = max(
            (window_end - last_end).total_seconds() / 60,
            0.0,
        )

        gross_runtime = min(max(gross_runtime, 0.0), CAPACITY_MINUTES_PER_FOLDER_DAY)
        late_start_minutes = min(max(late_start_minutes, 0.0), CAPACITY_MINUTES_PER_FOLDER_DAY)
        waiting_time = min(max(waiting_time, 0.0), CAPACITY_MINUTES_PER_FOLDER_DAY)
        adjusted_change_over = min(max(adjusted_change_over, 0.0), CAPACITY_MINUTES_PER_FOLDER_DAY)
        natural_buffer_time = min(max(natural_buffer_time, 0.0), CAPACITY_MINUTES_PER_FOLDER_DAY)

        results.append(
            {
                REPORT_DATE_COLUMN: report_date,
                "Machine": machine,
                "Folder": folder,
                "gross_runtime": gross_runtime,
                # Temporary value. Net runtime is recalculated after downtime is merged.
                "runtime": gross_runtime,
                "late_start_time": late_start_minutes,
                "waiting_time": waiting_time,
                "change_over_time": adjusted_change_over,
                "natural_buffer_time": natural_buffer_time,
            }
        )

    return pd.DataFrame(results)


def _calculate_runtime_segments_by_folder_day(book_df: pd.DataFrame) -> pd.DataFrame:
    keys = [REPORT_DATE_COLUMN, "Machine", "Folder"]
    columns = [*keys, "runtime_segments"]

    if book_df.empty:
        return pd.DataFrame(columns=columns)

    segment_rows = []
    dedupe_columns = [
        REPORT_DATE_COLUMN,
        "Machine",
        "Folder",
        "Issue Id",
        "Complexities",
        "Effective Start DateTime",
        "Effective End DateTime",
        "Print Order",
        "Total Run Time (mnts)",
        "Reflong"
    ]
    df = book_df.drop_duplicates(
        subset=[column for column in dedupe_columns if column in book_df.columns]
    ).copy()

    for (report_date, machine, folder), group in df.groupby(keys, dropna=False):
        category_totals: dict[str, dict[str, Any]] = {}

        for _, row in group.iterrows():
            classification = _categorize_complexity(row.get("Complexities"))
            runtime_minutes = _parse_minutes_value(row.get("Total Run Time (mnts)"))
            print_order = _parse_count_value(row.get("Print Order"))

            if runtime_minutes <= 0:
                continue

            key = classification["key"]
            current = category_totals.setdefault(
                key,
                {
                    "key": key,
                    "label": classification["label"],
                    "type": classification["type"],
                    "is_complex": classification["is_complex"],
                    "runtime_minutes": 0.0,
                    "print_order": 0.0,
                    "speed_runtime_minutes": 0.0,
                    "speed_print_order": 0.0,
                },
            )
            current["runtime_minutes"] += runtime_minutes
            current["print_order"] += print_order

            is_reflong = _clean_text(row.get("Reflong")).casefold() == "yes"
            if not is_reflong:
                current["speed_runtime_minutes"] += runtime_minutes
                current["speed_print_order"] += print_order

        segments = []
        for segment in category_totals.values():
            runtime_minutes = float(segment["runtime_minutes"])
            print_order = float(segment["print_order"])
            speed_runtime_minutes = float(segment["speed_runtime_minutes"])
            speed_print_order = float(segment["speed_print_order"])
            segments.append(
                {
                    "key": segment["key"],
                    "label": segment["label"],
                    "type": segment["type"],
                    "is_complex": segment["is_complex"],
                    "minutes": _clean_number(runtime_minutes),
                    "source_runtime_minutes": _clean_number(runtime_minutes),
                    "print_order": _clean_number(print_order),
                    "effective_speed": _clean_number(
                        _calculate_effective_speed(speed_print_order, speed_runtime_minutes)
                    ),
                }
            )

        segment_rows.append(
            {
                REPORT_DATE_COLUMN: report_date,
                "Machine": machine,
                "Folder": folder,
                "runtime_segments": sorted(
                    segments,
                    key=lambda item: ["snp", "snp_complex", "gnp", "gnp_complex", "unknown"].index(
                        item["key"] if item["key"] in ["snp", "snp_complex", "gnp", "gnp_complex", "unknown"] else "unknown"
                    ),
                ),
            }
        )

    if not segment_rows:
        return pd.DataFrame(columns=columns)

    return pd.DataFrame(segment_rows)


def _scale_runtime_segments(value: Any, runtime_minutes: float) -> list[dict[str, Any]]:
    if not isinstance(value, list) or runtime_minutes <= 0:
        return []

    source_total = sum(float(segment.get("source_runtime_minutes") or segment.get("minutes") or 0) for segment in value)
    if source_total <= 0:
        return []

    scale = float(runtime_minutes) / source_total
    scaled_segments = []

    for segment in value:
        source_minutes = float(segment.get("source_runtime_minutes") or segment.get("minutes") or 0)
        if source_minutes <= 0:
            continue

        scaled = dict(segment)
        scaled["minutes"] = _clean_number(source_minutes * scale)
        scaled_segments.append(scaled)

    return _normalize_segment_minutes(scaled_segments, runtime_minutes)


def _normalize_segment_minutes(segments: list[dict[str, Any]], target_minutes: float) -> list[dict[str, Any]]:
    if not segments:
        return []

    remaining = _clean_number(target_minutes)
    normalized = []

    for index, segment in enumerate(segments):
        next_segment = dict(segment)
        if index == len(segments) - 1:
            next_segment["minutes"] = _clean_number(max(float(remaining), 0.0))
        else:
            minutes = min(float(segment.get("minutes") or 0), float(remaining))
            next_segment["minutes"] = _clean_number(max(minutes, 0.0))
            remaining = _clean_number(float(remaining) - float(next_segment["minutes"]))

        if next_segment["minutes"] > 0:
            normalized.append(next_segment)

    return normalized


def _extract_last_tiff_times(group: pd.DataFrame) -> dict[int, pd.Timestamp]:
    """Extract Last Tiff times for each edition, handling Reflong cases.
    
    For each unique Issue Id in the group:
    - If there's a row with Reflong='Yes', use the earliest Last Tiff time
    - Otherwise, use the latest Last Tiff time
    
    Returns a dict mapping merged_interval_index to Last Tiff timestamp.
    """
    last_tiff_times = {}
    
    # Group by Issue Id to handle Reflong logic
    for issue_id, issue_group in group.groupby("Issue Id"):
        # Check if any row has Reflong='Yes'
        has_reflong = (issue_group["Reflong"].fillna("").astype(str).str.strip().str.casefold() == "yes").any()
        
        # Get all valid Last Tiff times
        valid_tiffs = [
            pd.Timestamp(value)
            for value in issue_group[issue_group["Last Tiff DateTime"].notna()]["Last Tiff DateTime"]
        ]
        
        if len(valid_tiffs) > 0:
            # If there's a Reflong, pick the earliest actual timestamp; otherwise pick the latest.
            selected_tiff = min(valid_tiffs) if has_reflong else max(valid_tiffs)
            
            # Map all rows in this issue to this Last Tiff time
            for idx in issue_group.index:
                last_tiff_times[idx] = selected_tiff
    
    return last_tiff_times


def _calculate_waiting_and_timing(
    group: pd.DataFrame,
    merged_intervals: list[tuple[pd.Timestamp, pd.Timestamp]],
    window_start: pd.Timestamp,
    last_tiff_times: dict[int, pd.Timestamp]
) -> tuple[float, float, float]:
    """Calculate waiting_time, late_start_time (LPR), and change_over_time for all intervals.
    
    For the first edition:
    - If Last Tiff is within the window: waiting_time = Last Tiff - window_start, late_start_time = print_start - Last Tiff
    - If Last Tiff is before the window: waiting_time = 0, late_start_time = print_start - window_start
    
    For subsequent editions:
    - waiting_time = Last Tiff - previous_edition_end
    - change_over_time = current_edition_start - Last Tiff
    
    Returns: (total_waiting_time, late_start_time_for_first, total_change_over_time)
    """
    waiting_time = 0.0
    late_start_time = 0.0
    change_over_time = 0.0
    
    if not merged_intervals or len(group) == 0:
        return 0.0, 0.0, 0.0
    
    # Build a mapping of interval start times to the first row that starts that interval
    group_sorted = group.sort_values("Effective Start DateTime")
    interval_to_row_index = {}
    
    for interval_idx, (interval_start, _) in enumerate(merged_intervals):
        # Find the first row(s) in the group that start at or before this interval
        rows_at_start = group_sorted[group_sorted["Effective Start DateTime"] == interval_start]
        if not rows_at_start.empty:
            # Get the first row's original index
            first_row_original_idx = rows_at_start.index[0]
            interval_to_row_index[interval_idx] = first_row_original_idx
    
    # Process first interval
    first_print_start = merged_intervals[0][0]
    first_last_tiff = last_tiff_times.get(interval_to_row_index.get(0))
    
    if first_last_tiff and pd.notna(first_last_tiff):
        if first_last_tiff >= window_start:
            ready_at = min(first_last_tiff, first_print_start)
            waiting_time = max((ready_at - window_start).total_seconds() / 60, 0.0)
            late_start_time = max((first_print_start - ready_at).total_seconds() / 60, 0.0)
        else:
            # Last Tiff is before the window (e.g., previous day)
            # No waiting time needed; edition was already ready
            waiting_time = 0.0
            late_start_time = max((first_print_start - window_start).total_seconds() / 60, 0.0)
    else:
        # No Last Tiff available, use original calculation
        late_start_time = max((first_print_start - window_start).total_seconds() / 60, 0.0)
    
    # Process subsequent intervals
    for i in range(len(merged_intervals) - 1):
        current_end = merged_intervals[i][1]
        next_start = merged_intervals[i + 1][0]
        next_interval_idx = i + 1
        
        next_last_tiff = last_tiff_times.get(interval_to_row_index.get(next_interval_idx))
        
        if next_last_tiff and pd.notna(next_last_tiff):
            # If the next edition was ready before the previous print ended, the
            # full gap is changeover. Otherwise split the gap into waiting and
            # changeover around the next Last Tiff timestamp.
            ready_at = min(max(next_last_tiff, current_end), next_start)
            edition_waiting = max((ready_at - current_end).total_seconds() / 60, 0.0)
            waiting_time += edition_waiting

            edition_changeover = max((next_start - ready_at).total_seconds() / 60, 0.0)
            change_over_time += edition_changeover
        else:
            # No Last Tiff available, treat gap as change-over time
            gap = (next_start - current_end).total_seconds() / 60
            if gap > 0:
                change_over_time += gap
    
    return waiting_time, late_start_time, change_over_time


def calculate_daily_metrics(folder_day_df: pd.DataFrame) -> pd.DataFrame:
    """Aggregate folder-day capacity units into daily executive metrics using Issue Date."""
    if folder_day_df.empty:
        return _empty_daily_metrics()

    daily = (
        folder_day_df.groupby(REPORT_DATE_COLUMN, dropna=False)
        .agg(
            active_folders_count=("Folder", "count"),
            available_capacity=("available_capacity", "sum"),
            runtime=("runtime", "sum"),
            lost_time=("lost_time", "sum"),
            downtime=("downtime", "sum"),
            buffer_time=("buffer_time", "sum"),
        )
        .reset_index()
        .rename(columns={REPORT_DATE_COLUMN: "Run Date"})
        .sort_values("Run Date")
    )

    capacity_folders_count = int(daily["active_folders_count"].max())
    fixed_daily_capacity = capacity_folders_count * CAPACITY_MINUTES_PER_FOLDER_DAY
    unused_folder_capacity = (
        fixed_daily_capacity - daily["available_capacity"]
    ).clip(lower=0)

    daily["capacity_folders_count"] = capacity_folders_count
    daily["available_capacity"] = fixed_daily_capacity
    daily["buffer_time"] = daily["buffer_time"] + unused_folder_capacity

    daily["utilization_percentage"] = daily.apply(
        lambda row: _percentage(row["runtime"], row["available_capacity"]),
        axis=1,
    )

    return daily


def calculate_tower_day_metrics(
    book_df: pd.DataFrame,
    down_time_df: pd.DataFrame,
    general_df: pd.DataFrame,
) -> pd.DataFrame:
    """Calculate engaged minutes for each tower in the Issue Date 00:00-04:00 window."""
    columns = [
        REPORT_DATE_COLUMN,
        "Machine",
        "Tower",
        "available_capacity",
        "gross_runtime",
        "runtime",
        "downtime",
        "change_over_time",
        "waiting_time",
        "reflong_related_downtime",
        "late_start_time",
        "buffer_time",
    ]

    if general_df.empty:
        return pd.DataFrame(columns=columns)

    tower_lookup = _build_issue_tower_lookup(general_df)
    if not tower_lookup:
        return pd.DataFrame(columns=columns)

    active_rows = book_df[_has_capacity_keys(book_df)].copy()
    if active_rows.empty:
        return pd.DataFrame(columns=columns)

    interval_editions = _filter_interval_editions(active_rows)
    if interval_editions.empty:
        return pd.DataFrame(columns=columns)

    interval_editions = interval_editions[
        [
            REPORT_DATE_COLUMN,
            "Issue Id",
            "Machine",
            "Reflong",
            "Last Tiff DateTime",
            "Effective Start DateTime",
            "Effective End DateTime",
            "Window Start",
            "Window End",
        ]
    ].drop_duplicates()

    tower_intervals: dict[tuple[date, str, str], list[tuple[pd.Timestamp, pd.Timestamp]]] = {}
    tower_rows: dict[tuple[date, str, str], list[dict[str, Any]]] = {}
    issue_tower_lookup: dict[str, list[str]] = {}
    issue_machine_lookup: dict[str, str] = {}
    issue_date_lookup: dict[str, date] = {}
    issue_reflong_lookup: dict[str, bool] = {}

    for _, row in interval_editions.iterrows():
        report_date = row[REPORT_DATE_COLUMN]
        issue_id = row["Issue Id"]
        machine = row["Machine"]
        towers = tower_lookup.get(issue_id, [])

        if not report_date or not towers or not machine:
            continue

        interval = (
            pd.Timestamp(row["Effective Start DateTime"]),
            pd.Timestamp(row["Effective End DateTime"]),
        )

        issue_date_lookup.setdefault(issue_id, report_date)
        issue_reflong_lookup[issue_id] = (
            issue_reflong_lookup.get(issue_id, False)
            or _clean_text(row["Reflong"]).casefold() == "yes"
        )
        issue_tower_lookup.setdefault(issue_id, towers)
        issue_machine_lookup.setdefault(issue_id, machine)

        for tower in towers:
            tower_key = (report_date, machine, tower)
            tower_intervals.setdefault(tower_key, []).append(interval)
            tower_rows.setdefault(tower_key, []).append(row.to_dict())

    tower_down_time = _aggregate_down_time_by_tower(
        down_time_df,
        issue_tower_lookup,
        issue_machine_lookup,
        issue_date_lookup,
        issue_reflong_lookup,
    )

    records = []

    for (report_date, machine, tower), intervals in tower_intervals.items():
        merged_intervals = _merge_print_intervals(intervals)
        runtime = sum(
            (end_dt - start_dt).total_seconds() / 60
            for start_dt, end_dt in merged_intervals
        )
        change_over_time = 0.0
        waiting_time = 0.0
        late_start_time = 0.0

        group = pd.DataFrame(tower_rows.get((report_date, machine, tower), []))
        if merged_intervals:
            # Determine window_start - either from group or construct default
            window_start = None
            if not group.empty:
                window_start = group.iloc[0].get("Window Start")
            
            if pd.isna(window_start):
                window_start = pd.Timestamp(datetime.combine(report_date, time(0, 0)))
            else:
                window_start = pd.Timestamp(window_start)
            
            # Always use _calculate_waiting_and_timing if we have group data with Last Tiff
            if not group.empty:
                last_tiff_times = _extract_last_tiff_times(group)
                waiting_time, late_start_time, change_over_time = _calculate_waiting_and_timing(
                    group,
                    merged_intervals,
                    window_start,
                    last_tiff_times,
                )
            else:
                # No group data, use fallback calculation
                late_start_time = max(
                    (merged_intervals[0][0] - window_start).total_seconds() / 60,
                    0.0,
                )
                change_over_time = _calculate_changeover_minutes(merged_intervals)

        downtime_parts = tower_down_time.get(
            (report_date, machine, tower),
            {"downtime": 0.0, "reflong_related_downtime": 0.0},
        )
        downtime = float(downtime_parts["downtime"])
        reflong_related_downtime = float(downtime_parts["reflong_related_downtime"])
        runtime_minutes = min(
            max(runtime - downtime - reflong_related_downtime, 0.0),
            CAPACITY_MINUTES_PER_FOLDER_DAY,
        )
        downtime_minutes = min(max(downtime, 0.0), CAPACITY_MINUTES_PER_FOLDER_DAY)
        changeover_minutes = min(max(change_over_time, 0.0), CAPACITY_MINUTES_PER_FOLDER_DAY)
        waiting_minutes = min(max(waiting_time, 0.0), CAPACITY_MINUTES_PER_FOLDER_DAY)
        reflong_minutes = min(max(reflong_related_downtime, 0.0), CAPACITY_MINUTES_PER_FOLDER_DAY)
        late_start_minutes = min(max(late_start_time, 0.0), CAPACITY_MINUTES_PER_FOLDER_DAY)
        fixed_used_minutes = (
            runtime_minutes
            + downtime_minutes
            + changeover_minutes
            + waiting_minutes
            + reflong_minutes
            + late_start_minutes
        )
        buffer_minutes = min(
            max(CAPACITY_MINUTES_PER_FOLDER_DAY - fixed_used_minutes, 0.0),
            CAPACITY_MINUTES_PER_FOLDER_DAY,
        )

        records.append(
            {
                REPORT_DATE_COLUMN: report_date,
                "Machine": machine,
                "Tower": tower,
                "available_capacity": CAPACITY_MINUTES_PER_FOLDER_DAY,
                "gross_runtime": min(max(runtime, 0.0), CAPACITY_MINUTES_PER_FOLDER_DAY),
                "runtime": runtime_minutes,
                "downtime": downtime_minutes,
                "change_over_time": changeover_minutes,
                "waiting_time": waiting_minutes,
                "reflong_related_downtime": reflong_minutes,
                "late_start_time": late_start_minutes,
                "buffer_time": buffer_minutes,
            }
        )

    if not records:
        return pd.DataFrame(columns=columns)

    return pd.DataFrame(records).sort_values([REPORT_DATE_COLUMN, "Machine", "Tower"]).reset_index(drop=True)


def calculate_summary_metrics(daily_df: pd.DataFrame) -> dict[str, float | int]:
    total_available = float(daily_df["available_capacity"].sum()) if not daily_df.empty else 0.0
    total_runtime = float(daily_df["runtime"].sum()) if not daily_df.empty else 0.0
    total_buffer_time = float(daily_df["buffer_time"].sum()) if not daily_df.empty else 0.0

    return {
        "total_available_capacity": _clean_number(total_available),
        "total_runtime": _clean_number(total_runtime),
        "total_lost_time": _clean_number(daily_df["lost_time"].sum() if not daily_df.empty else 0),
        "total_downtime": _clean_number(daily_df["downtime"].sum() if not daily_df.empty else 0),
        "total_buffer_time": _clean_number(total_buffer_time),
        "average_utilization_percentage": _clean_number(_percentage(total_runtime, total_available)),
        "spare_capacity_percentage": _clean_number(_percentage(total_buffer_time, total_available)),
        "active_folder_days": int(daily_df["active_folders_count"].sum()) if not daily_df.empty else 0,
    }


def _categorize_complexity(complexity_code: str) -> dict[str, Any]:
    """Categorize complexity code into type and whether it's complex.

    Returns dict with:
    - type: "SNP" or "GNP"
    - is_complex: True or False
    """
    if not complexity_code or pd.isna(complexity_code):
        return {"key": "unknown", "label": "Unknown", "type": "Unknown", "is_complex": False}

    code = str(complexity_code).strip()
    match = re.search(r"\bC(\d{1,2})\b", code, flags=re.IGNORECASE)
    if match:
        code = f"C{int(match.group(1))}"

    # C1-C3: SNP
    if code in ["C1", "C2", "C3"]:
        return {"key": "snp", "label": "SNP", "type": "SNP", "is_complex": False}

    # C4: SNP - Complex
    if code == "C4":
        return {"key": "snp_complex", "label": "SNP Complex", "type": "SNP", "is_complex": True}

    # C5-C8: GNP
    if code in ["C5", "C6", "C7", "C8"]:
        return {"key": "gnp", "label": "GNP", "type": "GNP", "is_complex": False}

    # C9-C15: GNP - Complex
    if code in ["C9", "C10", "C11", "C12", "C13", "C14", "C15"]:
        return {"key": "gnp_complex", "label": "GNP Complex", "type": "GNP", "is_complex": True}

    return {"key": "unknown", "label": "Unknown", "type": "Unknown", "is_complex": False}


def _calculate_effective_speed(print_order: float, runtime_minutes: float) -> float:
    """Calculate effective speed in copies per hour (cph).

    Formula: Print Order / runtime (in hours)
    Example: 2,20,000 copies in 110 minutes = 120k cph
    """
    if runtime_minutes <= 0:
        return 0.0

    runtime_hours = runtime_minutes / 60.0
    if runtime_hours <= 0:
        return 0.0

    # Parse print order if it's a string
    try:
        if isinstance(print_order, str):
            print_order = float(print_order.replace(",", ""))
        else:
            print_order = float(print_order)
    except (ValueError, TypeError):
        return 0.0

    if print_order <= 0:
        return 0.0

    cph = print_order / runtime_hours
    return cph


def build_capacity_response(workbook: pd.ExcelFile) -> dict[str, Any]:
    general_df = parse_general(workbook)
    book_df = parse_book_wise_details(workbook)
    down_time_df = parse_down_time(workbook)
    folder_day_df = calculate_folder_day_metrics(book_df, down_time_df)
    tower_day_df = calculate_tower_day_metrics(book_df, down_time_df, general_df)
    daily_df = calculate_daily_metrics(folder_day_df)

    return {
        "valid": True,
        "summary": calculate_summary_metrics(daily_df),
        "daily": _daily_records(daily_df),
        "details": _detail_records(folder_day_df),
        "tower_details": _tower_detail_records(tower_day_df),
        "errors": [],
    }


def _read_sheet(workbook: pd.ExcelFile, sheet_name: str) -> pd.DataFrame:
    df = pd.read_excel(workbook, sheet_name=sheet_name)
    df = df.rename(columns=lambda column: str(column).strip())
    df = df.dropna(how="all").copy()
    return df


def _has_capacity_keys(df: pd.DataFrame) -> pd.Series:
    date_column = REPORT_DATE_COLUMN if REPORT_DATE_COLUMN in df.columns else "Run Date"

    return (
        df[date_column].notna()
        & df["Machine"].ne("")
        & df["Folder"].ne("")
    )


def _aggregate_down_time_by_capacity_unit(book_df: pd.DataFrame, down_time_df: pd.DataFrame) -> pd.DataFrame:
    keys = ["Run Date", "Machine", "Folder"]
    down_rows = down_time_df[_has_capacity_keys(down_time_df)].copy()

    if down_rows.empty:
        return pd.DataFrame(columns=[*keys, "total_downtime", "reflong_related_downtime"])

    issue_reflong_lookup = (
        book_df[book_df["Issue Id"].ne("")]
        .assign(reflong_yes=book_df["Reflong"].apply(lambda value: value.strip().casefold() == "yes"))
        .groupby("Issue Id")["reflong_yes"]
        .any()
        .to_dict()
    )

    down_rows["book_reflong_yes"] = down_rows["IssueID"].map(issue_reflong_lookup).fillna(False)
    down_rows["related_starts_reflong"] = down_rows["Related"].str.casefold().str.startswith("reflong")
    down_rows["reflong_related_downtime"] = down_rows["Total Downtime"].where(
        down_rows["book_reflong_yes"] & down_rows["related_starts_reflong"],
        0.0,
    )

    return (
        down_rows.groupby(keys, dropna=False)
        .agg(
            total_downtime=("Total Downtime", "sum"),
            reflong_related_downtime=("reflong_related_downtime", "sum"),
        )
        .reset_index()
    )


def _calculate_late_start_minutes(run_date: date | None, first_start: Any) -> float:
    if run_date is None or pd.isna(first_start):
        return 0.0

    expected_start = pd.Timestamp(datetime.combine(run_date, time.min))
    actual_start = pd.Timestamp(first_start)

    if actual_start <= expected_start:
        return 0.0

    delay_minutes = (actual_start - expected_start).total_seconds() / 60
    return min(delay_minutes, CAPACITY_MINUTES_PER_FOLDER_DAY)


def _combine_date_time(date_value: Any, time_value: Any, fallback_date: date | None) -> pd.Timestamp | pd.NaT:
    parsed_date = _parse_date_value(date_value) or fallback_date
    parsed_time = _parse_time_value(time_value)

    if parsed_date is None or parsed_time is None:
        return pd.NaT

    return pd.Timestamp(datetime.combine(parsed_date, parsed_time))


def _parse_datetime_value(value: Any) -> pd.Timestamp | pd.NaT:
    """Parse a datetime value in multiple formats, including 'DD/MM/YYYY HH:MM:SS'."""
    if _is_blank(value):
        return pd.NaT

    if isinstance(value, datetime):
        return pd.Timestamp(value)

    if isinstance(value, pd.Timestamp):
        return value

    text = str(value).strip()
    
    # Try standard datetime parsing
    parsed = pd.to_datetime(text, errors="coerce", dayfirst=True)
    if not pd.isna(parsed):
        return pd.Timestamp(parsed)
    
    return pd.NaT


def _parse_date_value(value: Any) -> date | None:
    if _is_blank(value):
        return None

    if isinstance(value, datetime):
        return value.date()

    if isinstance(value, date):
        return value

    if isinstance(value, (int, float)) and isfinite(float(value)):
        try:
            return pd.to_datetime(value, unit="D", origin="1899-12-30").date()
        except Exception:
            return None

    text = str(value).strip()
    dayfirst = not bool(re.match(r"^\d{4}[-/]\d{1,2}[-/]\d{1,2}", text))
    parsed = pd.to_datetime(text, errors="coerce", dayfirst=dayfirst)
    if pd.isna(parsed):
        return None
    return parsed.date()


def _parse_time_value(value: Any) -> time | None:
    if _is_blank(value):
        return None

    if isinstance(value, datetime):
        return value.time().replace(tzinfo=None)

    if isinstance(value, time):
        return value.replace(tzinfo=None)

    if isinstance(value, timedelta):
        seconds = int(value.total_seconds()) % (24 * 60 * 60)
        return (datetime.min + timedelta(seconds=seconds)).time()

    if isinstance(value, (int, float)) and isfinite(float(value)):
        numeric = float(value)
        if 0 <= numeric < 1:
            seconds = int(round(numeric * 24 * 60 * 60))
            return (datetime.min + timedelta(seconds=seconds)).time()
        if 0 <= numeric <= 24:
            hours = int(numeric)
            minutes = int(round((numeric - hours) * 60))
            return (datetime.min + timedelta(hours=hours, minutes=minutes)).time()
        return None

    text = str(value).strip()
    for fmt in ("%H:%M:%S", "%H:%M"):
        try:
            return datetime.strptime(text, fmt).time()
        except ValueError:
            pass

    parsed = pd.to_datetime(text, errors="coerce")
    if pd.isna(parsed):
        return None
    return parsed.time().replace(tzinfo=None)


def _parse_minutes_value(value: Any) -> float:
    if _is_blank(value):
        return 0.0

    if isinstance(value, timedelta):
        return max(value.total_seconds() / 60, 0.0)

    if isinstance(value, time):
        return float(value.hour * 60 + value.minute + value.second / 60)

    if isinstance(value, (int, float)) and isfinite(float(value)):
        return max(float(value), 0.0)

    text = str(value).strip()
    if ":" in text:
        parts = text.split(":")
        try:
            numbers = [float(part) for part in parts]
        except ValueError:
            numbers = []

        if len(numbers) == 2:
            return max(numbers[0] * 60 + numbers[1], 0.0)
        if len(numbers) == 3:
            return max(numbers[0] * 60 + numbers[1] + numbers[2] / 60, 0.0)

    numeric = pd.to_numeric(text, errors="coerce")
    if pd.isna(numeric):
        return 0.0
    return max(float(numeric), 0.0)


def _parse_count_value(value: Any) -> float:
    if _is_blank(value):
        return 0.0

    if isinstance(value, (int, float)) and isfinite(float(value)):
        return max(float(value), 0.0)

    text = str(value).replace(",", "").strip()
    numeric = pd.to_numeric(text, errors="coerce")
    if pd.isna(numeric):
        return 0.0

    return max(float(numeric), 0.0)


def _clean_text(value: Any) -> str:
    if _is_blank(value):
        return ""

    if isinstance(value, float) and value.is_integer():
        return str(int(value))

    return str(value).strip()


def _build_issue_tower_lookup(general_df: pd.DataFrame) -> dict[str, list[str]]:
    lookup: dict[str, list[str]] = {}

    for _, row in general_df.iterrows():
        issue_id = _clean_text(row.get("IssueID"))
        towers = _split_towers(row.get("Towers used"))

        if not issue_id or not towers:
            continue

        existing_towers = lookup.setdefault(issue_id, [])
        for tower in towers:
            if tower not in existing_towers:
                existing_towers.append(tower)

    return lookup


def _split_towers(value: Any) -> list[str]:
    text = _clean_text(value)
    if not text:
        return []

    return [
        tower
        for tower in (_clean_text(part) for part in text.split(","))
        if tower
    ]


def _is_blank(value: Any) -> bool:
    if value is None:
        return True

    try:
        return bool(pd.isna(value))
    except (TypeError, ValueError):
        return False


def _percentage(numerator: float, denominator: float) -> float:
    if denominator <= 0:
        return 0.0
    percentage = numerator / denominator * 100
    return min(percentage, 100.0)


def _clean_number(value: Any) -> int | float:
    numeric = float(value) if value is not None else 0.0
    if not isfinite(numeric):
        numeric = 0.0
    rounded = round(numeric, 2)
    if rounded.is_integer():
        return int(rounded)
    return rounded


def _format_run_date(value: Any) -> str:
    if isinstance(value, date):
        return value.isoformat()
    parsed = _parse_date_value(value)
    return parsed.isoformat() if parsed else ""


def _daily_records(daily_df: pd.DataFrame) -> list[dict[str, Any]]:
    if daily_df.empty:
        return []

    renamed = daily_df.rename(columns={"Run Date": "run_date"}).copy()
    renamed["run_date"] = renamed["run_date"].apply(_format_run_date)
    return _rounded_records(renamed)


def _detail_records(folder_day_df: pd.DataFrame) -> list[dict[str, Any]]:
    if folder_day_df.empty:
        return []

    renamed = folder_day_df.rename(
        columns={
            REPORT_DATE_COLUMN: "run_date",
            "Machine": "machine",
            "Folder": "folder",
        }
    ).copy()

    renamed["run_date"] = renamed["run_date"].apply(_format_run_date)
    # Combine machine and folder into a single folder identifier with newline for better display
    renamed["folder"] = renamed["machine"] + "\n" + renamed["folder"]

    return _rounded_records(
        renamed[
            [
                "run_date",
                "folder",
                "available_capacity",
                "gross_runtime",
                "runtime",
                "lost_time",
                "downtime",
                "buffer_time",
                "change_over_time",
                "waiting_time",
                "reflong_related_downtime",
                "late_start_time",
                "runtime_segments",
            ]
        ]
    )


def _tower_detail_records(tower_day_df: pd.DataFrame) -> list[dict[str, Any]]:
    if tower_day_df.empty:
        return []

    renamed = tower_day_df.rename(
        columns={
            REPORT_DATE_COLUMN: "run_date",
            "Machine": "machine",
            "Tower": "tower",
        }
    ).copy()

    renamed["run_date"] = renamed["run_date"].apply(_format_run_date)
    # Combine machine and tower into a single tower identifier with newline for better display
    renamed["tower"] = renamed["machine"] + "\n" + renamed["tower"]

    return _rounded_records(
        renamed[
            [
                "run_date",
                "tower",
                "available_capacity",
                "gross_runtime",
                "runtime",
                "downtime",
                "change_over_time",
                "waiting_time",
                "reflong_related_downtime",
                "late_start_time",
                "buffer_time",
            ]
        ]
    )


def _rounded_records(df: pd.DataFrame) -> list[dict[str, Any]]:
    records: list[dict[str, Any]] = []
    for record in df.to_dict(orient="records"):
        records.append(
            {
                key: _clean_number(value) if isinstance(value, Real) else value
                for key, value in record.items()
            }
        )
    return records


def _empty_folder_day_metrics() -> pd.DataFrame:
    return pd.DataFrame(
        columns=[
            REPORT_DATE_COLUMN,
            "Machine",
            "Folder",
            "available_capacity",
            "gross_runtime",
            "runtime",
            "lost_time",
            "downtime",
            "buffer_time",
            "change_over_time",
            "waiting_time",
            "reflong_related_downtime",
            "late_start_time",
            "runtime_segments",
        ]
    )


def _empty_daily_metrics() -> pd.DataFrame:
    return pd.DataFrame(
        columns=[
            "Run Date",
            "active_folders_count",
            "capacity_folders_count",
            "available_capacity",
            "runtime",
            "lost_time",
            "downtime",
            "buffer_time",
            "utilization_percentage",
        ]
    )
