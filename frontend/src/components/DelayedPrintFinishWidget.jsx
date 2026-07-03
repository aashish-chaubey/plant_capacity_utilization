import { useMemo } from "react";
import { AlertTriangle } from "lucide-react";

const PF_COMPLIANCE_MINUTES_BY_PLANT = {
  baroda: 180,
  manesar: 180,
  trivandrum: 150,
};

function pfComplianceMinutes(plantName) {
  const key = String(plantName || "").toLowerCase().trim();
  return PF_COMPLIANCE_MINUTES_BY_PLANT[key] ?? 240;
}

const CAUSE_ORDER = [
  "low_speed",
  "high_complexity",
  "high_downtime",
  "high_wait_time",
  "high_lost_time",
];

const ROOT_CAUSE_THRESHOLDS = {
  downtime_minutes: 30,
  waiting_time_minutes: 30,
  lost_time_minutes: 40,
  speed_loss_minutes: 10,
  complex_minutes: 30,
  complex_share_pct: 40,
};

const CAUSE_META = {
  low_speed: {
    label: "Low average speed",
    color: "#2563eb",
    pill: "bg-blue-50 text-blue-700",
  },
  high_complexity: {
    label: "High complexity",
    color: "#7c3aed",
    pill: "bg-violet-50 text-violet-700",
  },
  high_downtime: {
    label: "High downtime",
    color: "#e11d48",
    pill: "bg-rose-50 text-rose-700",
  },
  high_wait_time: {
    label: "High wait time",
    color: "#64748b",
    pill: "bg-slate-100 text-slate-700",
  },
  high_lost_time: {
    label: "High lost time",
    color: "#d97706",
    pill: "bg-amber-50 text-amber-700",
  },
};

export default function DelayedPrintFinishWidget({ details }) {
  const analysis = useMemo(
    () => buildDelayedFinishAnalysis(details || []),
    [details]
  );

  if (!details || details.length === 0) return null;

  return (
    <div className="min-w-0">
      <div className="flex items-start gap-2">
        <span className="inline-flex h-8 w-8 shrink-0 items-center justify-center rounded-lg bg-red-50 text-red-700">
          <AlertTriangle className="h-4 w-4" aria-hidden="true" />
        </span>
        <div>
          <h3 className="text-base font-semibold text-slate-950">
            Folderwise & Plant-Level Delayed PF
          </h3>
          <p className="mt-1 text-sm text-slate-500">
            {formatNumber(analysis.folderBreaches.length)} breach{analysis.folderBreaches.length === 1 ? "" : "es"}
          </p>
        </div>
      </div>

      {analysis.folderBreaches.length === 0 ? (
        <div className="mt-3 rounded-lg border border-dashed border-slate-200 bg-slate-50 p-4 text-sm text-slate-500">
          No delayed PF beyond 04:00 AM was found for this selection.
        </div>
      ) : (
        <div className="mt-3 grid gap-4 lg:grid-cols-[minmax(0,1fr)_300px]">
          <DelayedFinishTable rows={analysis.tableRows} />
          <DelayCauseDonut
            rows={analysis.causeTotals}
            topCause={analysis.topCause}
            delayedPlantDays={analysis.delayedPlantDays}
          />
        </div>
      )}
    </div>
  );
}

