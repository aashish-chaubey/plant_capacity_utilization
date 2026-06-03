import {
  Bar,
  BarChart,
  CartesianGrid,
  ResponsiveContainer,
  Tooltip,
  XAxis,
  YAxis
} from "recharts";
import { ChevronLeft, ChevronRight, X } from "lucide-react";
import { useEffect, useMemo, useRef, useState } from "react";

import KpiCard from "./KpiCard.jsx";

const COLORS = {
  runtime: "#2563eb",
  lost_time: "#f59e0b",
  downtime: "#dc2626",
  buffer_time: "#cbd5e1",
  reflong_related_downtime: "#fede1c",
  waiting_time: "#ffce00",
  change_over_time: "#ffa701",
  late_start_time: "#ff8d02"
};

const ENGAGED_TIME_OPTIONS = [
  {
    key: "runtime",
    label: "Print runtime",
    color: COLORS.runtime
  },
  {
    key: "downtime",
    label: "Downtime / breakdown",
    color: COLORS.downtime
  },
  {
    key: "reflong_related_downtime",
    label: "Reflong time",
    color: COLORS.reflong_related_downtime
  },
  {
    key: "waiting_time",
    label: "Waiting time",
    color: COLORS.waiting_time
  },
  {
    key: "change_over_time",
    label: "Changeover time",
    color: COLORS.change_over_time
  },
  {
    key: "late_start_time",
    label: "LPR to print start",
    color: COLORS.late_start_time
  },
  {
    key: "buffer_time",
    label: "Spare time",
    color: COLORS.buffer_time
  }
];

const CAPACITY_WINDOW_MINUTES = 240;
const CAPACITY_PAGE_SIZE = 7;

const CAPACITY_SPLIT_COLORS = {
  waiting_time: "#B0B0B0",
  loss_time: "#F3C97B",
  downtime: "#FF9AA2",
  runtime: "#B2CFB2",
  spare_time: "#C5E1FF",
  idle_time: "#E5E7EB",
  window_line: "#234775"
};

const CAPACITY_SPLIT_LEGEND = [
  { key: "waiting_time", label: "Wait Time", color: CAPACITY_SPLIT_COLORS.waiting_time },
  { key: "loss_time", label: "Loss Time", color: CAPACITY_SPLIT_COLORS.loss_time },
  { key: "downtime", label: "Downtime", color: CAPACITY_SPLIT_COLORS.downtime },
  { key: "runtime_snp", label: "Run Time: SNP", color: "#CCDCCC" },
  { key: "runtime_gnp", label: "Run Time: GNP", color: "#88AA88" },
  { key: "complex_prints", label: "Complex prints", marker: "triangle" },
  { key: "spare_time", label: "Spare Time", color: CAPACITY_SPLIT_COLORS.spare_time },
  { key: "idle_time", label: "Idle (not scheduled)", color: CAPACITY_SPLIT_COLORS.idle_time, pattern: "idle" }
];

const RUNTIME_SEGMENT_STYLES = {
  snp: {
    color: "#CCDCCC",
    textColor: "#0f172a",
    label: "Runtime"
  },
  snp_complex: {
    color: "#CCDCCC",
    textColor: "#0f172a",
    label: "Runtime",
    isComplex: true
  },
  gnp: {
    color: "#88AA88",
    textColor: "#0f172a",
    label: "Runtime"
  },
  gnp_complex: {
    color: "#88AA88",
    textColor: "#0f172a",
    label: "Runtime",
    isComplex: true
  },
  unknown: {
    color: CAPACITY_SPLIT_COLORS.runtime,
    textColor: "#14532d",
    label: "Run"
  }
};

const FOLDER_ALIAS_COLORS = ["#2563eb", "#7c3aed", "#dc2626", "#d97706", "#059669", "#0f766e", "#475569"];

