import { useMemo } from "react";
import { RadioTower } from "lucide-react";

const CAPACITY_WINDOW_MINUTES = 240;
const AVAILABILITY_THRESHOLDS = [70, 80, 90, 100];

export default function TowerAvailabilitySummaryWidget({ towerDetails, daily }) {
  const summary = useMemo(
    () => buildTowerAvailabilitySummary(towerDetails || [], daily || []),
    [towerDetails, daily]
  );

  if (!towerDetails || towerDetails.length === 0 || summary.totalDays === 0 || summary.totalTowers === 0) {
    return null;
  }

  return (
    <div className="mt-4 border-t border-slate-100 pt-4">
      <div className="mb-3 flex items-start gap-2">
        <span className="inline-flex h-8 w-8 shrink-0 items-center justify-center rounded-lg bg-sky-50 text-sky-700">
          <RadioTower className="h-4 w-4" aria-hidden="true" />
        </span>
        <div>
          <h3 className="text-base font-semibold text-slate-950">
            Tower Availability Summary
          </h3>
          <p className="mt-1 text-sm text-slate-500">
            {formatNumber(summary.totalTowers)} towers tracked across {formatNumber(summary.totalDays)} day{summary.totalDays === 1 ? "" : "s"}.
          </p>
        </div>
      </div>

      <div className="grid gap-4 lg:grid-cols-[minmax(0,1fr)_260px]">
        <div className="overflow-hidden rounded-lg border border-slate-100">
          <table className="w-full min-w-[420px] text-sm">
            <thead className="bg-slate-50">
              <tr className="border-b border-slate-100 text-left text-xs font-semibold uppercase tracking-normal text-slate-500">
                <th className="px-3 py-2">Tower availability</th>
                <th className="px-3 py-2">Days</th>
                <th className="px-3 py-2">Share</th>
              </tr>
            </thead>
            <tbody className="divide-y divide-slate-50 bg-white">
              {summary.thresholdRows.map((row) => (
                <tr key={row.threshold}>
                  <td className="px-3 py-2 font-medium text-slate-800">
                    {row.threshold}% towers active
                  </td>
                  <td className="px-3 py-2 text-slate-700">
                    {formatNumber(row.days)} / {formatNumber(summary.totalDays)}
                  </td>
                  <td className="px-3 py-2">
                    <div className="flex items-center gap-2">
                      <div className="h-2 w-24 overflow-hidden rounded-full bg-slate-100">
                        <div
                          className="h-full rounded-full bg-sky-600"
                          style={{ width: `${row.share}%` }}
                        />
                      </div>
                      <span className="w-10 text-xs font-semibold text-slate-600">
                        {formatPercent(row.share)}
                      </span>
                    </div>
                  </td>
                </tr>
              ))}
            </tbody>
          </table>
        </div>

        <div className="grid gap-2 sm:grid-cols-2 lg:grid-cols-1">
          <TowerMetric
            label="Median tower utilization"
            value={formatPercent(summary.medianUtilization)}
          />
          <TowerMetric
            label="Lowest tower utilization"
            value={formatPercent(summary.lowestUtilization)}
            detail={summary.lowestTower}
          />
        </div>
      </div>
    </div>
  );
}

function TowerMetric({ label, value, detail }) {
  return (
    <div className="rounded-lg border border-slate-100 bg-slate-50 px-3 py-2">
      <div className="text-[11px] font-semibold uppercase tracking-normal text-slate-500">
        {label}
      </div>
      <div className="mt-1 text-lg font-bold text-slate-950">
        {value}
      </div>
      {detail && (
        <div className="mt-0.5 truncate text-xs font-medium text-slate-500" title={detail}>
          {formatTowerName(detail)}
        </div>
      )}
    </div>
  );
}

