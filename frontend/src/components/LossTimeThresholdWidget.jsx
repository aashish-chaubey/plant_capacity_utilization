import { useMemo, useState } from "react";
import { ChevronDown, ChevronUp, Info, TrendingDown } from "lucide-react";

// ─── Constants ──────────────────────────────────────────────────────────────

const SPEED_PERCENTILE = 75;
const DOWNTIME_PERCENTILE = 25;
const AVAILABLE_MINUTES = 240;
const MIN_SAMPLE_SIZE = 3; // minimum observations before a baseline is trusted

const COMPLEXITY_ORDER = ["snp", "snp_complex", "gnp", "gnp_complex", "unknown"];
const COMPLEXITY_META = {
  snp:         { label: "SNP",         color: "bg-emerald-50 text-emerald-800 border-emerald-100" },
  snp_complex: { label: "SNP Complex", color: "bg-violet-50 text-violet-800 border-violet-100" },
  gnp:         { label: "GNP",         color: "bg-blue-50 text-blue-800 border-blue-100" },
  gnp_complex: { label: "GNP Complex", color: "bg-orange-50 text-orange-800 border-orange-100" },
  unknown:     { label: "Unknown",     color: "bg-slate-50 text-slate-600 border-slate-200" },
};

// ─── Main component ──────────────────────────────────────────────────────────