export default function Dashboard({ data }) {
  const [focusedDay, setFocusedDay] = useState("");
  const [engagedComponentKeys, setEngagedComponentKeys] = useState(["runtime"]);

  useEffect(() => {
    if (focusedDay && !data.daily.some((day) => day.run_date === focusedDay)) {
      setFocusedDay("");
    }
  }, [data.daily, focusedDay]);

  const breakdownDetails = useMemo(
    () =>
      focusedDay
        ? data.details.filter((row) => row.run_date === focusedDay)
        : data.details,
    [data.details, focusedDay]
  );

  const breakdownTowerDetails = useMemo(
    () =>
      focusedDay
        ? (data.tower_details || []).filter((row) => row.run_date === focusedDay)
        : data.tower_details || [],
    [data.tower_details, focusedDay]
  );

  const breakdownProductionDays = focusedDay ? 1 : data.daily.length;

  const towerBreakdown = useMemo(
    () => aggregateResourceUsage(breakdownTowerDetails, "tower", breakdownProductionDays, engagedComponentKeys, "natural"),
    [breakdownTowerDetails, breakdownProductionDays, engagedComponentKeys]
  );
  const folderBreakdown = useMemo(
    () => aggregateResourceUsage(breakdownDetails, "folder", breakdownProductionDays, engagedComponentKeys),
    [breakdownDetails, breakdownProductionDays, engagedComponentKeys]
  );
  const selectedEngagedOptions = useMemo(
    () => ENGAGED_TIME_OPTIONS.filter((option) => engagedComponentKeys.includes(option.key)),
    [engagedComponentKeys]
  );

  const breakdownScope = focusedDay ? focusedDay : "Selected timeframe";

  function toggleEngagedComponent(componentKey) {
    setEngagedComponentKeys((current) => {
      if (current.includes(componentKey)) {
        return current.length === 1 ? current : current.filter((key) => key !== componentKey);
      }

      return [...current, componentKey];
    });
  }

  const kpis = [
    ["Total Available Capacity", formatMinutes(data.summary.total_available_capacity), "blue"],
    ["Total Runtime", formatMinutes(data.summary.total_runtime), "green"],
    ["Total Lost Time", formatMinutes(data.summary.total_lost_time), "amber"],
    ["Total Downtime", formatMinutes(data.summary.total_downtime), "red"],
    ["Total Spare Time", formatMinutes(data.summary.total_buffer_time), "slate"],
    [
      "Spare Capacity",
      formatPercent(data.summary.spare_capacity_percentage ?? calculatePercentage(data.summary.total_buffer_time, data.summary.total_available_capacity)),
      "slate"
    ],
    ["Active Folder-Days", formatNumber(data.summary.active_folder_days), "slate"]
  ];

  return (
    <div className="mt-5 space-y-5">
      <section className="grid gap-3 sm:grid-cols-2 lg:grid-cols-4 2xl:grid-cols-7">
        {kpis.map(([label, value, tone]) => (
          <KpiCard key={label} label={label} value={value} tone={tone} />
        ))}
      </section>

      <section className="rounded-lg border border-slate-200 bg-white p-3 shadow-soft">
        <div className="mb-3 flex flex-col gap-2 sm:flex-row sm:items-end sm:justify-between">
          <div>
            <h2 className="text-base font-semibold text-slate-950">Daily capacity split</h2>
            <p className="mt-1 text-sm text-slate-500">Machine-folder capacity by Run Date</p>
          </div>
          <div className="text-sm text-slate-500">
            Breakdowns: <span className="font-semibold text-slate-800">{breakdownScope}</span>
          </div>
        </div>

        <CapacitySplitChart
          daily={data.daily}
          details={data.details}
          selectedDay={focusedDay}
          onSelectDay={setFocusedDay}
        />
      </section>

      <section className="space-y-3">
        <div className="flex flex-col gap-2 sm:flex-row sm:items-center sm:justify-between">
          <div>
            <h2 className="text-base font-semibold text-slate-950">Breakdown charts</h2>
            <p className="mt-1 text-sm text-slate-500">
              {focusedDay ? `Showing ${focusedDay}` : "Showing selected timeframe"}
            </p>
          </div>
          {focusedDay && (
            <button
              type="button"
              onClick={() => setFocusedDay("")}
              className="inline-flex min-h-9 items-center justify-center gap-2 rounded-lg border border-slate-200 bg-white px-3 text-sm font-semibold text-slate-700 shadow-sm transition hover:bg-slate-50 hover:text-slate-950"
            >
              <X className="h-4 w-4" aria-hidden="true" />
              Clear day
            </button>
          )}
        </div>

        <EngagedTimeSelector
          options={ENGAGED_TIME_OPTIONS}
          selectedKeys={engagedComponentKeys}
          onToggle={toggleEngagedComponent}
        />

        <div className="grid gap-4 xl:grid-cols-2">
          <UtilizationBreakdownChart
            title="Tower breakdown"
            subtitle={focusedDay ? "Tower utilization for selected day" : "Average tower utilization across the selected timeframe"}
            data={towerBreakdown}
            nameKey="tower"
            engagedOptions={selectedEngagedOptions}
            barSize={10}
            rowHeight={34}
            emptyMessage="No tower usage found for this selection."
          />
          <UtilizationBreakdownChart
            title="Folder breakdown"
            subtitle={focusedDay ? "Folder utilization for selected day" : "Average folder utilization across the selected timeframe"}
            data={folderBreakdown}
            nameKey="folder"
            engagedOptions={selectedEngagedOptions}
            barSize={24}
            rowHeight={54}
            emptyMessage="No folder usage found for this selection."
          />
        </div>
      </section>
    </div>
  );
}