function buildTowerAvailabilitySummary(towerDetails, dailyRows) {
  const days = getSelectedDays(towerDetails, dailyRows);
  const totalDays = days.length;
  const daySet = new Set(days);
  const towers = Array.from(new Set(
    towerDetails
      .map((row) => row.tower)
      .filter(Boolean)
  )).sort((first, second) => String(first).localeCompare(String(second)));
  const totalTowers = towers.length;
  const activeTowersByDay = new Map();
  const towerDayEngaged = new Map();

  for (const row of towerDetails) {
    if (!row.run_date || !daySet.has(row.run_date) || !row.tower) continue;

    const activeSet = activeTowersByDay.get(row.run_date) || new Set();
    activeSet.add(row.tower);
    activeTowersByDay.set(row.run_date, activeSet);

    const towerDayKey = `${row.tower}||${row.run_date}`;
    const engagedMinutes = calculateTowerEngagedMinutes(row);
    towerDayEngaged.set(
      towerDayKey,
      Math.min((towerDayEngaged.get(towerDayKey) || 0) + engagedMinutes, CAPACITY_WINDOW_MINUTES)
    );
  }

  const availabilityByDay = days.map((day) => {
    const activeCount = activeTowersByDay.get(day)?.size || 0;
    return totalTowers > 0 ? (activeCount / totalTowers) * 100 : 0;
  });
  const thresholdRows = AVAILABILITY_THRESHOLDS.map((threshold) => {
    const daysAtThreshold = availabilityByDay.filter((value) =>
      threshold === 100 ? value >= 99.999 : value >= threshold
    ).length;

    return {
      threshold,
      days: daysAtThreshold,
      share: totalDays > 0 ? (daysAtThreshold / totalDays) * 100 : 0,
    };
  });
  const utilizationRows = towers.map((tower) => {
    const engaged = days.reduce(
      (sum, day) => sum + (towerDayEngaged.get(`${tower}||${day}`) || 0),
      0
    );
    const utilization = totalDays > 0
      ? (engaged / (totalDays * CAPACITY_WINDOW_MINUTES)) * 100
      : 0;

    return { tower, utilization };
  }).sort((first, second) => {
    if (first.utilization !== second.utilization) return first.utilization - second.utilization;
    return String(first.tower).localeCompare(String(second.tower));
  });

  return {
    totalDays,
    totalTowers,
    thresholdRows,
    medianUtilization: median(utilizationRows.map((row) => row.utilization)),
    lowestUtilization: utilizationRows[0]?.utilization || 0,
    lowestTower: utilizationRows[0]?.tower || "",
  };
}

function getSelectedDays(towerDetails, dailyRows) {
  const dailyDays = (dailyRows || [])
    .map((row) => row.run_date)
    .filter(Boolean);

  if (dailyDays.length > 0) {
    return Array.from(new Set(dailyDays)).sort();
  }

  return Array.from(new Set(
    (towerDetails || [])
      .map((row) => row.run_date)
      .filter(Boolean)
  )).sort();
}

function calculateTowerEngagedMinutes(row) {
  const available = positiveNumber(row.available_capacity) || CAPACITY_WINDOW_MINUTES;
  const buffer = positiveNumber(row.buffer_time);

  if (buffer > 0 || available > 0) {
    return Math.min(Math.max(available - buffer, 0), CAPACITY_WINDOW_MINUTES);
  }

  return Math.min(
    positiveNumber(row.runtime)
    + positiveNumber(row.downtime)
    + positiveNumber(row.waiting_time)
    + positiveNumber(row.change_over_time)
    + positiveNumber(row.reflong_related_downtime)
    + positiveNumber(row.late_start_time),
    CAPACITY_WINDOW_MINUTES
  );
}

function median(values) {
  const sorted = values
    .map((value) => Number(value || 0))
    .sort((first, second) => first - second);

  if (sorted.length === 0) return 0;

  const middle = Math.floor(sorted.length / 2);
  if (sorted.length % 2 === 1) return sorted[middle];

  return (sorted[middle - 1] + sorted[middle]) / 2;
}

function positiveNumber(value) {
  return Math.max(Number(value || 0), 0);
}

function formatTowerName(value) {
  return String(value || "")
    .split("\n")
    .map((part) => part.trim())
    .filter(Boolean)
    .join(" / ");
}

function formatNumber(value) {
  return new Intl.NumberFormat("en-US", { maximumFractionDigits: 1 }).format(Number(value || 0));
}

function formatPercent(value) {
  return `${formatNumber(Math.min(Math.max(Number(value || 0), 0), 100))}%`;
}
