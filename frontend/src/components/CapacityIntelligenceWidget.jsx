import {
  AlertTriangle,
  BrainCircuit,
  Clock,
  Folder,
  Gauge,
  Zap
} from "lucide-react";

export default function CapacityIntelligenceWidget({ intelligence, loading, error }) {
  if (!intelligence && (loading || error)) {
    return (
      <section className="rounded-lg border border-slate-200 bg-white p-3 shadow-soft">
        <Header status={loading ? "Generating insights" : "Intelligence issue"} />
        {loading ? (
          <div className="mt-3 grid gap-2 sm:grid-cols-4">
            {[0, 1, 2, 3].map((item) => (
              <div key={item} className="h-14 animate-pulse rounded-lg bg-slate-100" />
            ))}
          </div>
        ) : (
          <StatusMessage message={error} />
        )}
      </section>
    );
  }

  const summary = intelligence?.summary || {};
  const sections = intelligence?.sections || {};
  const llm = intelligence?.llm || {};
  const llmSummary = intelligence?.llm_summary || {};
  const points = getSummaryPoints(llmSummary);
  const facts = buildFactCards(summary, sections);
  const actions = (llmSummary.recommended_actions || []).slice(0, 3);

  return (
    <section className="rounded-lg border border-slate-200 bg-white p-3 shadow-soft">
      <Header status={loading ? "Generating insights" : llmStatusText(llm, error)} />

      <div className="mt-3 grid gap-4 xl:grid-cols-[1.2fr_0.8fr]">
        <div className="min-w-0">
          <h3 className="text-base font-semibold leading-6 text-slate-950">
            {llmSummary.headline || "Executive capacity summary"}
          </h3>

          <div className="mt-3 rounded-lg border border-blue-100 bg-blue-50/70 p-3">
            <h4 className="text-xs font-semibold uppercase tracking-normal text-blue-900">
              Key Summary Points
            </h4>
            <div className="mt-2 space-y-2">
              {points.slice(0, 4).map((point) => (
                <SummaryPoint key={point} text={point} />
              ))}
            </div>
          </div>
        </div>

        <aside className="grid gap-2 sm:grid-cols-2 xl:grid-cols-1">
          {facts.map((fact) => (
            <FactCard key={fact.label} {...fact} />
          ))}
        </aside>
      </div>

      {actions.length > 0 && (
        <div className="mt-3 border-t border-slate-100 pt-3">
          <h4 className="text-xs font-semibold uppercase tracking-normal text-slate-500">
            Executive Focus
          </h4>
          <div className="mt-2 grid gap-2 lg:grid-cols-3">
            {actions.map((action) => (
              <ActionItem key={action} text={action} />
            ))}
          </div>
        </div>
      )}

      {(error || llm.status === "error" || llm.status === "unconfigured") && (
        <StatusMessage message={error || llm.message || "LLM synthesis is unavailable; deterministic findings are shown."} />
      )}
    </section>
  );
}

function Header({ status }) {
  return (
    <div className="flex flex-col gap-2 sm:flex-row sm:items-center sm:justify-between">
      <div className="flex items-center gap-2">
        <span className="inline-flex h-8 w-8 items-center justify-center rounded-lg bg-blue-50 text-blue-700">
          <BrainCircuit className="h-4 w-4" aria-hidden="true" />
        </span>
        <h2 className="text-base font-semibold text-slate-950">Capacity intelligence</h2>
      </div>
      <span className="text-sm text-slate-500">{status}</span>
    </div>
  );
}

function SummaryPoint({ text }) {
  return (
    <div className="flex gap-2 text-sm leading-6 text-slate-800">
      <span className="mt-2 h-1.5 w-1.5 shrink-0 rounded-full bg-blue-700" />
      <span>{text}</span>
    </div>
  );
}

function FactCard({ icon, label, value, detail, tone }) {
  const toneClass = {
    amber: "border-amber-100 bg-amber-50 text-amber-800",
    blue: "border-blue-100 bg-blue-50 text-blue-800",
    green: "border-emerald-100 bg-emerald-50 text-emerald-800",
    red: "border-red-100 bg-red-50 text-red-800"
  }[tone] || "border-slate-100 bg-slate-50 text-slate-700";

  return (
    <div className={`rounded-lg border px-3 py-2 ${toneClass}`}>
      <div className="flex items-center gap-1.5 text-xs font-semibold uppercase tracking-normal">
        {icon}
        <span>{label}</span>
      </div>
      <p className="mt-1 text-sm font-bold text-slate-950">{value}</p>
      {detail && <p className="mt-0.5 text-xs leading-5 text-slate-600">{detail}</p>}
    </div>
  );
}