function CapacitySplitChart({ daily, details, selectedDay, onSelectDay }) {
  const [pageStart, setPageStart] = useState(0);
  const [summaryPopover, setSummaryPopover] = useState(null);
  const chartFrameRef = useRef(null);

  const chartModel = useMemo(
    () => buildCapacitySplitModel(daily, details),
    [daily, details]
  );

  const { days, folders, rows } = chartModel;
  const maxPageStart = Math.max(days.length - CAPACITY_PAGE_SIZE, 0);
  const safePageStart = Math.min(pageStart, maxPageStart);
  const visibleDays = days.slice(safePageStart, safePageStart + CAPACITY_PAGE_SIZE);

  useEffect(() => {
    setPageStart(0);
  }, [days.length, folders.length]);

  const width = 1380;
  const height = 500;
  const margins = { top: 18, right: 8, bottom: 78, left: 62 };
  const yAxisTitleX = 14;
  const plotWidth = width - margins.left - margins.right;
  const plotHeight = height - margins.top - margins.bottom;
  const yMax = 270;
  const yTicks = [0, 60, 120, 180, 240];
  const dayCount = Math.max(visibleDays.length, 1);
  const groupWidth = plotWidth / dayCount;
  const folderCount = Math.max(folders.length, 1);
  const dayGap = 28;
  const barGap = 4;
  const availableGroupWidth = Math.max(groupWidth - dayGap, 28);
  const barWidth = Math.min(38, Math.max(10, (availableGroupWidth - barGap * (folderCount - 1)) / folderCount));
  const actualGroupWidth = barWidth * folderCount + barGap * (folderCount - 1);
  const viewRows = rows.filter((row) => visibleDays.includes(row.run_date));
  const selectedHighlightColor = "#475569";
  const selectedDaySummary = useMemo(
    () => buildCapacityDaySummary(summaryPopover?.day, rows, folders.length),
    [folders.length, rows, summaryPopover?.day]
  );

  useEffect(() => {
    if (!selectedDay) {
      setSummaryPopover(null);
      return;
    }

    setSummaryPopover((current) => {
      if (!current || current.day === selectedDay) return current;
      return null;
    });
  }, [selectedDay]);

  function xFor(dayIndex, folderIndex) {
    const groupStart = margins.left + dayIndex * groupWidth + (groupWidth - actualGroupWidth) / 2;
    return groupStart + folderIndex * (barWidth + barGap);
  }

  function groupStartFor(dayIndex) {
    return margins.left + dayIndex * groupWidth + (groupWidth - actualGroupWidth) / 2;
  }

  function daySeparatorXFor(dayIndex) {
    return margins.left + (dayIndex + 1) * groupWidth;
  }

  function yFor(minutes) {
    return margins.top + plotHeight - (Math.min(Math.max(minutes, 0), yMax) / yMax) * plotHeight;
  }

  function heightFor(minutes) {
    return (Math.min(Math.max(minutes, 0), yMax) / yMax) * plotHeight;
  }

  function setPreviousPage() {
    setSummaryPopover(null);
    setPageStart((current) => Math.max(current - CAPACITY_PAGE_SIZE, 0));
  }

  function setNextPage() {
    setSummaryPopover(null);
    setPageStart((current) => Math.min(current + CAPACITY_PAGE_SIZE, maxPageStart));
  }

  function handleBarClick(event, runDate) {
    const bounds = chartFrameRef.current?.getBoundingClientRect();
    onSelectDay(runDate);

    if (!bounds) {
      setSummaryPopover({ day: runDate, left: 12, top: 12 });
      return;
    }

    const cardWidth = 380;
    const cardHeight = 300;
    const localX = event.clientX - bounds.left;
    const localY = event.clientY - bounds.top;
    const left = localX + cardWidth + 24 > bounds.width
      ? Math.max(12, localX - cardWidth - 12)
      : Math.max(12, localX + 12);
    const top = Math.min(
      Math.max(12, localY - 24),
      Math.max(12, bounds.height - cardHeight - 12)
    );

    setSummaryPopover({ day: runDate, left, top });
  }

  if (!days.length || !folders.length) {
    return (
      <div className="flex h-80 items-center justify-center rounded-lg border border-dashed border-slate-200 bg-slate-50 text-sm text-slate-500">
        No machine-folder capacity data found for this selection.
      </div>
    );
  }

  return (
    <div className="space-y-3">
      <div className="flex flex-col gap-3 lg:flex-row lg:items-center lg:justify-between">
        <div className="flex flex-wrap gap-x-4 gap-y-2 text-xs text-slate-700">
          {CAPACITY_SPLIT_LEGEND.map((item) => (
            <div key={item.key} className="inline-flex items-center gap-1.5">
              {item.marker === "triangle" ? (
                <span className="text-[11px] font-black leading-none text-slate-950">▲</span>
              ) : (
                <span
                  className="h-3 w-3 rounded-sm border border-slate-300"
                  style={{
                    backgroundColor: item.color,
                    backgroundImage: item.pattern === "idle"
                      ? "repeating-linear-gradient(135deg, rgba(100,116,139,0.38) 0 1px, transparent 1px 4px)"
                      : "none"
                  }}
                />
              )}
              <span>{item.label}</span>
            </div>
          ))}
          <div className="inline-flex items-center gap-1.5">
            <span
              className="h-0 w-7 border-t border-dashed"
              style={{ borderColor: CAPACITY_SPLIT_COLORS.window_line }}
            />
            <span>4-hr Window</span>
          </div>
        </div>

        <div className="inline-flex items-center justify-end gap-2">
          <button
            type="button"
            onClick={setPreviousPage}
            disabled={safePageStart === 0}
            aria-label="Previous 7 days"
            className="inline-flex h-9 w-9 items-center justify-center rounded-lg border border-slate-200 text-slate-600 transition hover:bg-slate-50 disabled:cursor-not-allowed disabled:opacity-40"
          >
            <ChevronLeft className="h-4 w-4" aria-hidden="true" />
          </button>
          <span className="min-w-28 text-center text-xs font-semibold text-slate-500">
            {safePageStart + 1}-{Math.min(safePageStart + CAPACITY_PAGE_SIZE, days.length)} of {days.length}
          </span>
          <button
            type="button"
            onClick={setNextPage}
            disabled={safePageStart >= maxPageStart}
            aria-label="Next 7 days"
            className="inline-flex h-9 w-9 items-center justify-center rounded-lg border border-slate-200 text-slate-600 transition hover:bg-slate-50 disabled:cursor-not-allowed disabled:opacity-40"
          >
            <ChevronRight className="h-4 w-4" aria-hidden="true" />
          </button>
        </div>
      </div>

      <div className="flex flex-wrap gap-2">
        {folders.map((folder) => (
          <span
            key={folder.key}
            className="inline-flex min-h-8 items-center rounded-full px-3 text-xs font-bold text-white"
            style={{ backgroundColor: folder.color }}
          >
            {folder.alias}: {folder.shortName}
          </span>
        ))}
      </div>

      <div ref={chartFrameRef} className="relative rounded-lg border border-slate-100 bg-[#f3f6fa] p-1.5">
        <svg
          className="h-[500px] w-full"
          viewBox={`0 0 ${width} ${height}`}
          role="img"
          aria-label="Daily machine-folder capacity split"
        >
          <defs>
            <pattern id="idlePattern" patternUnits="userSpaceOnUse" width="8" height="8" patternTransform="rotate(35)">
              <rect width="8" height="8" fill={CAPACITY_SPLIT_COLORS.idle_time} opacity="0.55" />
              <line x1="0" y1="0" x2="0" y2="8" stroke="#94a3b8" strokeWidth="1" opacity="0.55" />
            </pattern>
          </defs>

          <rect x="0" y="0" width={width} height={height} fill="#f3f6fa" />

          {yTicks.map((tick) => (
            <g key={tick}>
              <line
                x1={margins.left}
                x2={width - margins.right}
                y1={yFor(tick)}
                y2={yFor(tick)}
                stroke={tick === CAPACITY_WINDOW_MINUTES ? CAPACITY_SPLIT_COLORS.window_line : "#d9e1ea"}
                strokeDasharray={tick === CAPACITY_WINDOW_MINUTES ? "6 5" : ""}
                strokeWidth={tick === CAPACITY_WINDOW_MINUTES ? 1.8 : 1}
              />
              <text
                x={margins.left - 8}
                y={yFor(tick) + 4}
                textAnchor="end"
                fontSize="12"
                fill="#334155"
              >
                {formatHourTick(tick)}
              </text>
            </g>
          ))}

          <line
            x1={margins.left}
            x2={margins.left}
            y1={margins.top}
            y2={margins.top + plotHeight}
            stroke="#cbd5e1"
          />

          {visibleDays.slice(0, -1).map((day, dayIndex) => {
            const x = daySeparatorXFor(dayIndex);

            return (
              <line
                key={`${day}-day-separator`}
                x1={x}
                x2={x}
                y1={margins.top}
                y2={margins.top + plotHeight}
                stroke="#64748b"
                strokeDasharray="2 5"
                strokeWidth="0.9"
                opacity="0.55"
              />
            );
          })}

          {visibleDays.map((day, dayIndex) => {
            if (day !== selectedDay) return null;

            return (
              <rect
                key={`selected-${day}`}
                x={groupStartFor(dayIndex) - 8}
                y={margins.top - 5}
                width={actualGroupWidth + 16}
                height={plotHeight + 10}
                rx="6"
                fill="#ffffff"
                fillOpacity="0.45"
                stroke={selectedHighlightColor}
                strokeOpacity="0.65"
                strokeWidth="1"
              />
            );
          })}

          <text
            x={yAxisTitleX}
            y={margins.top + plotHeight / 2}
            textAnchor="middle"
            fontSize="12"
            fill="#0f172a"
            transform={`rotate(-90 ${yAxisTitleX} ${margins.top + plotHeight / 2})`}
          >
            Time
          </text>

          {viewRows.map((row) => {
            const dayIndex = visibleDays.indexOf(row.run_date);
            const folder = folders[row.folderIndex];
            const x = xFor(dayIndex, row.folderIndex);
            let cursorY = yFor(0);

            return (
              <g key={`${row.run_date}-${row.folderKey}`} onClick={(event) => handleBarClick(event, row.run_date)} className="cursor-pointer">
                {row.segments.map((segment) => {
                  const segmentHeight = heightFor(segment.value);
                  const y = cursorY - segmentHeight;
                  cursorY = y;

                  if (segment.value <= 0) return null;

                  const fill = getSegmentFill(segment);
                  const sparePercent = row.isIdle ? 0 : calculatePercentage(row.spare_time, CAPACITY_WINDOW_MINUTES);
                  const runtimeSpeedText = formatEffectiveSpeed(segment.effective_speed);
                  const runtimeLabelText = segment.isComplex ? `▲ | ${runtimeSpeedText}` : runtimeSpeedText;
                  const canShowSpareLabel = segment.key === "spare_time" && sparePercent > 0;
                  const showSpareLabelAboveBar = canShowSpareLabel && segmentHeight < 22;
                  const spareLabel = `${Math.round(sparePercent)}%`;
                  const spareLabelY = Math.max(margins.top + 10, y - 8);
                  const canShowRuntimeLabel = segment.runtimeSegment && Number(segment.effective_speed || 0) > 0 && segmentHeight >= 30 && barWidth >= 10;
                  const textRotation = `rotate(-90 ${x + barWidth / 2} ${y + segmentHeight / 2})`;

                  return (
                    <g key={segment.key}>
                      <rect
                        x={x}
                        y={y}
                        width={barWidth}
                        height={segmentHeight}
                        fill={fill}
                        stroke={segment.key === "idle_time" ? "#cbd5e1" : "rgba(255,255,255,0.75)"}
                        strokeWidth="0.6"
                      >
                        <title>
                          {`${row.run_date} ${folder.alias}: ${segment.runtimeSegment ? "Run Time" : segment.label} ${formatMinutes(segment.value)}${segment.runtimeSegment ? `, ${runtimeSpeedText}` : ""}`}
                        </title>
                      </rect>
                      {canShowRuntimeLabel && (
                        <text
                          x={x + barWidth / 2}
                          y={y + segmentHeight / 2 + 3}
                          textAnchor="middle"
                          fontSize="12"
                          fontWeight="800"
                          fill={segment.textColor || "#14532d"}
                          transform={textRotation}
                          pointerEvents="none"
                        >
                          {runtimeLabelText}
                        </text>
                      )}
                      {canShowSpareLabel && showSpareLabelAboveBar && (
                        <g pointerEvents="none">
                          <rect
                            x={x + barWidth / 2 - 14}
                            y={spareLabelY - 13}
                            width="28"
                            height="16"
                            rx="3"
                            fill="#ffffff"
                            fillOpacity="0.88"
                            stroke="#cbd5e1"
                            strokeWidth="0.5"
                          />
                          <text
                            x={x + barWidth / 2}
                            y={spareLabelY}
                            textAnchor="middle"
                            fontSize="10"
                            fontWeight="800"
                            fill="#1e3a5f"
                          >
                            {spareLabel}
                          </text>
                        </g>
                      )}
                      {canShowSpareLabel && !showSpareLabelAboveBar && (
                        <text
                          x={x + barWidth / 2}
                          y={y + segmentHeight / 2 + 4}
                          textAnchor="middle"
                          fontSize="11"
                          fontWeight="800"
                          fill="#1e3a5f"
                          transform={textRotation}
                          pointerEvents="none"
                        >
                          {spareLabel}
                        </text>
                      )}
                    </g>
                  );
                })}
              </g>
            );
          })}

          {visibleDays.map((day, dayIndex) => {
            const groupCenter = margins.left + dayIndex * groupWidth + groupWidth / 2;
            return (
              <g key={day}>
                {folders.map((folder, folderIndex) => (
                  <text
                    key={`${day}-${folder.key}`}
                    x={xFor(dayIndex, folderIndex) + barWidth / 2}
                    y={margins.top + plotHeight + 18}
                    textAnchor="middle"
                    fontSize="11"
                    fontWeight="700"
                    fill={folder.color}
                  >
                    {folder.alias}
                  </text>
                ))}
                <text
                  x={groupCenter}
                  y={margins.top + plotHeight + 42}
                  textAnchor="middle"
                  fontSize="12"
                  fontWeight="700"
                  fill="#334155"
                >
                  {formatDayLabel(day)}
                </text>
              </g>
            );
          })}
        </svg>

        {summaryPopover && selectedDaySummary && (
          <CapacityDaySummary
            summary={selectedDaySummary}
            style={{ left: summaryPopover.left, top: summaryPopover.top }}
            onClose={() => setSummaryPopover(null)}
          />
        )}
      </div>
    </div>
  );
}