export default function LossTimeThresholdWidget({ details }) {
  const [methodologyOpen, setMethodologyOpen] = useState(false);

  const analysis = useMemo(() => buildAnalysis(details || []), [details]);

  if (!details || !details.length) return null;

  const { baselines, p25Downtime, deviations, totalFolderDays, trustedKeys } = analysis;
  if (Object.keys(baselines).length === 0) return null;

  return (
    <section className="rounded-lg border border-slate-200 bg-white shadow-soft">
      {/* Header */}
      <div className="p-3">
        <div className="flex items-center gap-2">
          <span className="inline-flex h-8 w-8 items-center justify-center rounded-lg bg-amber-50 text-amber-700">
            <TrendingDown className="h-4 w-4" aria-hidden="true" />
          </span>
          <div>
            <h2 className="text-base font-semibold text-slate-950">
              Lost time threshold · complexity-based
            </h2>
            <p className="text-xs text-slate-500">
              Max affordable lost time derived from {totalFolderDays} folder-day{totalFolderDays === 1 ? "" : "s"} of historical data
            </p>
          </div>
        </div>

        {/* Collapsible methodology */}
        <button
          type="button"
          onClick={() => setMethodologyOpen((v) => !v)}
          className="mt-3 flex w-full items-center justify-between rounded-lg border border-slate-100 bg-slate-50 px-3 py-2 text-left transition hover:bg-slate-100"
        >
          <div className="flex items-center gap-1.5 text-xs font-semibold text-slate-600">
            <Info className="h-3.5 w-3.5" aria-hidden="true" />
            How the threshold is calculated
          </div>
          {methodologyOpen
            ? <ChevronUp className="h-3.5 w-3.5 text-slate-400" />
            : <ChevronDown className="h-3.5 w-3.5 text-slate-400" />}
        </button>

        {methodologyOpen && (
          <div className="mt-2 space-y-2 rounded-lg border border-blue-100 bg-blue-50/60 p-3 text-xs leading-5 text-slate-700">
            <p>
              <strong className="text-slate-900">Formula:</strong>{" "}
              Max Loss = {AVAILABLE_MINUTES} min − Expected Runtime − Baseline Downtime
            </p>
            <p>
              <strong className="text-slate-900">Expected Runtime</strong> is computed
              per complexity segment: print order ÷ P{SPEED_PERCENTILE} speed for that
              complexity type, converted to minutes. For segments without print order
              data the actual segment minutes are scaled by the optimal speed ratio.
              Summing across all segments gives the minimum time the machine would have
              needed on a strong performance day.
            </p>
            <p>
              <strong className="text-slate-900">Why P{SPEED_PERCENTILE} speed?</strong>{" "}
              The 50th percentile (median) accepts average-day performance as the
              norm, which embeds existing inefficiencies. The 90th is too aspirational
              and would flag almost every day. The 75th percentile is achievable in
              roughly one in four runs without exceptional conditions — ambitious but
              real.
            </p>
            <p>
              <strong className="text-slate-900">Baseline Downtime</strong> uses
              P{DOWNTIME_PERCENTILE} of observed daily downtime — a day with minimal
              but non-zero machine interruptions. Using zero downtime as the floor
              would produce thresholds that can never be met; the 25th percentile
              sets a fair, achievable baseline.
            </p>
            <p>
              <strong className="text-slate-900">Why complexity matters:</strong>{" "}
              Complex variants (SNP C4, GNP C9–C15) run significantly slower than
              simple variants. A heavy-complex day leaves far less room for lost time
              within the 240-minute window before risking a 04:00 AM overrun.
              The threshold self-adjusts — it is lower on complex-mix days and higher
              on fast, simple-print days.
            </p>
            {trustedKeys.length < COMPLEXITY_ORDER.length && (
              <p className="text-amber-700">
                <strong>Note:</strong> Baselines marked with a low sample count
                (&lt; {MIN_SAMPLE_SIZE} observations) are less reliable and shown
                with reduced confidence.
              </p>
            )}
          </div>
        )}
      </div>

      {/* Complexity baselines table */}
      <div className="border-t border-slate-100 px-3 py-3">
        <h3 className="mb-2 text-xs font-semibold uppercase tracking-normal text-slate-500">
          Learned speed baselines
        </h3>
        <div className="overflow-x-auto">
          <table className="w-full min-w-[540px] text-sm">
            <thead>
              <tr className="border-b border-slate-100 text-left text-xs font-semibold uppercase tracking-normal text-slate-400">
                <th className="pb-2 pr-4">Complexity</th>
                <th className="pb-2 pr-4 text-right">P{SPEED_PERCENTILE} speed</th>
                <th className="pb-2 pr-4 text-right">Median speed</th>
                <th className="pb-2 pr-4 text-right">P{100 - SPEED_PERCENTILE} speed</th>
                <th className="pb-2 pr-4 text-right">Min / 100k PO</th>
                <th className="pb-2 text-right">Observations</th>
              </tr>
            </thead>
            <tbody className="divide-y divide-slate-50">
              {COMPLEXITY_ORDER.filter((k) => baselines[k]).map((key) => {
                const b = baselines[key];
                const minsPerHundredK = b.p75 > 0 ? (100_000 / b.p75) * 60 : 0;
                const trusted = b.count >= MIN_SAMPLE_SIZE;
                return (
                  <tr key={key} className={trusted ? "" : "opacity-60"}>
                    <td className="py-2 pr-4">
                      <ComplexityBadge complexityKey={key} />
                    </td>
                    <td className="py-2 pr-4 text-right font-mono font-semibold text-slate-800">
                      {formatSpeed(b.p75)}
                    </td>
                    <td className="py-2 pr-4 text-right font-mono text-slate-500">
                      {formatSpeed(b.p50)}
                    </td>
                    <td className="py-2 pr-4 text-right font-mono text-slate-400">
                      {formatSpeed(b.p25)}
                    </td>
                    <td className="py-2 pr-4 text-right font-mono text-slate-600">
                      {minsPerHundredK > 0 ? `${formatN(minsPerHundredK)} min` : "—"}
                    </td>
                    <td className="py-2 text-right text-slate-400">
                      {b.count}
                      {!trusted && <span className="ml-1 text-amber-500">⚠</span>}
                    </td>
                  </tr>
                );
              })}
            </tbody>
          </table>
          <p className="mt-2 text-xs text-slate-400">
            Downtime floor (P{DOWNTIME_PERCENTILE} across all folder-days):{" "}
            <span className="font-mono font-semibold text-slate-600">{formatN(p25Downtime)} min</span>
            {" · "}
            Threshold = {AVAILABLE_MINUTES} min − expected runtime at P{SPEED_PERCENTILE} speed − {formatN(p25Downtime)} min
          </p>
        </div>
      </div>

      {/* Deviation table */}
      <div className="border-t border-slate-100 px-3 pb-3">
        <h3 className="mb-2 mt-3 text-xs font-semibold uppercase tracking-normal text-slate-500">
          {deviations.length > 0
            ? `${deviations.length} folder-day${deviations.length === 1 ? "" : "s"} exceeded threshold`
            : "No threshold breaches in this selection"}
        </h3>
        {deviations.length > 0 && (
          <div className="overflow-x-auto">
            <table className="w-full min-w-[680px] text-sm">
              <thead>
                <tr className="border-b border-slate-100 text-left text-xs font-semibold uppercase tracking-normal text-slate-400">
                  <th className="pb-2 pr-4">Date</th>
                  <th className="pb-2 pr-4">Folder</th>
                  <th className="pb-2 pr-4">Complexity mix</th>
                  <th className="pb-2 pr-4 text-right">Threshold</th>
                  <th className="pb-2 pr-4 text-right">Actual loss</th>
                  <th className="pb-2 text-right">Excess</th>
                </tr>
              </thead>
              <tbody className="divide-y divide-slate-50">
                {deviations.map((row) => (
                  <DeviationRow key={`${row.run_date}||${row.folder}`} row={row} />
                ))}
              </tbody>
            </table>
          </div>
        )}
      </div>
    </section>
  );
}