function DelayCauseDonut({ rows, topCause, delayedPlantDays }) {
  const positiveRows = rows.filter((row) => row.minutes > 0);
  const totalMinutes = positiveRows.reduce((sum, row) => sum + row.minutes, 0);
  const gradient = buildDonutGradient(positiveRows, totalMinutes);

  return (
    <aside className="rounded-lg border border-slate-100 bg-slate-50 p-3">
      <div className="flex items-center justify-between gap-3">
        <h4 className="text-sm font-semibold text-slate-950">Plant-level cause mix</h4>
        <span className="text-xs font-semibold text-slate-500">
          {formatNumber(delayedPlantDays)} delayed day{delayedPlantDays === 1 ? "" : "s"}
        </span>
      </div>

      <div className="mt-3 grid gap-3 md:grid-cols-[128px_minmax(0,1fr)] lg:grid-cols-1">
        <div className="relative mx-auto h-32 w-32 shrink-0 rounded-full" style={{ background: gradient }}>
          <div className="absolute inset-5 flex flex-col items-center justify-center rounded-full bg-white text-center shadow-inner">
            <span className="text-[10px] font-semibold uppercase tracking-normal text-slate-500">Top</span>
            <span className="mt-1 max-w-20 text-xs font-bold leading-4 text-slate-950">
              {topCause ? CAUSE_META[topCause.cause].label : "NA"}
            </span>
          </div>
        </div>

        <div className="space-y-1.5">
          {positiveRows.length === 0 ? (
            <div className="rounded-lg border border-dashed border-slate-200 bg-white p-3 text-sm text-slate-500">
              No plant-level cause mix available.
            </div>
          ) : positiveRows.map((row) => {
            const meta = CAUSE_META[row.cause];
            const percentage = totalMinutes > 0 ? (row.minutes / totalMinutes) * 100 : 0;

            return (
              <div key={row.cause} className="flex items-center justify-between gap-3 text-xs">
                <span className="flex min-w-0 items-center gap-1.5 font-semibold text-slate-700">
                  <span
                    className="h-2.5 w-2.5 shrink-0 rounded-sm"
                    style={{ backgroundColor: meta.color }}
                  />
                  <span className="truncate">{meta.label}</span>
                </span>
                <span className="shrink-0 text-right font-bold text-slate-900">
                  {formatPercent(percentage)}
                </span>
              </div>
            );
          })}
        </div>
      </div>
    </aside>
  );
}

function buildDonutGradient(rows, totalMinutes) {
  if (totalMinutes <= 0 || rows.length === 0) {
    return "conic-gradient(#e2e8f0 0deg 360deg)";
  }

  let cursor = 0;
  const stops = rows.map((row) => {
    const start = cursor;
    const end = cursor + (row.minutes / totalMinutes) * 360;
    cursor = end;
    return `${CAUSE_META[row.cause].color} ${start}deg ${end}deg`;
  });

  return `conic-gradient(${stops.join(", ")})`;
}

function DelayedFinishTable({ rows }) {
  return (
    <div>
      <div className="mb-2 flex items-center justify-between gap-3">
        <h4 className="text-sm font-semibold text-slate-950">Delayed PF breaches</h4>
      </div>
      <div className="max-h-[300px] overflow-auto rounded-lg border border-slate-100">
        <table className="w-full min-w-[900px] text-sm">
          <thead className="sticky top-0 bg-slate-50">
            <tr className="border-b border-slate-100 text-left text-xs font-semibold uppercase tracking-normal text-slate-500">
              <th className="px-3 py-2">Date</th>
              <th className="px-3 py-2">Folder</th>
              <th className="px-3 py-2">Print finish</th>
              <th className="px-3 py-2">Considered editions</th>
              <th className="px-3 py-2">Root cause</th>
            </tr>
          </thead>
          <tbody className="divide-y divide-slate-50 bg-white">
            {rows.map((row) => (
              <tr key={row.key} className="align-top">
                <td className="px-3 py-2 font-medium text-slate-800">
                  {formatDate(row.run_date)}
                </td>
                <td className="px-3 py-2 text-slate-700">
                  {row.display_name}
                </td>
                <td className="px-3 py-2 font-mono text-slate-700">
                  {formatFinishTime(row.overrun_minutes, row.plant_name)}
                </td>
                <td className="max-w-[220px] px-3 py-2 text-slate-600">
                  <span className="line-clamp-2" title={formatEditions(row.considered_editions)}>
                    {formatEditions(row.considered_editions)}
                  </span>
                </td>
                <td className="px-3 py-2">
                  <div className="flex flex-wrap gap-1">
                    {row.root_causes.map((cause) => (
                      <CausePill key={cause.cause} cause={cause.cause} />
                    ))}
                  </div>
                </td>
              </tr>
            ))}
          </tbody>
        </table>
      </div>
    </div>
  );
}

function CausePill({ cause, minutes, showMinutes = false }) {
  const meta = CAUSE_META[cause] || {
    label: cause,
    pill: "bg-slate-100 text-slate-600",
  };

  return (
    <span className={`inline-flex items-center gap-1 rounded-md px-2 py-0.5 text-xs font-medium ${meta.pill}`}>
      <span>{meta.label}</span>
      {showMinutes && Number(minutes || 0) > 0 && (
        <span className="font-bold">{formatMinutes(minutes)}</span>
      )}
    </span>
  );
}