function CapacityDaySummary({ summary, style, onClose }) {
  return (
    <section
      className="absolute z-20 w-[380px] max-w-[calc(100%-1rem)] rounded-lg border border-slate-200 bg-white p-3 text-sm shadow-xl"
      style={style}
    >
      <div className="flex items-start justify-between gap-3 border-b border-slate-100 pb-2">
        <div>
          <p className="text-xs font-semibold uppercase tracking-normal text-slate-500">Selected day</p>
          <p className="mt-0.5 font-bold text-slate-950">{summary.dayLabel}</p>
        </div>
        <button
          type="button"
          aria-label="Close selected day summary"
          onClick={onClose}
          className="inline-flex h-7 w-7 shrink-0 items-center justify-center rounded-md text-slate-400 transition hover:bg-slate-100 hover:text-slate-700"
        >
          <X className="h-4 w-4" aria-hidden="true" />
        </button>
      </div>

      <div className="mt-2 space-y-1.5">
        {summary.components.map((component) => (
          <div key={component.key} className="flex items-center justify-between gap-3">
            <div className="flex min-w-0 items-center gap-2 font-medium text-slate-600">
              <span className="h-2.5 w-2.5 shrink-0 rounded-sm" style={{ backgroundColor: component.color }} />
              <span>{component.label}</span>
            </div>
            <span className="shrink-0 font-semibold text-slate-950">
              {formatCapacitySummaryValue(component.value, summary.totalCapacity)}
            </span>
          </div>
        ))}
      </div>

      <div className="mt-2 space-y-1.5 border-t border-slate-100 pt-2">
        <div className="flex items-center justify-between gap-3">
          <span className="font-medium text-slate-600">Active folders</span>
          <span className="font-semibold text-slate-950">{summary.activeFolders}/{summary.totalFolders}</span>
        </div>
        <div className="flex items-center justify-between gap-3">
          <span className="font-medium text-slate-600">Utilization</span>
          <span className="font-semibold text-slate-950">
            {formatCapacitySummaryValue(summary.utilization, summary.totalCapacity)}
          </span>
        </div>
      </div>
    </section>
  );
}

