import DelayedPrintFinishWidget from "./DelayedPrintFinishWidget.jsx";

export default function CapacityIntelligenceWidget({ details }) {
  return (
    <section className="rounded-lg border border-slate-200 bg-white p-3 shadow-soft">
      <DelayedPrintFinishWidget details={details} />
    </section>
  );
}
