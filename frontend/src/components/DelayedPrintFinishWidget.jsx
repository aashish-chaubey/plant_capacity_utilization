import { useMemo, useState } from "react";
import { AlertTriangle, ChevronDown, ChevronUp } from "lucide-react";

const ROOT_CAUSE_THRESHOLDS = {
  downtime_minutes: 30,
  waiting_time_minutes: 30,
  lost_time_minutes: 40,
  speed_below_average_pct: 25,
  complex_share_pct: 40,
  print_order_above_average_pct: 25,
};

const ROOT_CAUSE_LABELS = {
  high_downtime: "High downtime",
  high_wait_time: "High wait time",
  high_lost_time: "High lost time",
  low_speed: "Low average speed",
  high_complexity: "High complexity",
  high_print_order: "High print order",
};

export default function DelayedPrintFinishWidget({ details }) {
  const [expanded, setExpanded] = useState(true);

  const { breachedRows, folderBaselines } = useMemo(
    () => buildBreachAnalysis(details || []),
    [details]
  );

  if (!details || details.length === 0) return null;
  if (breachedRows.length === 0) return null;

  return (
    <section className="rounded-lg border border-slate-200 bg-white shadow-soft">
      <button
        type="button"
        onClick={() => setExpanded((v) => !v)}
        className="flex w-full items-center justify-between p-3 text-left"
      >
        <div className="flex items-center gap-2">
          <span className="inline-flex h-8 w-8 items-center justify-center rounded-lg bg-red-50 text-red-700">
            <AlertTriangle className="h-4 w-4" aria-hidden="true" />
          </span>
          <div>
            <h2 className="text-base font-semibold text-slate-950">
              Delayed print finish
            </h2>
            <p className="text-xs text-slate-500">
              {breachedRows.length} folder-day{breachedRows.length === 1 ? "" : "s"} crossed the 04:00 AM window
            </p>
          </div>
        </div>
        {expanded
          ? <ChevronUp className="h-4 w-4 text-slate-400" />
          : <ChevronDown className="h-4 w-4 text-slate-400" />}
      </button>

      {expanded && (
        <div className="border-t border-slate-100 px-3 pb-3">
          <div className="mt-3 overflow-x-auto">
            <table className="w-full min-w-[600px] text-sm">
              <thead>
                <tr className="border-b border-slate-100 text-left text-xs font-semibold uppercase tracking-normal text-slate-500">
                  <th className="pb-2 pr-4">Date</th>
                  <th className="pb-2 pr-4">Folder</th>
                  <th className="pb-2 pr-4 text-right">Finish time</th>
                  <th className="pb-2 pr-4 text-right">Overrun</th>
                  <th className="pb-2">Root cause</th>
                </tr>
              </thead>
              <tbody className="divide-y divide-slate-50">
                {breachedRows.map((row) => {
                  const baseline = folderBaselines[row.folder] || {};
                  const causes = identifyRootCauses(row, baseline);
                  return (
                    <tr key={`${row.run_date}||${row.folder}`} className="align-top">
                      <td className="py-2 pr-4 font-medium text-slate-800">
                        {formatDate(row.run_date)}
                      </td>
                      <td className="py-2 pr-4 text-slate-700">
                        {formatFolderName(row.folder)}
                      </td>
                      <td className="py-2 pr-4 text-right font-mono text-slate-700">
                        {formatFinishTime(row.overrun_minutes)}
                      </td>
                      <td className="py-2 pr-4 text-right">
                        <span className="inline-block rounded-md bg-red-50 px-2 py-0.5 text-xs font-semibold text-red-700">
                          +{formatMinutes(row.overrun_minutes)}
                        </span>
                      </td>
                      <td className="py-2">
                        <div className="flex flex-wrap gap-1">
                          {causes.length > 0
                            ? causes.map((cause) => (
                                <CausePill key={cause} cause={cause} />
                              ))
                            : <span className="text-xs text-slate-400">—</span>}
                        </div>
                      </td>
                    </tr>
                  );
                })}
              </tbody>
            </table>
          </div>
        </div>
      )}
    </section>
  );
}

function CausePill({ cause }) {
  const colorMap = {
    high_downtime: "bg-pink-50 text-pink-700",
    high_wait_time: "bg-slate-100 text-slate-700",
    high_lost_time: "bg-amber-50 text-amber-700",
    low_speed: "bg-blue-50 text-blue-700",
    high_complexity: "bg-violet-50 text-violet-700",
    high_print_order: "bg-orange-50 text-orange-700",
  };
  return (
    <span className={`inline-block rounded-md px-2 py-0.5 text-xs font-medium ${colorMap[cause] || "bg-slate-100 text-slate-600"}`}>
      {ROOT_CAUSE_LABELS[cause] || cause}
    </span>
  );
}