function EngagedTimeSelector({ options, selectedKeys, onToggle }) {
  return (
    <section className="rounded-lg border border-slate-200 bg-white p-3 shadow-soft">
      <div className="flex flex-col gap-3 xl:flex-row xl:items-center xl:justify-between">
        <div>
          <h3 className="text-sm font-semibold text-slate-950">Engaged time includes</h3>
          <p className="mt-1 text-xs text-slate-500">Select one or more components to define utilization in the breakdown charts.</p>
        </div>
        <div className="grid gap-2 sm:grid-cols-2 lg:grid-cols-3 xl:flex xl:flex-wrap xl:justify-end">
          {options.map((option) => {
            const selected = selectedKeys.includes(option.key);

            return (
              <label
                key={option.key}
                className={`inline-flex min-h-9 cursor-pointer items-center gap-2 rounded-lg border px-3 text-sm font-semibold transition ${
                  selected
                    ? "border-blue-300 bg-blue-50 text-slate-950"
                    : "border-slate-200 bg-white text-slate-500 hover:bg-slate-50 hover:text-slate-900"
                }`}
              >
                <input
                  type="checkbox"
                  checked={selected}
                  onChange={() => onToggle(option.key)}
                  className="h-4 w-4 shrink-0 rounded border-slate-300 text-blue-600 focus:ring-blue-500"
                />
                <span
                  className="h-2.5 w-2.5 rounded-full"
                  style={{ backgroundColor: option.color }}
                />
                {option.label}
              </label>
            );
          })}
        </div>
      </div>
    </section>
  );
}

function UtilizationBreakdownChart({
  title,
  subtitle,
  data,
  nameKey,
  engagedOptions,
  barSize,
  rowHeight,
  emptyMessage
}) {
  const chartHeight = Math.max(320, data.length * rowHeight + 56);

  const CustomYAxisTick = (props) => {
    const { x, y, payload } = props;
    const parts = payload.value.split("\n");

    if (parts.length === 2) {
      return (
        <g transform={`translate(${x},${y})`}>
          <text
            x={0}
            y={0}
            dy={4}
            textAnchor="end"
            fill="#628141"
            fontSize={11}
            fontWeight="600"
          >
            {parts[0]}
          </text>
          <text
            x={0}
            y={14}
            dy={4}
            textAnchor="end"
            fill="#B12C00"
            fontSize={11}
            fontWeight="500"
          >
            {parts[1]}
          </text>
        </g>
      );
    }

    return (
      <text
        x={x}
        y={y}
        dy={4}
        textAnchor="end"
        fill="#334155"
        fontSize={11}
      >
        {payload.value}
      </text>
    );
  };

  return (
    <section className="rounded-lg border border-slate-200 bg-white p-3 shadow-soft">
      <div className="mb-3">
        <h2 className="text-base font-semibold text-slate-950">{title}</h2>
        <p className="mt-1 text-sm text-slate-500">{subtitle}</p>
      </div>

      {data.length === 0 ? (
        <div className="flex h-72 items-center justify-center rounded-lg border border-dashed border-slate-200 text-sm text-slate-500">
          {emptyMessage}
        </div>
      ) : (
        <div className="rounded-lg border border-slate-100 bg-slate-50 p-1.5">
          <div className="w-full" style={{ height: chartHeight }}>
            <ResponsiveContainer>
              <BarChart data={data} layout="vertical" margin={{ top: 8, right: 24, left: 16, bottom: 8 }}>
                <CartesianGrid strokeDasharray="3 3" stroke="#94a3b8" horizontal={false} />
                <XAxis
                  type="number"
                  domain={[0, 100]}
                  tickFormatter={(value) => `${value}%`}
                  tick={{ fill: "#475569", fontSize: 12 }}
                  tickLine={false}
                  axisLine={{ stroke: "#cbd5e1" }}
                />
                <YAxis
                  type="category"
                  dataKey={nameKey}
                  width={140}
                  interval={0}
                  tick={<CustomYAxisTick />}
                  tickLine={false}
                  axisLine={{ stroke: "#cbd5e1" }}
                />
                <Tooltip content={<UtilizationTooltip nameKey={nameKey} engagedOptions={engagedOptions} />} cursor={{ fill: "rgba(15, 23, 42, 0.06)" }} />
                {engagedOptions.map((option, index) => (
                  <Bar
                    key={option.key}
                    dataKey={`${option.key}_percentage`}
                    name={option.label}
                    stackId="engaged"
                    fill={option.color}
                    barSize={barSize}
                    radius={index === engagedOptions.length - 1 ? [0, 4, 4, 0] : [0, 0, 0, 0]}
                  />
                ))}
              </BarChart>
            </ResponsiveContainer>
          </div>
        </div>
      )}
    </section>
  );
}