// ─── Sub-components ──────────────────────────────────────────────────────────

function ComplexityBadge({ complexityKey }) {
  const meta = COMPLEXITY_META[complexityKey] || COMPLEXITY_META.unknown;
  return (
    <span className={`inline-block rounded-md border px-2 py-0.5 text-xs font-medium ${meta.color}`}>
      {meta.label}
    </span>
  );
}

function ComplexityMixPills({ segments }) {
  if (!Array.isArray(segments) || !segments.length) {
    return <span className="text-xs text-slate-400">—</span>;
  }

  const totalMinutes = segments.reduce((s, seg) => s + Number(seg.minutes || 0), 0);
  if (totalMinutes <= 0) return <span className="text-xs text-slate-400">—</span>;

  const grouped = {};
  for (const seg of segments) {
    const key = seg.key || "unknown";
    grouped[key] = (grouped[key] || 0) + Number(seg.minutes || 0);
  }

  return (
    <div className="flex flex-wrap gap-1">
      {COMPLEXITY_ORDER.filter((k) => grouped[k] > 0).map((key) => {
        const pct = Math.round((grouped[key] / totalMinutes) * 100);
        const meta = COMPLEXITY_META[key] || COMPLEXITY_META.unknown;
        return (
          <span key={key} className={`inline-block rounded-md border px-1.5 py-0.5 text-xs font-medium ${meta.color}`}>
            {meta.label} {pct}%
          </span>
        );
      })}
    </div>
  );
}

function DeviationRow({ row }) {
  return (
    <tr className="align-top">
      <td className="py-2 pr-4 font-medium text-slate-800">
        {formatDate(row.run_date)}
      </td>
      <td className="py-2 pr-4 text-slate-700">
        {formatFolderName(row.folder)}
      </td>
      <td className="py-2 pr-4">
        <ComplexityMixPills segments={row.runtime_segments} />
      </td>
      <td className="py-2 pr-4 text-right font-mono text-slate-600">
        {formatN(row.threshold)} min
      </td>
      <td className="py-2 pr-4 text-right font-mono text-slate-800">
        {formatN(row.lost_time)} min
      </td>
      <td className="py-2 text-right">
        <span className="inline-block rounded-md bg-red-50 px-2 py-0.5 text-xs font-semibold text-red-700">
          +{formatN(row.excess)} min
        </span>
      </td>
    </tr>
  );
}

// ─── Analysis engine ─────────────────────────────────────────────────────────

