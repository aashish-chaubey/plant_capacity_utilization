import { useMemo } from "react";
import { Gauge } from "lucide-react";

const AVAILABLE_MINUTES = 240;
const DEFAULT_CONFIG = {
  pWait: 50,
  pMot: 75,
  pSpare: 25,
  mediumConfidenceNights: 30,
  highConfidenceNights: 60,
};

const COMPLEXITY_ORDER = ["snp", "snp_complex", "gnp", "gnp_complex", "unknown"];
const COMPLEXITY_META = {
  snp: { label: "SNP", color: "bg-emerald-50 text-emerald-800 border-emerald-100" },
  snp_complex: { label: "SNP Complex", color: "bg-violet-50 text-violet-800 border-violet-100" },
  gnp: { label: "GNP", color: "bg-blue-50 text-blue-800 border-blue-100" },
  gnp_complex: { label: "GNP Complex", color: "bg-orange-50 text-orange-800 border-orange-100" },
  unknown: { label: "Unknown", color: "bg-slate-50 text-slate-600 border-slate-200" },
};

export default function MaximumAllowableLossTimeWidget({ details }) {
  const analysis = useMemo(() => buildMaltAnalysis(details || []), [details]);

  if (!details?.length || analysis.rows.length === 0) return null;

  return (
    <div className="mt-4 border-t border-slate-100 pt-4">
      <div className="mb-3 flex items-start gap-2">
        <span className="inline-flex h-8 w-8 shrink-0 items-center justify-center rounded-lg bg-amber-50 text-amber-700">
          <Gauge className="h-4 w-4" aria-hidden="true" />
        </span>
        <div>
          <h3 className="text-base font-semibold text-slate-950">
            Maximum Allowable Loss Time
          </h3>
          <p className="mt-1 text-sm text-slate-500">
            MALT = 240 - P{DEFAULT_CONFIG.pWait} wait - P{DEFAULT_CONFIG.pMot} machine operating time - P{DEFAULT_CONFIG.pSpare} spare.
          </p>
        </div>
      </div>

      <div className="overflow-hidden rounded-lg border border-slate-100">
        <table className="w-full min-w-[520px] text-sm">
          <thead className="bg-slate-50">
            <tr className="border-b border-slate-100 text-left text-xs font-semibold uppercase tracking-normal text-slate-500">
              <th className="px-3 py-2">Complexity</th>
              <th className="px-3 py-2 text-right">Maximum allowable loss time (MALT)</th>
            </tr>
          </thead>
          <tbody className="divide-y divide-slate-50 bg-white">
            {analysis.rows.map((row) => (
              <tr key={row.key} className="align-top">
                <td className="px-3 py-2">
                  <div className="flex flex-col gap-1">
                    <ComplexityBadge complexityKey={row.complexityKey} />
                    <span className="text-xs font-medium text-slate-500">
                      {row.scope}
                    </span>
                  </div>
                </td>
                <td className="px-3 py-2 text-right">
                  <div className="font-mono text-base font-bold text-slate-950">
                    {formatMinutes(row.malt)}
                  </div>
                  <div className="mt-1 flex flex-wrap justify-end gap-1.5">
                    <span className={`rounded-md border px-1.5 py-0.5 text-[11px] font-semibold ${confidenceClass(row.confidence)}`}>
                      {row.confidence}
                    </span>
                    <span className="rounded-md bg-slate-50 px-1.5 py-0.5 text-[11px] font-medium text-slate-500">
                      {formatNumber(row.onTimeNights)} on-time nights
                    </span>
                  </div>
                </td>
              </tr>
            ))}
          </tbody>
        </table>
      </div>

      <p className="mt-2 text-xs leading-5 text-slate-500">
        Calibrated only from on-time records that satisfy the 240-minute identity. Late editorial files directly reduce the effective loss budget on that night.
      </p>
    </div>
  );
}

function ComplexityBadge({ complexityKey }) {
  const meta = COMPLEXITY_META[complexityKey] || COMPLEXITY_META.unknown;
  return (
    <span className={`w-fit rounded-md border px-2 py-0.5 text-xs font-semibold ${meta.color}`}>
      {meta.label}
    </span>
  );
}