function buildDelayedFinishAnalysis(details) {
  const sourceRows = details.filter((row) => row.run_date && row.folder);
  const folderBaselines = buildBaselines(sourceRows, (row) => row.folder);
  const delayedRows = dedupeTwinFolderBreaches(
    sourceRows.filter((row) => Number(row.overrun_minutes || 0) > 0)
  );
  const folderBreaches = delayedRows
    .map((row) => buildFolderBreach(row, folderBaselines[row.folder]))
    .sort(compareBreachRows);
  const plantBreaches = buildPlantBreaches(folderBreaches);
  const causeTotals = buildCauseTotals(plantBreaches);
  const topCause = causeTotals.find((row) => row.minutes > 0) || null;
  const tableRows = folderBreaches;
  const delayedPlantDays = new Set(plantBreaches.map((row) => `${row.run_date}||${row.plant_name}`)).size;

  return {
    folderBreaches,
    plantBreaches,
    causeTotals,
    topCause,
    tableRows,
    delayedPlantDays,
  };
}

function dedupeTwinFolderBreaches(rows) {
  const grouped = new Map();

  for (const row of rows) {
    const key = delayedPrintFinishKey(row);
    const current = grouped.get(key);

    if (!current || compareTwinDuplicateRows(row, current) < 0) {
      grouped.set(key, row);
    }
  }

  return Array.from(grouped.values());
}

function delayedPrintFinishKey(row) {
  const plant = row.plant_name || "";

  if (row.twin_folder_mode && row.twin_folder_group) {
    return [
      row.run_date || "",
      plant,
      row.twin_folder_group,
      Math.round(Number(row.overrun_minutes || 0)),
    ].join("||");
  }

  return [
    row.run_date || "",
    plant,
    row.folder || "",
    Math.round(Number(row.overrun_minutes || 0)),
  ].join("||");
}

function compareTwinDuplicateRows(first, second) {
  const folderDiff = String(first.folder || "").localeCompare(String(second.folder || ""));
  if (folderDiff !== 0) return folderDiff;

  return formatEditions(first.cutoff_started_editions).localeCompare(formatEditions(second.cutoff_started_editions));
}

function buildFolderBreach(row, baseline) {
  const causeScores = calculateCauseScores(row, baseline || {});
  const consideredEditions = Array.isArray(row.cutoff_started_editions) ? row.cutoff_started_editions : [];

  return {
    ...row,
    key: `folder||${row.run_date}||${row.folder}`,
    scope: "folder",
    plant_name: row.plant_name || "Plant",
    display_name: formatFolderName(row.folder),
    overrun_minutes: cleanNumber(row.overrun_minutes),
    considered_editions: consideredEditions,
    cause_scores: causeScores,
    root_causes: getRootCauses(causeScores),
  };
}

function buildPlantBreaches(folderBreaches) {
  const grouped = new Map();

  for (const row of folderBreaches) {
    const plantName = row.plant_name || "Plant";
    const key = `${row.run_date}||${plantName}`;
    const current = grouped.get(key) || {
      key: `plant||${row.run_date}||${plantName}`,
      scope: "plant",
      run_date: row.run_date,
      plant_name: plantName,
      display_name: `${plantName} (Plant level)`,
      folder_count: 0,
      overrun_minutes: 0,
      cause_scores: emptyCauseScores(),
    };

    current.folder_count += 1;
    current.overrun_minutes = Math.max(current.overrun_minutes, Number(row.overrun_minutes || 0));
    current.cause_scores = addCauseScores(current.cause_scores, row.cause_scores);
    grouped.set(key, current);
  }

  return Array.from(grouped.values())
    .map((row) => ({
      ...row,
      overrun_minutes: cleanNumber(row.overrun_minutes),
      root_causes: getRootCauses(row.cause_scores),
    }))
    .sort(compareBreachRows);
}