function UtilizationTooltip({ active, payload, nameKey, engagedOptions }) {
  if (!active || !payload?.length) return null;
  const row = payload[0].payload;

  return (
    <div className="rounded-lg border border-slate-200 bg-white p-3 text-sm shadow-soft">
      <p className="font-semibold text-slate-950">{row[nameKey]}</p>
      <div className="mt-2 space-y-1 text-slate-600">
        {engagedOptions.map((option) => (
          <TooltipRow
            key={option.key}
            label={option.label}
            value={`${formatMinutes(row[option.key])} (${formatPercent(row[`${option.key}_percentage`])})`}
            color={option.color}
          />
        ))}
        <div className="border-t border-slate-200 pt-2">
          <p>Engaged: {formatMinutes(row.runtime)}</p>
          <p>Available: {formatMinutes(row.available_capacity)}</p>
          <p>Utilization: {formatPercent(row.utilization_percentage)}</p>
        </div>
      </div>
    </div>
  );
}

function TooltipRow({ label, value, color }) {
  return (
    <div className="flex items-center gap-2">
      <span className="h-2.5 w-2.5 rounded-full" style={{ backgroundColor: color }} />
      <span>
        {label}: {value}
      </span>
    </div>
  );
}

function buildCapacitySplitModel(dailyRows, detailRows) {
  const days = [...(dailyRows || [])]
    .map((day) => day.run_date)
    .filter(Boolean)
    .sort();
  const folderKeys = Array.from(
    new Set((detailRows || []).map((row) => row.folder).filter(Boolean))
  ).sort(compareResourceNames);
  const folders = folderKeys.map((folderKey, index) => ({
    key: folderKey,
    alias: `M${index + 1}`,
    shortName: getFolderShortName(folderKey),
    color: FOLDER_ALIAS_COLORS[index % FOLDER_ALIAS_COLORS.length]
  }));
  const detailsByDayFolder = new Map(
    (detailRows || []).map((row) => [`${row.run_date}||${row.folder}`, row])
  );
  const rows = [];

  for (const runDate of days) {
    folders.forEach((folder, folderIndex) => {
      const detail = detailsByDayFolder.get(`${runDate}||${folder.key}`);

      if (!detail) {
        const idleValues = {
          waiting_time: 0,
          loss_time: 0,
          downtime: 0,
          runtime: 0,
          spare_time: 0,
          idle_time: CAPACITY_WINDOW_MINUTES
        };

        rows.push({
          run_date: runDate,
          folderKey: folder.key,
          folderIndex,
          isIdle: true,
          ...idleValues,
          segments: buildCapacitySegments(idleValues)
        });
        return;
      }

      const values = normalizeCapacityValues(detail);

      rows.push({
        run_date: runDate,
        folderKey: folder.key,
        folderIndex,
        isIdle: false,
        ...values,
        idle_time: 0,
        segments: buildCapacitySegments({ ...values, idle_time: 0 })
      });
    });
  }

  return { days, folders, rows };
}

function buildCapacityDaySummary(selectedDay, rows, totalFolders) {
  if (!selectedDay) return null;

  const dayRows = rows.filter((row) => row.run_date === selectedDay);
  if (!dayRows.length) return null;

  const activeRows = dayRows.filter((row) => !row.isIdle);
  const folderCount = Math.max(totalFolders, dayRows.length);
  const totalCapacity = folderCount * CAPACITY_WINDOW_MINUTES;
  const runtime = sumCapacityRows(activeRows, "runtime");
  const waitingTime = sumCapacityRows(activeRows, "waiting_time");
  const lossTime = sumCapacityRows(activeRows, "loss_time");
  const downtime = sumCapacityRows(activeRows, "downtime");
  const spareTime = sumCapacityRows(activeRows, "spare_time");

  return {
    run_date: selectedDay,
    dayLabel: formatDayLabel(selectedDay),
    activeFolders: activeRows.length,
    totalFolders: folderCount,
    totalCapacity,
    utilization: cleanNumber(runtime + lossTime + downtime),
    components: [
      {
        key: "runtime",
        label: "Run Time",
        value: runtime,
        color: CAPACITY_SPLIT_COLORS.runtime
      },
      {
        key: "waiting_time",
        label: "Wait Time",
        value: waitingTime,
        color: CAPACITY_SPLIT_COLORS.waiting_time
      },
      {
        key: "loss_time",
        label: "Loss Time",
        value: lossTime,
        color: CAPACITY_SPLIT_COLORS.loss_time
      },
      {
        key: "downtime",
        label: "Downtime",
        value: downtime,
        color: CAPACITY_SPLIT_COLORS.downtime
      },
      {
        key: "spare_time",
        label: "Spare time",
        value: spareTime,
        color: CAPACITY_SPLIT_COLORS.spare_time
      }
    ]
  };
}