function ActionItem({ text }) {
  return (
    <div className="rounded-lg border border-slate-100 bg-slate-50 px-3 py-2 text-sm leading-6 text-slate-700">
      {text}
    </div>
  );
}

function StatusMessage({ message }) {
  return (
    <div className="mt-3 flex items-start gap-2 rounded-lg border border-amber-200 bg-amber-50 p-3 text-sm text-amber-900">
      <AlertTriangle className="mt-0.5 h-4 w-4 shrink-0" aria-hidden="true" />
      <span>{message}</span>
    </div>
  );
}

function buildFactCards(summary, sections) {
  const complexity = sections?.complexity_speed || {};
  const folderUtilization = sections?.folder_utilization || {};
  const lossTime = sections?.loss_time || {};
  const fastest = complexity.fastest_folders?.[0];
  const slowest = complexity.slowest_folders?.[0];
  const highLossFolder = folderUtilization.highest_loss_share?.[0];
  const peakDay = lossTime.peak_day;

  return [
    {
      icon: <Gauge className="h-3.5 w-3.5" aria-hidden="true" />,
      label: "Complexity impact",
      value: formatImpact(summary.complexity_speed_gap_percentage),
      detail: `${formatPercent(summary.complex_runtime_share_percentage)} runtime in SNP/GNP complex variants`,
      tone: "amber"
    },
    {
      icon: <Zap className="h-3.5 w-3.5" aria-hidden="true" />,
      label: "Speed comparison",
      value: fastest && slowest ? `${shortName(fastest.resource)} vs ${shortName(slowest.resource)}` : formatSpeed(summary.average_speed_cph),
      detail: fastest && slowest
        ? `${formatSpeed(fastest.average_speed_cph)} against ${formatSpeed(slowest.average_speed_cph)}`
        : "Average folder speed",
      tone: "blue"
    },
    {
      icon: <Folder className="h-3.5 w-3.5" aria-hidden="true" />,
      label: "Folder utilization",
      value: `${formatNumber(summary.folder_utilization_range_percentage_points)} pt spread`,
      detail: highLossFolder ? `${shortName(highLossFolder.resource)} has ${formatPercent(highLossFolder.loss_share_percentage)} loss share` : `${formatPercent(summary.average_folder_utilization_percentage)} average`,
      tone: "green"
    },
    {
      icon: <Clock className="h-3.5 w-3.5" aria-hidden="true" />,
      label: "Loss time",
      value: summary.dominant_loss_driver || "NA",
      detail: peakDay ? `${peakDay.run_date}: ${formatMinutes(peakDay.lost_time_minutes)} lost` : `${formatMinutes(summary.total_loss_time_minutes)} total`,
      tone: "red"
    }
  ];
}

function getSummaryPoints(llmSummary) {
  const points = llmSummary.key_summary_points || llmSummary.observations || [];
  if (points.length > 0) return points;
  return ["No key summary points available for this selection."];
}

function llmStatusText(llm, error) {
  if (error) return "Intelligence issue";
  if (llm.status === "ready") return "LLM summary ready";
  if (llm.status === "disabled") return "Deterministic insights";
  if (llm.status === "unconfigured") return "Deterministic insights";
  if (llm.status === "error") return "Deterministic insights";
  return "Waiting for insights";
}

function formatImpact(value) {
  const numeric = Number(value || 0);
  if (numeric <= 0) return "No drag";
  return `${formatNumber(numeric)}% slower`;
}

function formatSpeed(value) {
  const speed = Number(value || 0);
  if (speed <= 0) return "0 cph";
  if (speed >= 1000) return `${formatNumber(speed / 1000)}k cph`;
  return `${formatNumber(speed)} cph`;
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

function shortName(value) {
  const parts = String(value || "").split("/").map((part) => part.trim()).filter(Boolean);
  return parts[parts.length - 1] || String(value || "");
}