function buildAnalysis(details) {
  if (!details.length) {
    return { baselines: {}, p25Downtime: 0, deviations: [], totalFolderDays: 0, trustedKeys: [] };
  }

  // Collect speed samples per complexity key and downtime per folder-day
  const speedSamples = {};
  const downtimeSamples = [];

  for (const row of details) {
    downtimeSamples.push(Number(row.downtime || 0));
    for (const seg of row.runtime_segments || []) {
      const key = seg.key || "unknown";
      const speed = Number(seg.effective_speed || 0);
      if (speed > 0) {
        if (!speedSamples[key]) speedSamples[key] = [];
        speedSamples[key].push(speed);
      }
    }
  }

  // Build per-complexity baselines
  const baselines = {};
  for (const [key, speeds] of Object.entries(speedSamples)) {
    if (!speeds.length) continue;
    baselines[key] = {
      p75: pct(speeds, 75),
      p50: pct(speeds, 50),
      p25: pct(speeds, 25),
      count: speeds.length,
    };
  }

  const p25Downtime = pct(downtimeSamples, DOWNTIME_PERCENTILE);
  const trustedKeys = Object.entries(baselines)
    .filter(([, b]) => b.count >= MIN_SAMPLE_SIZE)
    .map(([k]) => k);

  // Compute threshold for every folder-day and flag deviations
  const deviations = [];
  for (const row of details) {
    const { threshold, expectedRuntime } = computeThreshold(row, baselines, p25Downtime);
    if (threshold < 0) continue; // print order alone fills window — skip

    const lostTime = Number(row.lost_time || 0);
    const excess = lostTime - threshold;
    if (excess > 0) {
      deviations.push({ ...row, threshold, expectedRuntime, excess });
    }
  }

  // Sort: largest excess first, then by date
  deviations.sort((a, b) => b.excess - a.excess || String(a.run_date).localeCompare(String(b.run_date)));

  return {
    baselines,
    p25Downtime,
    deviations,
    totalFolderDays: details.length,
    trustedKeys,
  };
}

function computeThreshold(row, baselines, p25Downtime) {
  const segments = row.runtime_segments || [];
  let expectedRuntime = 0;

  for (const seg of segments) {
    const key = seg.key || "unknown";
    const p75Speed = baselines[key]?.p75 || 0;
    const printOrder = Number(seg.print_order || 0);
    const actualSpeed = Number(seg.effective_speed || 0);
    const actualMinutes = Number(seg.source_runtime_minutes || seg.minutes || 0);

    if (printOrder > 0 && p75Speed > 0) {
      // Primary path: scale print order by optimal speed
      // minutes = (print_order / speed_cph) * 60
      expectedRuntime += (printOrder / p75Speed) * 60;
    } else if (actualMinutes > 0 && actualSpeed > 0 && p75Speed > 0) {
      // Fallback: scale actual duration by speed ratio
      expectedRuntime += actualMinutes * (actualSpeed / p75Speed);
    } else {
      // No speed or PO data: use actual minutes as-is (conservative)
      expectedRuntime += actualMinutes;
    }
  }

  const threshold = AVAILABLE_MINUTES - expectedRuntime - p25Downtime;
  return { threshold: Math.max(0, threshold), expectedRuntime };
}

// ─── Helpers ─────────────────────────────────────────────────────────────────

function pct(values, p) {
  if (!values.length) return 0;
  const sorted = [...values].sort((a, b) => a - b);
  const idx = (p / 100) * (sorted.length - 1);
  const lo = Math.floor(idx);
  const hi = Math.ceil(idx);
  if (lo === hi) return sorted[lo];
  return sorted[lo] + (sorted[hi] - sorted[lo]) * (idx - lo);
}

function formatSpeed(value) {
  const n = Number(value || 0);
  if (n <= 0) return "—";
  if (n >= 1_000_000) return `${formatN(n / 1_000_000)}M`;
  if (n >= 1_000) return `${formatN(n / 1_000)}k`;
  return `${formatN(n)}`;
}

function formatN(value, decimals = 0) {
  return new Intl.NumberFormat("en-US", { maximumFractionDigits: decimals }).format(
    Number(value || 0)
  );
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
