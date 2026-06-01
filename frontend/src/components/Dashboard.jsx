import {
  Bar,
  BarChart,
  CartesianGrid,
  Legend,
  ResponsiveContainer,
  Tooltip,
  XAxis,
  YAxis
} from "recharts";
import { X } from "lucide-react";
import { useEffect, useMemo, useState } from "react";

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

const COMPLEXITY_CODES = Array.from({ length: 15 }, (_, index) => `C${index + 1}`);
const COMPLEXITY_METRIC_OPTIONS = [
  { key: "runtime", label: "Runtime" },
  { key: "change_over_time", label: "Changeover time" },
  { key: "downtime", label: "Downtime" },
  { key: "spare_time", label: "Spare time" }
];
const BOX_PLOT_FILL = "#cbd5e1";

function prepareDailyChartData(dailyData) {
  if (!dailyData || dailyData.length === 0) return dailyData;

  // If 31 or fewer days, show daily
  if (dailyData.length <= 31) {
    return dailyData.map((day) => ({
      ...day,
      display_label: formatDisplayDate(day.run_date),
      original_date: day.run_date
    }));
  }

  // Otherwise aggregate to weeks
  const weeks = [];
  let currentWeek = null;
  let weekData = null;

  for (const day of dailyData) {
    const date = new Date(day.run_date);
    const dayOfWeek = date.getDay();
    const dayOfMonth = date.getDate();
    const month = date.getMonth();
    const year = date.getFullYear();

    // Sunday = 0, so week starts on Sunday
    const daysFromSunday = dayOfWeek;
    const weekStart = new Date(year, month, dayOfMonth - daysFromSunday);
    const weekKey = weekStart.toISOString().split("T")[0];

    if (currentWeek !== weekKey) {
      if (weekData !== null) {
        weeks.push(weekData);
      }

      const weekEnd = new Date(weekStart);
      weekEnd.setDate(weekEnd.getDate() + 6);

      currentWeek = weekKey;
      weekData = {
        run_date: weekStart.toISOString().split("T")[0],
        display_label: `${formatDisplayDate(weekStart.toISOString().split("T")[0])} - ${formatDisplayDate(weekEnd.toISOString().split("T")[0])}`,
        original_date: weekStart.toISOString().split("T")[0],
        runtime: 0,
        lost_time: 0,
        downtime: 0,
        buffer_time: 0,
        available_capacity: 0,
        active_folders_count: 0,
        capacity_folders_count: 0,
        utilization_percentage: 0
      };
    }

    // Aggregate metrics
    weekData.runtime += day.runtime;
    weekData.lost_time += day.lost_time;
    weekData.downtime += day.downtime;
    weekData.buffer_time += day.buffer_time;
    weekData.available_capacity += day.available_capacity;
    weekData.active_folders_count += day.active_folders_count;
    weekData.capacity_folders_count += day.capacity_folders_count || day.active_folders_count || 0;
  }

  if (weekData !== null) {
    weeks.push(weekData);
  }

  // Recalculate utilization percentage based on aggregated values
  return weeks.map((week) => ({
    ...week,
    utilization_percentage: calculatePercentage(week.runtime, week.available_capacity)
  }));
}

function formatDisplayDate(dateStr) {
  const date = new Date(dateStr);
  return date.toLocaleDateString("en-US", { month: "short", day: "numeric" });
}

function buildCapacityTicks(rows) {
  const maxValue = Math.max(
    120,
    ...(rows || []).map((row) =>
      Number(row.runtime || 0)
      + Number(row.lost_time || 0)
      + Number(row.downtime || 0)
      + Number(row.buffer_time || 0)
    )
  );
  const maxTick = Math.ceil(maxValue / 120) * 120;

  return Array.from(
    { length: Math.floor(maxTick / 120) + 1 },
    (_, index) => index * 120
  );
}