function sumCapacityRows(rows, key) {
  return cleanNumber(rows.reduce((total, row) => total + Number(row[key] || 0), 0));
}

function getSegmentFill(segment) {
  if (segment.fill) return segment.fill;
  if (segment.key === "idle_time") return "url(#idlePattern)";
  return segment.color;
}

function buildCapacitySegments(values) {
  return [
    {
      key: "waiting_time",
      label: "Wait Time",
      value: values.waiting_time,
      color: CAPACITY_SPLIT_COLORS.waiting_time
    },
    {
      key: "loss_time",
      label: "Loss Time",
      value: values.loss_time,
      color: CAPACITY_SPLIT_COLORS.loss_time
    },
    {
      key: "downtime",
      label: "Downtime",
      value: values.downtime,
      color: CAPACITY_SPLIT_COLORS.downtime
    },
    ...buildRuntimeCapacitySegments(values.runtime_segments, values.runtime),
    {
      key: "spare_time",
      label: "Spare Time",
      value: values.spare_time,
      color: CAPACITY_SPLIT_COLORS.spare_time
    },
    {
      key: "idle_time",
      label: "Idle (not scheduled)",
      value: values.idle_time,
      color: CAPACITY_SPLIT_COLORS.idle_time
    }
  ];
}

function buildRuntimeCapacitySegments(runtimeSegments, fallbackRuntime) {
  const normalizedSegments = normalizeRuntimeSegments(runtimeSegments, fallbackRuntime);

  if (normalizedSegments.length === 0) {
    return [
      {
        key: "runtime",
        label: "Run Time",
        value: fallbackRuntime,
        color: CAPACITY_SPLIT_COLORS.runtime,
        runtimeSegment: true,
        textColor: "#14532d",
        effective_speed: 0
      }
    ];
  }

  return normalizedSegments.map((segment, index) => {
    const style = RUNTIME_SEGMENT_STYLES[segment.key] || RUNTIME_SEGMENT_STYLES.unknown;

    return {
      key: `runtime_${segment.key}_${index}`,
      label: segment.label || style.label,
      value: segment.minutes,
      color: style.color,
      fill: style.color,
      runtimeSegment: true,
      isComplex: Boolean(style.isComplex || segment.is_complex),
      textColor: style.textColor,
      effective_speed: segment.effective_speed,
      print_order: segment.print_order
    };
  });
}

function normalizeRuntimeSegments(runtimeSegments, targetRuntime) {
  const runtime = cleanNumber(Math.min(Math.max(Number(targetRuntime || 0), 0), CAPACITY_WINDOW_MINUTES));
  if (runtime <= 0 || !Array.isArray(runtimeSegments) || runtimeSegments.length === 0) {
    return [];
  }

  const positiveSegments = runtimeSegments
    .map((segment) => ({
      ...segment,
      minutes: cleanNumber(Math.max(Number(segment.minutes || 0), 0))
    }))
    .filter((segment) => segment.minutes > 0);
  const totalMinutes = cleanNumber(positiveSegments.reduce((total, segment) => total + segment.minutes, 0));

  if (totalMinutes <= 0) return [];

  const scale = runtime / totalMinutes;
  let remaining = runtime;

  return positiveSegments.map((segment, index) => {
    const nextSegment = { ...segment };

    if (index === positiveSegments.length - 1) {
      nextSegment.minutes = cleanNumber(Math.max(remaining, 0));
      return nextSegment;
    }

    nextSegment.minutes = cleanNumber(Math.min(segment.minutes * scale, remaining));
    remaining = cleanNumber(remaining - nextSegment.minutes);
    return nextSegment;
  }).filter((segment) => segment.minutes > 0);
}

function normalizeCapacityValues(detail) {
  const waitingTime = clampMinutes(detail.waiting_time);
  const lostTime = clampMinutes(detail.lost_time);
  const lossTime = clampMinutes(lostTime - waitingTime);
  const downtime = clampMinutes(detail.downtime);
  const runtime = clampMinutes(detail.runtime);
  const nonSpareValues = {
    waiting_time: waitingTime,
    loss_time: lossTime,
    downtime,
    runtime
  };
  const nonSpareTotal = Object.values(nonSpareValues).reduce((total, value) => total + value, 0);
  const spareTime = cleanNumber(Math.max(CAPACITY_WINDOW_MINUTES - nonSpareTotal, 0));
  const values = {
    ...nonSpareValues,
    spare_time: spareTime,
    runtime_segments: normalizeRuntimeSegments(detail.runtime_segments, runtime)
  };
  const total = cleanNumber(nonSpareTotal + spareTime);

  if (total <= CAPACITY_WINDOW_MINUTES) {
    return values;
  }

  let overage = cleanNumber(total - CAPACITY_WINDOW_MINUTES);
  const normalized = { ...values };

  for (const key of ["spare_time", "runtime", "downtime", "loss_time", "waiting_time"]) {
    if (overage <= 0) break;

    const reduction = Math.min(normalized[key], overage);
    normalized[key] = cleanNumber(normalized[key] - reduction);
    overage = cleanNumber(overage - reduction);
  }

  normalized.runtime_segments = normalizeRuntimeSegments(normalized.runtime_segments, normalized.runtime);

  return normalized;
}

function clampMinutes(value) {
  return Math.min(Math.max(Number(value || 0), 0), CAPACITY_WINDOW_MINUTES);
}

function getFolderShortName(folderKey) {
  const parts = String(folderKey || "")
    .split("\n")
    .map((part) => part.trim())
    .filter(Boolean);

  return parts[1] || parts[0] || "";
}

function formatHourTick(minutes) {
  const hours = Math.floor(Number(minutes || 0) / 60);
  const minutePart = String(Math.round(Number(minutes || 0) % 60)).padStart(2, "0");
  return `${String(hours).padStart(2, "0")}:${minutePart}`;
}

