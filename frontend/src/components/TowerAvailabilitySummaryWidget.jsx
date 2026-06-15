import { useMemo } from "react";
import { RadioTower } from "lucide-react";

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
            Tower Utilization Summary
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
                <th className="px-3 py-2">Tower utilization</th>
                <th className="px-3 py-2">Days</th>
                <th className="px-3 py-2">Share</th>
              </tr>
            </thead>
            <tbody className="divide-y divide-slate-50 bg-white">
              {summary.thresholdRows.map((row) => (
                <tr key={row.towerCount}>
                  <td className="px-3 py-2 font-medium text-slate-800">
                    At least {formatNumber(row.towerCount)} of {formatNumber(summary.totalTowers)} towers
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

        <div className="grid gap-2">
          <TowerMetric
            label="Minimum operational towers"
            value={`${formatNumber(summary.minimumActiveTowers)} / ${formatNumber(summary.totalTowers)}`}
            detail="Lowest active tower count on any selected day"
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
        <div className="mt-0.5 text-xs font-medium text-slate-500" title={detail}>
          {detail}
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

  for (const row of towerDetails) {
    if (!row.run_date || !daySet.has(row.run_date) || !row.tower) continue;

    const activeSet = activeTowersByDay.get(row.run_date) || new Set();
    activeSet.add(row.tower);
    activeTowersByDay.set(row.run_date, activeSet);
  }

  const activeCountsByDay = days.map((day) => activeTowersByDay.get(day)?.size || 0);
  const minimumActiveTowers = activeCountsByDay.length > 0
    ? Math.min(...activeCountsByDay)
    : 0;
  const thresholds = buildDynamicTowerThresholds(minimumActiveTowers, totalTowers);
  const thresholdRows = thresholds.map((towerCount) => {
    const daysAtThreshold = activeCountsByDay.filter((activeCount) => activeCount >= towerCount).length;

    return {
      towerCount,
      days: daysAtThreshold,
      share: totalDays > 0 ? (daysAtThreshold / totalDays) * 100 : 0,
    };
  });

  return {
    totalDays,
    totalTowers,
    minimumActiveTowers,
    thresholdRows,
  };
}

function buildDynamicTowerThresholds(minimumActiveTowers, totalTowers) {
  if (totalTowers <= 0) return [];

  const lower = Math.min(Math.max(Math.ceil(minimumActiveTowers), 1), totalTowers);
  const availableThresholdCount = totalTowers - lower;
  if (availableThresholdCount <= 0) return [];

  if (availableThresholdCount <= 4) {
    return Array.from(
      { length: availableThresholdCount },
      (_, index) => lower + index + 1
    );
  }

  return Array.from(new Set(
    [1, 2, 3, 4].map((step) =>
      Math.min(totalTowers, lower + Math.ceil((availableThresholdCount * step) / 4))
    )
  ));
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

function formatNumber(value) {
  return new Intl.NumberFormat("en-US", { maximumFractionDigits: 1 }).format(Number(value || 0));
}

function formatPercent(value) {
  return `${formatNumber(Math.min(Math.max(Number(value || 0), 0), 100))}%`;
}