export default function Dashboard({ data }) {
  const [focusedDay, setFocusedDay] = useState("");
  const [engagedComponentKeys, setEngagedComponentKeys] = useState(["runtime"]);
  const [activeBreakdownTab, setActiveBreakdownTab] = useState("utilization");
  const [complexityMetricKey, setComplexityMetricKey] = useState("runtime");

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

  const breakdownComplexityTiming = useMemo(
    () =>
      focusedDay
        ? (data.complexity_timing || []).filter((row) => row.run_date === focusedDay)
        : data.complexity_timing || [],
    [data.complexity_timing, focusedDay]
  );

  const breakdownProductionDays = focusedDay ? 1 : data.daily.length;
  
  const dailyChartData = useMemo(
    () => prepareDailyChartData(data.daily),
    [data.daily]
  );
  const dailyCapacityTicks = useMemo(
    () => buildCapacityTicks(dailyChartData),
    [dailyChartData]
  );

  const towerBreakdown = useMemo(
    () => aggregateResourceUsage(breakdownTowerDetails, "tower", breakdownProductionDays, engagedComponentKeys, "natural"),
    [breakdownTowerDetails, breakdownProductionDays, engagedComponentKeys]
  );
  const folderBreakdown = useMemo(
    () => aggregateResourceUsage(breakdownDetails, "folder", breakdownProductionDays, engagedComponentKeys),
    [breakdownDetails, breakdownProductionDays, engagedComponentKeys]
  );
  const complexityBoxData = useMemo(
    () => buildComplexityBoxData(breakdownComplexityTiming, complexityMetricKey),
    [breakdownComplexityTiming, complexityMetricKey]
  );
  const selectedComplexityMetric = useMemo(
    () =>
      COMPLEXITY_METRIC_OPTIONS.find((option) => option.key === complexityMetricKey)
      || COMPLEXITY_METRIC_OPTIONS[0],
    [complexityMetricKey]
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
    ["Average Utilization", formatPercent(data.summary.average_utilization_percentage), "blue"],
    ["Active Folder-Days", formatNumber(data.summary.active_folder_days), "slate"]
  ];

  return (
    <div className="mt-6 space-y-6">
      <section className="grid gap-4 sm:grid-cols-2 lg:grid-cols-4">
        {kpis.map(([label, value, tone]) => (
          <KpiCard key={label} label={label} value={value} tone={tone} />
        ))}
      </section>

      <section className="rounded-lg border border-slate-200 bg-white p-4 shadow-soft">
        <div className="mb-4 flex flex-col gap-2 sm:flex-row sm:items-end sm:justify-between">
          <div>
            <h2 className="text-base font-semibold text-slate-950">Daily capacity split</h2>
            <p className="mt-1 text-sm text-slate-500">Runtime, lost time, downtime, and spare time by Run Date</p>
          </div>
          <div className="text-sm text-slate-500">
            Breakdowns: <span className="font-semibold text-slate-800">{breakdownScope}</span>
          </div>
        </div>

        <div className="h-[360px] w-full">
          <ResponsiveContainer>
            <BarChart
              data={dailyChartData}
              margin={{ top: 12, right: 20, left: 8, bottom: 60 }}
              onClick={(event) => {
                if (event?.activeTooltipIndex !== undefined && dailyChartData[event.activeTooltipIndex]) {
                  setFocusedDay(dailyChartData[event.activeTooltipIndex].original_date);
                }
              }}
            >
              <CartesianGrid strokeDasharray="3 3" stroke="#e2e8f0" vertical={false} />
              <XAxis
                dataKey="display_label"
                tick={{ fill: "#475569", fontSize: 11 }}
                tickLine={false}
                axisLine={{ stroke: "#cbd5e1" }}
                angle={-45}
                textAnchor="end"
                height={80}
              />
              <YAxis
                domain={[0, dailyCapacityTicks[dailyCapacityTicks.length - 1]]}
                ticks={dailyCapacityTicks}
                tick={{ fill: "#475569", fontSize: 12 }}
                tickLine={false}
                axisLine={{ stroke: "#cbd5e1" }}
                width={72}
              />
              <Tooltip content={<CapacityTooltip />} cursor={{ fill: "rgba(37, 99, 235, 0.08)" }} />
              <Legend wrapperStyle={{ paddingTop: 10 }} />
              <Bar dataKey="runtime" name="Runtime" stackId="capacity" fill={COLORS.runtime} />
              <Bar dataKey="lost_time" name="Lost Time" stackId="capacity" fill={COLORS.lost_time} />
              <Bar dataKey="downtime" name="Downtime / Breakdown" stackId="capacity" fill={COLORS.downtime} />
              <Bar dataKey="buffer_time" name="Spare Time" stackId="capacity" fill={COLORS.buffer_time} />
            </BarChart>
          </ResponsiveContainer>
        </div>
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

        <div className="flex flex-wrap gap-2 rounded-lg border border-slate-200 bg-white p-1 shadow-soft">
          <TabButton
            active={activeBreakdownTab === "utilization"}
            onClick={() => setActiveBreakdownTab("utilization")}
          >
            Tower and folder utilization
          </TabButton>
          <TabButton
            active={activeBreakdownTab === "complexity"}
            onClick={() => setActiveBreakdownTab("complexity")}
          >
            Complexity timing
          </TabButton>
        </div>

        {activeBreakdownTab === "utilization" ? (
          <>
            <EngagedTimeSelector
              options={ENGAGED_TIME_OPTIONS}
              selectedKeys={engagedComponentKeys}
              onToggle={toggleEngagedComponent}
            />

            <div className="grid gap-6 xl:grid-cols-2">
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
          </>
        ) : (
          <ComplexityBoxPlotChart
            data={complexityBoxData}
            metric={selectedComplexityMetric}
            metricOptions={COMPLEXITY_METRIC_OPTIONS}
            onMetricChange={setComplexityMetricKey}
            scopeLabel={focusedDay ? `Selected day: ${focusedDay}` : "Selected timeframe"}
          />
        )}
      </section>
    </div>
  );
}

