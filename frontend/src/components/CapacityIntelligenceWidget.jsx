import {
  AlertTriangle,
  BrainCircuit
} from "lucide-react";

import DelayedPrintFinishWidget from "./DelayedPrintFinishWidget.jsx";
import MaximumAllowableLossTimeWidget from "./MaximumAllowableLossTimeWidget.jsx";
import TowerAvailabilitySummaryWidget from "./TowerAvailabilitySummaryWidget.jsx";

export default function CapacityIntelligenceWidget({ intelligence, loading, error, details, towerDetails, daily }) {
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
        <MaximumAllowableLossTimeWidget details={details} />
        <DelayedPrintFinishWidget details={details} />
        <TowerAvailabilitySummaryWidget towerDetails={towerDetails} daily={daily} />
      </section>
    );
  }

  const summary = intelligence?.summary || {};
  const sections = intelligence?.sections || {};
  const llm = intelligence?.llm || {};
  const llmSummary = intelligence?.llm_summary || {};
  const points = getSummaryPoints(llmSummary);
  const actions = (llmSummary.recommended_actions || []).slice(0, 3);

  return (
    <section className="rounded-lg border border-slate-200 bg-white p-3 shadow-soft">
      <Header status={loading ? "Generating insights" : llmStatusText(llm, error)} />

      <div className="mt-3 min-w-0">
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

      {(error || llm.status === "error" || llm.status === "unconfigured") && (
        <StatusMessage message={error || llm.message || "LLM synthesis is unavailable; deterministic findings are shown."} />
      )}

      <MaximumAllowableLossTimeWidget details={details} />
      <DelayedPrintFinishWidget details={details} />
      <TowerAvailabilitySummaryWidget towerDetails={towerDetails} daily={daily} />
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