function calculateCauseScores(row, baseline) {
  const waitingTime = positiveNumber(row.waiting_time);
  const downtime = positiveNumber(row.downtime);
  const nonWaitLostTime = calculateNonWaitLostTime(row, waitingTime);
  const runtime = positiveNumber(row.runtime);
  const printOrder = computeTotalPrintOrder(row);
  const baselineSpeed = positiveNumber(baseline.avg_speed);
  const complexMinutes = computeComplexMinutes(row);
  const complexShare = runtime > 0 ? (complexMinutes / runtime) * 100 : 0;
  const speedLossMinutes = baselineSpeed > 0 && printOrder > 0
    ? Math.max(runtime - (printOrder / baselineSpeed) * 60, 0)
    : 0;
  const scores = {
    low_speed: speedLossMinutes >= ROOT_CAUSE_THRESHOLDS.speed_loss_minutes ? speedLossMinutes : 0,
    high_complexity:
      complexMinutes >= ROOT_CAUSE_THRESHOLDS.complex_minutes || complexShare >= ROOT_CAUSE_THRESHOLDS.complex_share_pct
        ? complexMinutes
        : 0,
    high_downtime: downtime >= ROOT_CAUSE_THRESHOLDS.downtime_minutes ? downtime : 0,
    high_wait_time: waitingTime >= ROOT_CAUSE_THRESHOLDS.waiting_time_minutes ? waitingTime : 0,
    high_lost_time: nonWaitLostTime >= ROOT_CAUSE_THRESHOLDS.lost_time_minutes ? nonWaitLostTime : 0,
  };

  if (Object.values(scores).some((value) => value > 0)) {
    return cleanCauseScores(scores);
  }

  return cleanCauseScores({
    low_speed: speedLossMinutes,
    high_complexity: complexMinutes,
    high_downtime: downtime,
    high_wait_time: waitingTime,
    high_lost_time: nonWaitLostTime,
  });
}

function buildCauseTotals(plantBreaches) {
  const totals = plantBreaches.reduce((acc, row) => addCauseScores(acc, row.cause_scores), emptyCauseScores());

  return orderedCauseEntries(totals).sort((a, b) => {
    if (b.minutes !== a.minutes) return b.minutes - a.minutes;
    return CAUSE_ORDER.indexOf(a.cause) - CAUSE_ORDER.indexOf(b.cause);
  });
}

function getRootCauses(scores) {
  const entries = orderedCauseEntries(scores)
    .filter((entry) => entry.minutes > 0)
    .sort((a, b) => {
      if (b.minutes !== a.minutes) return b.minutes - a.minutes;
      return CAUSE_ORDER.indexOf(a.cause) - CAUSE_ORDER.indexOf(b.cause);
    });

  if (!entries.length) return [];

  const [top, ...rest] = entries;
  const secondary = rest.filter((entry) => entry.minutes >= Math.max(top.minutes * 0.35, 10)).slice(0, 1);
  return [top, ...secondary];
}

function buildBaselines(rows, keyGetter) {
  const groups = {};

  for (const row of rows) {
    const key = keyGetter(row);
    if (!key) continue;
    if (!groups[key]) groups[key] = [];
    groups[key].push(row);
  }

  return Object.fromEntries(
    Object.entries(groups).map(([key, groupRows]) => [key, computeBaseline(groupRows)])
  );
}

function computeBaseline(rows) {
  const speedValues = [];

  for (const row of rows) {
    const speed = computeAverageSpeed(row);
    if (speed > 0) speedValues.push(speed);
  }

  return {
    avg_speed: average(speedValues),
  };
}

function computeAverageSpeed(row) {
  const segments = Array.isArray(row.runtime_segments) ? row.runtime_segments : [];
  let weightedSpeed = 0;
  let totalMinutes = 0;
  const twinDivisor = row.twin_folder_mode ? 2 : 1;

  for (const segment of segments) {
    const minutes = positiveNumber(segment.minutes);
    const speed = positiveNumber(segment.effective_speed) / twinDivisor;
    if (minutes > 0 && speed > 0) {
      weightedSpeed += speed * minutes;
      totalMinutes += minutes;
    }
  }

  return totalMinutes > 0 ? weightedSpeed / totalMinutes : 0;
}

function computeTotalPrintOrder(row) {
  const segments = Array.isArray(row.runtime_segments) ? row.runtime_segments : [];
  const twinDivisor = row.twin_folder_mode ? 2 : 1;
  return segments.reduce((sum, segment) => sum + positiveNumber(segment.print_order) / twinDivisor, 0);
}

