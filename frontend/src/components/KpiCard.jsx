import { Clock } from "lucide-react";

export default function KpiCard({ label, value, valuePlanned, valueAvailable, detail = "", tone = "slate" }) {
  const tones = {
    blue: { className: "border-blue-200 bg-blue-50 text-blue-800" },
    green: { className: "text-slate-950", bg: "#B2CFB2", border: "#B2CFB2" },
    amber: { className: "text-slate-950", bg: "#F3C97B", border: "#F3C97B" },
    red: { className: "text-slate-950", bg: "#FF9AA2", border: "#FF9AA2" },
    wait: { className: "text-slate-950", bg: "#B0B0B0", border: "#B0B0B0" },
    spare: { className: "text-slate-950", bg: "#C5E1FF", border: "#C5E1FF" },
    unplanned: {
      className: "text-slate-900",
      bg: "#E5E7EB",
      border: "#E5E7EB",
      pattern: "repeating-linear-gradient(135deg, #B4BBC7 0 1px, transparent 1px 5px)",
    },
    slate: { className: "border-slate-200 bg-white text-slate-800" }
  };
  const selectedTone = tones[tone] || tones.slate;
  const style = selectedTone.bg
    ? {
        backgroundColor: selectedTone.bg,
        borderColor: selectedTone.border || selectedTone.bg,
        backgroundImage: selectedTone.pattern || "none",
      }
    : undefined;

  return (
    <article className={`min-w-[230px] rounded-lg border p-4 shadow-sm ${selectedTone.className}`} style={style}>
      <p className="text-sm font-bold uppercase leading-snug tracking-normal opacity-75">{label}</p>
      {valuePlanned != null ? (
        <>
          <p className="mt-5 whitespace-nowrap">
            <span className="text-3xl font-bold leading-none tracking-tight">{valuePlanned}</span>
            <span className="ml-1 text-sm font-medium leading-snug opacity-60">of Planned Time</span>
          </p>
          {detail && (
            <p className="mt-4 flex min-w-0 items-center gap-1.5 text-sm font-semibold opacity-75">
              <Clock className="h-3.5 w-3.5 shrink-0" aria-hidden="true" />
              <span className="truncate">{detail}</span>
            </p>
          )}
          <p className="mt-4 whitespace-nowrap">
            <span className="text-sm font-bold leading-snug opacity-75">{valueAvailable}</span>
            <span className="ml-1 text-sm font-medium leading-snug opacity-55">of Available Time</span>
          </p>
        </>
      ) : (
        <>
          <p className="mt-5 text-3xl font-bold leading-none tracking-tight">{value}</p>
          {detail && (
            <p className="mt-4 flex min-w-0 items-center gap-1.5 text-sm font-semibold opacity-75">
              <Clock className="h-3.5 w-3.5 shrink-0" aria-hidden="true" />
              <span className="truncate">{detail}</span>
            </p>
          )}
        </>
      )}
    </article>
  );
}