function buildMaltAnalysis(details) {
  const groups = new Map();

  for (const row of details) {
    const sample = buildMaltSample(row);
    if (!sample) continue;

    const key = [
      sample.plantName,
      sample.machine,
      sample.complexityKey,
    ].join("||");

    if (!groups.has(key)) {
      groups.set(key, {
        key,
        plantName: sample.plantName,
        machine: sample.machine,
        complexityKey: sample.complexityKey,
        waitValues: [],
        motValues: [],
        spareValues: [],
      });
    }

    const group = groups.get(key);
    group.waitValues.push(sample.wait);
    group.motValues.push(sample.mot);
    group.spareValues.push(sample.spare);
  }

  const rows = Array.from(groups.values())
    .map((group) => {
      const waitValue = percentile(group.waitValues, DEFAULT_CONFIG.pWait);
      const motValue = percentile(group.motValues, DEFAULT_CONFIG.pMot);
      const spareValue = percentile(group.spareValues, DEFAULT_CONFIG.pSpare);
      const malt = Math.max(AVAILABLE_MINUTES - waitValue - motValue - spareValue, 0);
      const onTimeNights = group.waitValues.length;

      return {
        key: group.key,
        plantName: group.plantName,
        machine: group.machine,
        complexityKey: group.complexityKey,
        scope: formatScope(group.plantName, group.machine),
        malt,
        onTimeNights,
        confidence: confidenceLabel(onTimeNights),
      };
    })
    .sort((first, second) => {
      const plantCompare = first.plantName.localeCompare(second.plantName);
      if (plantCompare) return plantCompare;
      const machineCompare = first.machine.localeCompare(second.machine);
      if (machineCompare) return machineCompare;
      return complexityRank(first.complexityKey) - complexityRank(second.complexityKey);
    });

  return { rows };
}

function buildMaltSample(row) {
  const wait = toFiniteNumber(row.waiting_time);
  const run = toFiniteNumber(row.runtime);
  const down = toFiniteNumber(row.downtime);
  const spare = toFiniteNumber(row.buffer_time);
  const idle = toFiniteNumber(row.idle_time) ?? 0;
  const totalLoss = toFiniteNumber(row.lost_time);
  const overrun = toFiniteNumber(row.overrun_minutes);

  if ([wait, run, down, spare, idle, totalLoss].some((value) => value === null || value < 0)) return null;
  if (overrun !== null && overrun > 0) return null;

  const operatingLoss = Math.max(totalLoss - wait, 0);
  const identityTotal = wait + operatingLoss + run + down + spare + idle;
  if (Math.abs(identityTotal - AVAILABLE_MINUTES) > 1) return null;

  const { machine } = parseFolderIdentity(row.folder);
  const complexity = dominantComplexity(row.runtime_segments);
  return {
    plantName: String(row.plant_name || "Unknown plant"),
    machine,
    complexityKey: complexity.key,
    wait,
    mot: run + down,
    spare,
  };
}

function dominantComplexity(segments) {
  if (!Array.isArray(segments) || segments.length === 0) {
    return { key: "unknown" };
  }

  return segments.reduce((best, segment) => {
    const minutes = Number(segment.minutes || 0);
    const bestMinutes = Number(best.minutes || 0);
    if (minutes > bestMinutes) return segment;
    return best;
  }, segments[0]);
}

function parseFolderIdentity(value) {
  const parts = String(value || "")
    .split("\n")
    .map((part) => part.trim())
    .filter(Boolean);

  return {
    machine: parts[0] || "Unknown machine",
    folder: parts.slice(1).join(" / "),
  };
}

function confidenceLabel(count) {
  if (count >= DEFAULT_CONFIG.highConfidenceNights) return "High confidence";
  if (count >= DEFAULT_CONFIG.mediumConfidenceNights) return "Medium confidence";
  return "Low confidence";
}

function confidenceClass(label) {
  if (label.startsWith("High")) return "border-emerald-100 bg-emerald-50 text-emerald-700";
  if (label.startsWith("Medium")) return "border-blue-100 bg-blue-50 text-blue-700";
  return "border-amber-100 bg-amber-50 text-amber-700";
}

function complexityRank(key) {
  const index = COMPLEXITY_ORDER.indexOf(key);
  return index === -1 ? COMPLEXITY_ORDER.length : index;
}

function percentile(values, p) {
  const sorted = values
    .map((value) => Number(value))
    .filter((value) => Number.isFinite(value))
    .sort((first, second) => first - second);

  if (sorted.length === 0) return 0;

  const index = (p / 100) * (sorted.length - 1);
  const lower = Math.floor(index);
  const upper = Math.ceil(index);
  if (lower === upper) return sorted[lower];
  return sorted[lower] + (sorted[upper] - sorted[lower]) * (index - lower);
}

function toFiniteNumber(value) {
  const number = Number(value);
  return Number.isFinite(number) ? number : null;
}

function formatScope(plantName, machine) {
  return [plantName, machine].filter(Boolean).join(" / ");
}

function formatMinutes(value) {
  return `${formatNumber(value)} min`;
}

function formatNumber(value) {
  return new Intl.NumberFormat("en-US", { maximumFractionDigits: 0 }).format(Number(value || 0));
}