function computeComplexMinutes(row) {
  const segments = Array.isArray(row.runtime_segments) ? row.runtime_segments : [];

  return segments.reduce((sum, segment) => {
    const isComplex = Boolean(segment.is_complex) || String(segment.key || "").toLowerCase().includes("complex");
    return isComplex ? sum + positiveNumber(segment.minutes) : sum;
  }, 0);
}

function calculateNonWaitLostTime(row, waitingTime) {
  const explicitLoss = (
    positiveNumber(row.change_over_time)
    + positiveNumber(row.reflong_related_downtime)
    + positiveNumber(row.late_start_time)
  );

  if (explicitLoss > 0) return explicitLoss;
  return positiveNumber(row.lost_time);
}

function addCauseScores(first, second) {
  return CAUSE_ORDER.reduce((acc, cause) => {
    acc[cause] = cleanNumber(positiveNumber(first?.[cause]) + positiveNumber(second?.[cause]));
    return acc;
  }, {});
}

function emptyCauseScores() {
  return Object.fromEntries(CAUSE_ORDER.map((cause) => [cause, 0]));
}

function cleanCauseScores(scores) {
  return CAUSE_ORDER.reduce((acc, cause) => {
    acc[cause] = cleanNumber(scores[cause]);
    return acc;
  }, {});
}

function orderedCauseEntries(scores) {
  return CAUSE_ORDER.map((cause) => ({
    cause,
    minutes: cleanNumber(scores?.[cause]),
  }));
}

function compareBreachRows(first, second) {
  const dateDiff = String(first.run_date).localeCompare(String(second.run_date));
  if (dateDiff !== 0) return dateDiff;

  if (first.scope !== second.scope) {
    return first.scope === "plant" ? -1 : 1;
  }

  return String(first.display_name).localeCompare(String(second.display_name));
}

function average(values) {
  if (!values.length) return 0;
  return values.reduce((sum, value) => sum + value, 0) / values.length;
}

function positiveNumber(value) {
  return Math.max(Number(value || 0), 0);
}

function cleanNumber(value) {
  const numeric = Number.isFinite(Number(value)) ? Number(value) : 0;
  const rounded = Math.round(numeric * 100) / 100;
  return Number.isInteger(rounded) ? rounded : Number(rounded.toFixed(2));
}

function formatDate(value) {
  const match = /^(\d{4})-(\d{2})-(\d{2})$/.exec(String(value || ""));
  if (!match) return String(value || "");
  const date = new Date(Number(match[1]), Number(match[2]) - 1, Number(match[3]));
  return new Intl.DateTimeFormat("en-US", { month: "short", day: "numeric", year: "numeric" }).format(date);
}

function formatFolderName(value) {
  return String(value || "")
    .split("\n")
    .map((part) => part.trim())
    .filter(Boolean)
    .join(" / ");
}

function formatEditions(value) {
  if (Array.isArray(value)) {
    const editions = value
      .map((edition) => String(edition || "").trim())
      .filter(Boolean);

    return editions.length > 0 ? editions.join(", ") : "-";
  }

  const edition = String(value || "").trim();
  return edition || "-";
}

function formatMinutes(value) {
  const minutes = Math.round(Number(value || 0));
  return `${minutes} min`;
}

function formatFinishTime(overrunMinutes, plantName) {
  const baseMinutes = pfComplianceMinutes(plantName);
  const totalMinutes = baseMinutes + Math.round(Number(overrunMinutes || 0));
  const dayOffset = Math.floor(totalMinutes / 1440);
  const clockMinutes = totalMinutes % 1440;
  const hours = Math.floor(clockMinutes / 60);
  const minutes = clockMinutes % 60;
  const timeText = `${String(hours).padStart(2, "0")}:${String(minutes).padStart(2, "0")}`;
  return dayOffset > 0 ? `${timeText} +${dayOffset}d` : timeText;
}

function formatNumber(value) {
  return new Intl.NumberFormat("en-US", { maximumFractionDigits: 1 }).format(Number(value || 0));
}

function formatPercent(value) {
  return `${formatNumber(Math.min(Math.max(Number(value || 0), 0), 100))}%`;
}