function TabButton({ active, children, onClick }) {
  return (
    <button
      type="button"
      onClick={onClick}
      className={`min-h-9 rounded-md px-4 text-sm font-semibold transition ${
        active
          ? "bg-slate-950 text-white shadow-sm"
          : "text-slate-500 hover:bg-slate-50 hover:text-slate-950"
      }`}
    >
      {children}
    </button>
  );
}

function ComplexityBoxPlotChart({ data, metric, metricOptions, onMetricChange, scopeLabel }) {
  const hasData = data.some((row) => row.count > 0);
  const width = 1160;
  const height = 440;
  const margins = { top: 32, right: 32, bottom: 72, left: 78 };
  const plotWidth = width - margins.left - margins.right;
  const plotHeight = height - margins.top - margins.bottom;
  const maxValue = Math.max(0, ...data.map((row) => (row.count > 0 ? row.max : 0)));
  const ticks = buildRuntimeTicks(maxValue);
  const yMax = ticks[ticks.length - 1] || 10;
  const yMin = -Math.max(yMax * 0.06, 2);
  const yRange = yMax - yMin;
  const xInset = 44;
  const xStep = (plotWidth - xInset * 2) / (COMPLEXITY_CODES.length - 1);
  const boxWidth = 34;

  function xAt(index) {
    return margins.left + xInset + index * xStep;
  }

  function yAt(value) {
    return margins.top + plotHeight - ((Number(value || 0) - yMin) / yRange) * plotHeight;
  }

  return (
    <section className="rounded-lg border border-slate-200 bg-white p-4 shadow-soft">
      <div className="mb-4 flex flex-col gap-2 sm:flex-row sm:items-start sm:justify-between">
        <div>
          <h2 className="text-base font-semibold text-slate-950">Complexity timing distribution</h2>
          <p className="mt-1 text-sm text-slate-500">{metric.label} distribution by complexity with edition printing count</p>
        </div>
        <div className="flex flex-col gap-2 sm:items-end">
          <div className="inline-flex flex-wrap gap-1 rounded-lg border border-slate-200 bg-slate-50 p-1">
            {metricOptions.map((option) => (
              <button
                key={option.key}
                type="button"
                onClick={() => onMetricChange(option.key)}
                className={`min-h-8 rounded-md px-3 text-xs font-semibold transition ${
                  metric.key === option.key
                    ? "bg-white text-slate-950 shadow-sm"
                    : "text-slate-500 hover:bg-white/70 hover:text-slate-900"
                }`}
              >
                {option.label}
              </button>
            ))}
          </div>
          <span className="rounded-md border border-slate-200 bg-slate-50 px-3 py-1 text-xs font-semibold text-slate-600">
            {scopeLabel}
          </span>
        </div>
      </div>

      {!hasData ? (
        <div className="flex h-72 items-center justify-center rounded-lg border border-dashed border-slate-200 text-sm text-slate-500">
          No complexity timing data found for this selection.
        </div>
      ) : (
        <div className="rounded-lg border border-slate-100 bg-slate-50 p-3">
          <svg
            className="h-[440px] w-full"
            role="img"
            aria-label={`Box and whisker plot of ${metric.label.toLowerCase()} by complexity with edition printing counts`}
            viewBox={`0 0 ${width} ${height}`}
          >
            <rect x="0" y="0" width={width} height={height} rx="10" fill="#f8fafc" />
            {ticks.map((tick) => (
              <g key={tick}>
                <line
                  x1={margins.left}
                  x2={width - margins.right}
                  y1={yAt(tick)}
                  y2={yAt(tick)}
                  stroke="#e2e8f0"
                  strokeDasharray={tick === 0 ? "" : "4 4"}
                />
                <text
                  x={margins.left - 14}
                  y={yAt(tick) + 4}
                  textAnchor="end"
                  fontSize="12"
                  fill="#64748b"
                >
                  {tick}
                </text>
              </g>
            ))}

            <line x1={margins.left} x2={margins.left} y1={margins.top} y2={height - margins.bottom} stroke="#0f172a" strokeWidth="2" />
            <line x1={margins.left} x2={width - margins.right} y1={height - margins.bottom} y2={height - margins.bottom} stroke="#0f172a" strokeWidth="2" />

            {data.map((row, index) => {
              const x = xAt(index);
              const yMin = yAt(row.min);
              const yQ1 = yAt(row.q1);
              const yMedian = yAt(row.median);
              const yQ3 = yAt(row.q3);
              const yMaxValue = yAt(row.max);
              const countHasSpaceAbove = yMaxValue - 12 >= margins.top + 14;
              const countY = countHasSpaceAbove
                ? yMaxValue - 12
                : Math.min(height - margins.bottom - 14, yMin + 18);
              return (
                <g key={row.complexity}>
                  <line
                    x1={x}
                    x2={x}
                    y1={height - margins.bottom - 3}
                    y2={height - margins.bottom + 3}
                    stroke="#94a3b8"
                    strokeWidth="1.5"
                  />
                  <text
                    x={x}
                    y={height - margins.bottom + 34}
                    textAnchor="middle"
                    fontSize="12"
                    fontWeight="600"
                    fill="#0f172a"
                  >
                    {row.complexity}
                  </text>

                  {row.count > 0 && (
                    <g>
                      <title>
                        {`${row.complexity}: ${formatNumber(row.count)} edition printing${row.count === 1 ? "" : "s"}, median ${metric.label.toLowerCase()} ${formatMinutes(row.median)}, ${metric.label.toLowerCase()} range ${formatMinutes(row.min)}-${formatMinutes(row.max)}`}
                      </title>
                      <text
                        x={x}
                        y={countY}
                        textAnchor="middle"
                        fontSize="12"
                        fontWeight="700"
                        fill="#334155"
                      >
                        {row.count}
                      </text>
                      <line x1={x} x2={x} y1={yMaxValue} y2={yMin} stroke="#94a3b8" strokeWidth="3" />
                      <line x1={x - boxWidth * 0.35} x2={x + boxWidth * 0.35} y1={yMaxValue} y2={yMaxValue} stroke="#94a3b8" strokeWidth="3" />
                      <line x1={x - boxWidth * 0.35} x2={x + boxWidth * 0.35} y1={yMin} y2={yMin} stroke="#94a3b8" strokeWidth="3" />
                      <rect
                        x={x - boxWidth / 2}
                        y={yQ3}
                        width={boxWidth}
                        height={Math.max(yQ1 - yQ3, 3)}
                        rx="4"
                        fill={BOX_PLOT_FILL}
                        stroke="#64748b"
                        strokeWidth="1.5"
                      />
                      <line x1={x - boxWidth / 2} x2={x + boxWidth / 2} y1={yMedian} y2={yMedian} stroke="#64748b" strokeWidth="3" />
                    </g>
                  )}
                </g>
              );
            })}

            <text
              x={margins.left - 52}
              y={margins.top + plotHeight / 2}
              textAnchor="middle"
              fontSize="13"
              fontWeight="700"
              fill="#0f172a"
              transform={`rotate(-90 ${margins.left - 52} ${margins.top + plotHeight / 2})`}
            >
              {metric.label} (min)
            </text>
            <text
              x={margins.left + plotWidth / 2}
              y={height - 16}
              textAnchor="middle"
              fontSize="13"
              fontWeight="700"
              fill="#0f172a"
            >
              Complexity
            </text>
            <g transform={`translate(${width - 250}, 18)`}>
              <rect x="0" y="0" width="12" height="12" rx="2" fill={BOX_PLOT_FILL} stroke="#64748b" />
              <text x="20" y="11" fontSize="12" fill="#475569">{metric.label} distribution</text>
            </g>
          </svg>
        </div>
      )}
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
    <section className="rounded-lg border border-slate-200 bg-white p-4 shadow-soft">
      <div className="mb-4">
        <h2 className="text-base font-semibold text-slate-950">{title}</h2>
        <p className="mt-1 text-sm text-slate-500">{subtitle}</p>
      </div>

      {data.length === 0 ? (
        <div className="flex h-72 items-center justify-center rounded-lg border border-dashed border-slate-200 text-sm text-slate-500">
          {emptyMessage}
        </div>
      ) : (
        <div className="rounded-lg border border-slate-100 bg-slate-50 p-2">
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

function FolderMachinePanel({ day, details, open, onClose }) {
  if (!open || !day || details.length === 0) return null;

  const chartHeight = Math.max(280, details.length * 58 + 92);

  return (
    <div className="fixed inset-0 z-50">
      <button
        type="button"
        aria-label="Close machine-folder panel"
        className="absolute inset-0 h-full w-full bg-slate-950/30"
        onClick={onClose}
      />

      <section className="absolute bottom-4 left-4 right-4 top-4 flex flex-col rounded-lg border border-slate-200 bg-white shadow-2xl md:left-auto md:w-[760px]">
        <div className="flex items-start justify-between gap-4 border-b border-slate-200 px-5 py-4">
          <div>
            <h2 className="text-base font-semibold text-slate-950">Machine-folder time consumption</h2>
            <p className="mt-1 text-sm text-slate-500">
              {day.run_date} · {formatNumber(details.length)} active folder{details.length === 1 ? "" : "s"}
            </p>
          </div>
          <button
            type="button"
            aria-label="Close panel"
            onClick={onClose}
            className="inline-flex h-9 w-9 shrink-0 items-center justify-center rounded-lg border border-slate-200 text-slate-500 transition hover:bg-slate-50 hover:text-slate-800"
          >
            <X className="h-4 w-4" aria-hidden="true" />
          </button>
        </div>

        <div className="min-h-0 flex-1 overflow-y-auto p-5">
          <div className="w-full" style={{ height: chartHeight }}>
            <ResponsiveContainer>
              <BarChart
                data={details}
                layout="vertical"
                margin={{ top: 8, right: 24, left: 12, bottom: 8 }}
              >
                <CartesianGrid strokeDasharray="3 3" stroke="#e2e8f0" horizontal={false} />
                <XAxis
                  type="number"
                  tick={{ fill: "#475569", fontSize: 12 }}
                  tickLine={false}
                  axisLine={{ stroke: "#cbd5e1" }}
                />
                <YAxis
                  type="category"
                  dataKey="label"
                  width={170}
                  tick={{ fill: "#334155", fontSize: 12 }}
                  tickLine={false}
                  axisLine={{ stroke: "#cbd5e1" }}
                />
                <Tooltip content={<FolderTooltip />} cursor={{ fill: "rgba(37, 99, 235, 0.08)" }} />
                <Legend wrapperStyle={{ paddingTop: 10 }} />
                <Bar dataKey="runtime" name="Runtime" stackId="folder" fill={COLORS.runtime} />
                <Bar dataKey="lost_time" name="Lost Time" stackId="folder" fill={COLORS.lost_time} />
                <Bar dataKey="downtime" name="Downtime / Breakdown" stackId="folder" fill={COLORS.downtime} />
                <Bar dataKey="buffer_time" name="Spare Time" stackId="folder" fill={COLORS.buffer_time} />
              </BarChart>
            </ResponsiveContainer>
          </div>
        </div>
      </section>
    </div>
  );
}

function FolderTooltip({ active, payload }) {
  if (!active || !payload?.length) return null;
  const row = payload[0].payload;

  return (
    <div className="rounded-lg border border-slate-200 bg-white p-3 text-sm shadow-soft">
      <p className="font-semibold text-slate-950">{row.machine}</p>
      <p className="text-slate-500">{row.folder}</p>
      <div className="mt-2 space-y-1 text-slate-600">
        <TooltipRow label="Runtime" value={formatMinutes(row.runtime)} color={COLORS.runtime} />
        <TooltipRow label="Lost Time" value={formatMinutes(row.lost_time)} color={COLORS.lost_time} />
        <TooltipRow label="Downtime" value={formatMinutes(row.downtime)} color={COLORS.downtime} />
        <TooltipRow label="Spare Time" value={formatMinutes(row.buffer_time)} color={COLORS.buffer_time} />
        <div className="border-t border-slate-200 pt-2">
          <p>Available Capacity: {formatMinutes(row.available_capacity)}</p>
          <p>Changeover: {formatMinutes(row.change_over_time)}</p>
          <p>Late Start: {formatMinutes(row.late_start_time)}</p>
        </div>
      </div>
    </div>
  );
}

function CapacityTooltip({ active, payload, label }) {
  if (!active || !payload?.length) return null;
  const day = payload[0].payload;
  const capacityFolders = Number(day.capacity_folders_count || 0);
  const activeFolders = Number(day.active_folders_count || 0);

  return (
    <div className="rounded-lg border border-slate-200 bg-white p-3 text-sm shadow-soft">
      <p className="font-semibold text-slate-950">{day.display_label}</p>
      <div className="mt-2 space-y-1 text-slate-600">
        <TooltipRow label="Runtime" value={formatMinutes(day.runtime)} color={COLORS.runtime} />
        <TooltipRow label="Lost Time" value={formatMinutes(day.lost_time)} color={COLORS.lost_time} />
        <TooltipRow label="Downtime" value={formatMinutes(day.downtime)} color={COLORS.downtime} />
        <TooltipRow label="Spare Time" value={formatMinutes(day.buffer_time)} color={COLORS.buffer_time} />
        <div className="border-t border-slate-200 pt-2">
          <p>Available Capacity: {formatMinutes(day.available_capacity)}</p>
          <p>
            Active Folders: {formatNumber(activeFolders)}
            {capacityFolders > activeFolders ? ` / ${formatNumber(capacityFolders)}` : ""}
          </p>
          <p>Utilization: {formatPercent(day.utilization_percentage)}</p>
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

function DailyTable({ daily, selectedDay, onSelectDay }) {
  return (
    <section className="rounded-lg border border-slate-200 bg-white shadow-soft">
      <div className="border-b border-slate-200 px-4 py-3">
        <h2 className="text-base font-semibold text-slate-950">Daily summary</h2>
      </div>
      <div className="overflow-x-auto">
        <table className="min-w-full divide-y divide-slate-200 text-sm">
          <thead className="bg-slate-50 text-left text-xs font-semibold uppercase tracking-normal text-slate-500">
            <tr>
              <th className="px-4 py-3">Run Date</th>
              <th className="px-4 py-3 text-right">Capacity</th>
              <th className="px-4 py-3 text-right">Runtime</th>
              <th className="px-4 py-3 text-right">Lost</th>
              <th className="px-4 py-3 text-right">Down</th>
              <th className="px-4 py-3 text-right">Spare</th>
              <th className="px-4 py-3 text-right">Util.</th>
            </tr>
          </thead>
          <tbody className="divide-y divide-slate-100">
            {daily.map((day) => (
              <tr
                key={day.run_date}
                onClick={() => onSelectDay(day.run_date)}
                className={`cursor-pointer transition hover:bg-blue-50 ${
                  selectedDay === day.run_date ? "bg-blue-50" : "bg-white"
                }`}
              >
                <td className="whitespace-nowrap px-4 py-3 font-medium text-slate-900">{day.run_date}</td>
                <td className="px-4 py-3 text-right text-slate-700">{formatMinutes(day.available_capacity)}</td>
                <td className="px-4 py-3 text-right text-slate-700">{formatMinutes(day.runtime)}</td>
                <td className="px-4 py-3 text-right text-slate-700">{formatMinutes(day.lost_time)}</td>
                <td className="px-4 py-3 text-right text-slate-700">{formatMinutes(day.downtime)}</td>
                <td className="px-4 py-3 text-right text-slate-700">{formatMinutes(day.buffer_time)}</td>
                <td className="px-4 py-3 text-right text-slate-700">{formatPercent(day.utilization_percentage)}</td>
              </tr>
            ))}
          </tbody>
        </table>
      </div>
    </section>
  );
}

function DetailsTable({ day, details }) {
  return (
    <section className="rounded-lg border border-slate-200 bg-white shadow-soft">
      <div className="flex items-center justify-between border-b border-slate-200 px-4 py-3">
        <h2 className="text-base font-semibold text-slate-950">Folder breakdown</h2>
        {day && <span className="text-sm text-slate-500">{day.run_date}</span>}
      </div>
      <div className="overflow-x-auto">
        <table className="min-w-full divide-y divide-slate-200 text-sm">
          <thead className="bg-slate-50 text-left text-xs font-semibold uppercase tracking-normal text-slate-500">
            <tr>
              <th className="px-4 py-3">Machine</th>
              <th className="px-4 py-3">Folder</th>
              <th className="px-4 py-3 text-right">Runtime</th>
              <th className="px-4 py-3 text-right">Lost</th>
              <th className="px-4 py-3 text-right">Down</th>
              <th className="px-4 py-3 text-right">Spare</th>
            </tr>
          </thead>
          <tbody className="divide-y divide-slate-100">
            {details.map((row) => (
              <tr key={`${row.run_date}-${row.machine}-${row.folder}`}>
                <td className="whitespace-nowrap px-4 py-3 font-medium text-slate-900">{row.machine}</td>
                <td className="whitespace-nowrap px-4 py-3 text-slate-700">{row.folder}</td>
                <td className="px-4 py-3 text-right text-slate-700">{formatMinutes(row.runtime)}</td>
                <td className="px-4 py-3 text-right text-slate-700">{formatMinutes(row.lost_time)}</td>
                <td className="px-4 py-3 text-right text-slate-700">{formatMinutes(row.downtime)}</td>
                <td className="px-4 py-3 text-right text-slate-700">{formatMinutes(row.buffer_time)}</td>
              </tr>
            ))}
          </tbody>
        </table>
      </div>
    </section>
  );
}

function buildComplexityBoxData(rows, metricKey) {
  const grouped = new Map(
    COMPLEXITY_CODES.map((complexity) => [
      complexity,
      {
        values: [],
        count: 0
      }
    ])
  );

  for (const row of rows || []) {
    const complexity = String(row.complexity || "").trim().toUpperCase();
    if (!grouped.has(complexity)) continue;

    const metricValue = Number(row[metricKey] || 0);
    const group = grouped.get(complexity);

    group.count += 1;
    if (Number.isFinite(metricValue)) {
      group.values.push(metricValue);
    }
  }

  return COMPLEXITY_CODES.map((complexity) => {
    const group = grouped.get(complexity);
    const metricValues = [...group.values].sort((a, b) => a - b);

    if (metricValues.length === 0) {
      return {
        complexity,
        count: group.count,
        min: 0,
        q1: 0,
        median: 0,
        q3: 0,
        max: 0
      };
    }

    return {
      complexity,
      count: group.count,
      min: cleanNumber(metricValues[0]),
      q1: cleanNumber(percentile(metricValues, 0.25)),
      median: cleanNumber(percentile(metricValues, 0.5)),
      q3: cleanNumber(percentile(metricValues, 0.75)),
      max: cleanNumber(metricValues[metricValues.length - 1])
    };
  });
}

function percentile(sortedValues, percentileValue) {
  if (!sortedValues.length) return 0;

  const position = (sortedValues.length - 1) * percentileValue;
  const lowerIndex = Math.floor(position);
  const upperIndex = Math.ceil(position);

  if (lowerIndex === upperIndex) {
    return sortedValues[lowerIndex];
  }

  const weight = position - lowerIndex;
  return sortedValues[lowerIndex] * (1 - weight) + sortedValues[upperIndex] * weight;
}

function buildRuntimeTicks(maxValue) {
  const safeMax = Math.max(Number(maxValue) || 0, 1);
  const paddedMax = safeMax * 1.15;
  const roughStep = paddedMax / 5;
  const magnitude = 10 ** Math.floor(Math.log10(roughStep));
  const normalizedStep = roughStep / magnitude;
  const stepMultiplier =
    normalizedStep <= 1 ? 1 : normalizedStep <= 2 ? 2 : normalizedStep <= 5 ? 5 : 10;
  const step = stepMultiplier * magnitude;
  const upper = Math.max(step, Math.ceil(paddedMax / step) * step);

  return Array.from(
    { length: Math.round(upper / step) + 1 },
    (_, index) => index * step
  );
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

function formatPercent(value) {
  return `${formatNumber(Math.min(Math.max(Number(value || 0), 0), 100))}%`;
}