function buildBreachAnalysis(details) {
  const breachedRows = details
    .filter((row) => Number(row.overrun_minutes || 0) > 0)
    .sort((a, b) => {
      const dateDiff = String(a.run_date).localeCompare(String(b.run_date));
      return dateDiff !== 0 ? dateDiff : String(a.folder).localeCompare(String(b.folder));
    });

  // Build per-folder baselines from ALL days (to compare breached days against)
  const folderGroups = {};
  for (const row of details) {
    const key = row.folder;
    if (!folderGroups[key]) folderGroups[key] = [];
    folderGroups[key].push(row);
  }

  const folderBaselines = {};
  for (const [folder, rows] of Object.entries(folderGroups)) {
    folderBaselines[folder] = computeBaseline(rows);
  }

  return { breachedRows, folderBaselines };
}

function computeBaseline(rows) {
  if (!rows.length) return {};

  const speedValues = [];
  const printOrderValues = [];

  for (const row of rows) {
    const speed = computeAverageSpeed(row);
    const po = computeTotalPrintOrder(row);
    if (speed > 0) speedValues.push(speed);
    if (po > 0) printOrderValues.push(po);
  }

  return {
    avg_speed: speedValues.length ? speedValues.reduce((s, v) => s + v, 0) / speedValues.length : 0,
    avg_print_order: printOrderValues.length ? printOrderValues.reduce((s, v) => s + v, 0) / printOrderValues.length : 0,
  };
}

function identifyRootCauses(row, baseline) {
  const causes = [];

  // High downtime
  if (Number(row.downtime || 0) >= ROOT_CAUSE_THRESHOLDS.downtime_minutes) {
    causes.push("high_downtime");
  }

  // High wait time
  if (Number(row.waiting_time || 0) >= ROOT_CAUSE_THRESHOLDS.waiting_time_minutes) {
    causes.push("high_wait_time");
  }

  // High lost time
  if (Number(row.lost_time || 0) >= ROOT_CAUSE_THRESHOLDS.lost_time_minutes) {
    causes.push("high_lost_time");
  }

  // Low average speed vs folder baseline
  const daySpeed = computeAverageSpeed(row);
  if (daySpeed > 0 && baseline.avg_speed > 0) {
    const pctBelow = ((baseline.avg_speed - daySpeed) / baseline.avg_speed) * 100;
    if (pctBelow >= ROOT_CAUSE_THRESHOLDS.speed_below_average_pct) {
      causes.push("low_speed");
    }
  }

  // High complexity share
  const complexShare = computeComplexShare(row);
  if (complexShare >= ROOT_CAUSE_THRESHOLDS.complex_share_pct) {
    causes.push("high_complexity");
  }

  // High print order vs folder baseline
  const dayPO = computeTotalPrintOrder(row);
  if (dayPO > 0 && baseline.avg_print_order > 0) {
    const pctAbove = ((dayPO - baseline.avg_print_order) / baseline.avg_print_order) * 100;
    if (pctAbove >= ROOT_CAUSE_THRESHOLDS.print_order_above_average_pct) {
      causes.push("high_print_order");
    }
  }

  return causes;
}

function computeAverageSpeed(row) {
  const segments = row.runtime_segments;
  if (!Array.isArray(segments) || !segments.length) return 0;

  let weightedSpeed = 0;
  let totalMinutes = 0;

  for (const seg of segments) {
    const minutes = Number(seg.minutes || 0);
    const speed = Number(seg.effective_speed || 0);
    if (minutes > 0 && speed > 0) {
      weightedSpeed += speed * minutes;
      totalMinutes += minutes;
    }
  }

  return totalMinutes > 0 ? weightedSpeed / totalMinutes : 0;
}

function computeComplexShare(row) {
  const segments = row.runtime_segments;
  if (!Array.isArray(segments) || !segments.length) return 0;

  let complexMinutes = 0;
  let totalMinutes = 0;

  for (const seg of segments) {
    const minutes = Number(seg.minutes || 0);
    if (minutes > 0) {
      totalMinutes += minutes;
      if (seg.is_complex) complexMinutes += minutes;
    }
  }

  return totalMinutes > 0 ? (complexMinutes / totalMinutes) * 100 : 0;
}

function computeTotalPrintOrder(row) {
  const segments = row.runtime_segments;
  if (!Array.isArray(segments) || !segments.length) return 0;
  return segments.reduce((sum, seg) => sum + Number(seg.print_order || 0), 0);
}

function formatDate(value) {
  const match = /^(\d{4})-(\d{2})-(\d{2})$/.exec(String(value || ""));
  if (!match) return String(value || "");
  const d = new Date(Number(match[1]), Number(match[2]) - 1, Number(match[3]));
  return new Intl.DateTimeFormat("en-US", { month: "short", day: "numeric", year: "numeric" }).format(d);
}

function formatFolderName(value) {
  return String(value || "")
    .split("\n")
    .map((p) => p.trim())
    .filter(Boolean)
    .join(" / ");
}

function formatFinishTime(overrunMinutes) {
  const totalMinutes = 240 + Number(overrunMinutes || 0);
  const hours = Math.floor(totalMinutes / 60);
  const mins = Math.round(totalMinutes % 60);
  return `${String(hours).padStart(2, "0")}:${String(mins).padStart(2, "0")}`;
}

function formatMinutes(value) {
  const mins = Math.round(Number(value || 0));
  if (mins < 60) return `${mins} min`;
  const h = Math.floor(mins / 60);
  const m = mins % 60;
  return m > 0 ? `${h}h ${m}m` : `${h}h`;
}