function formatDayLabel(dateStr) {
  const date = new Date(dateStr);
  return date.toLocaleDateString("en-US", { day: "2-digit", month: "short" });
}

function aggregateResourceUsage(rows, nameKey, selectedProductionDays, componentKeys, sortMode = "utilization") {
  if (!selectedProductionDays || rows.length === 0) return [];

  const grouped = new Map();

  for (const row of rows) {
    const name = row[nameKey];
    if (!name) continue;

    const current = grouped.get(name) || {
      [nameKey]: name,
      runtime: 0,
      available_capacity: selectedProductionDays * 240
    };

    for (const componentKey of componentKeys) {
      current[componentKey] = (current[componentKey] || 0) + Number(row[componentKey] || 0);
    }

    grouped.set(name, current);
  }

  return Array.from(grouped.values())
    .map((row) => {
      const runtime = componentKeys.reduce((total, componentKey) => total + Number(row[componentKey] || 0), 0);
      const utilizationPercentage = calculatePercentage(runtime, row.available_capacity);
      const componentPercentages = calculateStackPercentages(row, componentKeys, row.available_capacity);

      const nextRow = {
        ...row,
        runtime: cleanNumber(Math.min(runtime, row.available_capacity)),
        available_capacity: cleanNumber(row.available_capacity),
        utilization_percentage: utilizationPercentage
      };

      for (const componentKey of componentKeys) {
        const componentMinutes = Number(row[componentKey] || 0);
        nextRow[componentKey] = cleanNumber(Math.min(componentMinutes, row.available_capacity));
        nextRow[`${componentKey}_percentage`] = componentPercentages[componentKey] || 0;
      }

      return nextRow;
    })
    .sort((a, b) => {
      if (sortMode === "natural") {
        return compareResourceNames(a[nameKey], b[nameKey]);
      }

      return b.utilization_percentage - a.utilization_percentage || b.runtime - a.runtime;
    });
}

function calculateStackPercentages(row, componentKeys, availableCapacity) {
  const capacity = Number(availableCapacity || 0);
  if (capacity <= 0) {
    return Object.fromEntries(componentKeys.map((componentKey) => [componentKey, 0]));
  }

  const rawPercentages = componentKeys.map((componentKey) => {
    const componentMinutes = Math.max(Number(row[componentKey] || 0), 0);
    return {
      componentKey,
      percentage: Math.min((componentMinutes / capacity) * 100, 100)
    };
  });
  const rawTotal = rawPercentages.reduce((total, item) => total + item.percentage, 0);
  const scale = rawTotal > 100 ? 100 / rawTotal : 1;
  let remainingPercentage = 100;
  const normalized = {};

  rawPercentages.forEach((item, index) => {
    const scaledPercentage = item.percentage * scale;
    if (index === rawPercentages.length - 1) {
      normalized[item.componentKey] = cleanNumber(
        Math.min(Math.max(scaledPercentage, 0), Math.max(remainingPercentage, 0))
      );
      return;
    }

    const roundedPercentage = cleanNumber(
      Math.min(Math.max(scaledPercentage, 0), Math.max(remainingPercentage, 0))
    );
    normalized[item.componentKey] = roundedPercentage;
    remainingPercentage -= roundedPercentage;
  });

  return normalized;
}

function calculatePercentage(numerator, denominator) {
  const capacity = Number(denominator || 0);
  if (capacity <= 0) return 0;

  const percentage = (Number(numerator || 0) / capacity) * 100;
  return cleanNumber(Math.min(Math.max(percentage, 0), 100));
}

function compareResourceNames(first, second) {
  const firstParts = splitResourceName(first);
  const secondParts = splitResourceName(second);

  if (firstParts.prefix !== secondParts.prefix) {
    return firstParts.prefix.localeCompare(secondParts.prefix);
  }

  if (firstParts.number !== secondParts.number) {
    return firstParts.number - secondParts.number;
  }

  return String(first || "").localeCompare(String(second || ""));
}

function splitResourceName(value) {
  const text = String(value || "").trim();
  const match = /^(.*?)(\d+)(.*)$/.exec(text);

  if (!match) {
    return {
      prefix: text,
      number: Number.MAX_SAFE_INTEGER
    };
  }

  return {
    prefix: match[1].trim(),
    number: Number(match[2])
  };
}

function cleanNumber(value) {
  const numeric = Number.isFinite(Number(value)) ? Number(value) : 0;
  const rounded = Math.round(numeric * 100) / 100;
  return Number.isInteger(rounded) ? rounded : Number(rounded.toFixed(2));
}

function formatNumber(value) {
  return new Intl.NumberFormat("en-US", { maximumFractionDigits: 1 }).format(Number(value || 0));
}

function formatMinutes(value) {
  return `${formatNumber(value)} min`;
}

function formatCapacityMinutes(value) {
  return `${formatNumber(value)} mins`;
}

function formatCapacitySummaryValue(minutes, totalCapacity) {
  const percentage = totalCapacity > 0 ? (Number(minutes || 0) / totalCapacity) * 100 : 0;
  return `${formatCapacityMinutes(minutes)} (${formatFixedPercent(percentage)})`;
}

function formatEffectiveSpeed(value) {
  const speed = Number(value || 0);
  if (speed <= 0) return "0";

  if (speed >= 1000) {
    const thousands = speed / 1000;
    const rounded = thousands >= 100
      ? Math.round(thousands)
      : Math.round(thousands * 10) / 10;
    return `${rounded}k`;
  }

  return `${Math.round(speed)}`;
}

function formatFixedPercent(value) {
  const numeric = Math.min(Math.max(Number(value || 0), 0), 100);
  return `${numeric.toLocaleString("en-US", {
    minimumFractionDigits: 2,
    maximumFractionDigits: 2
  })}%`;
}

function formatPercent(value) {
  return `${formatNumber(Math.min(Math.max(Number(value || 0), 0), 100))}%`;
}
