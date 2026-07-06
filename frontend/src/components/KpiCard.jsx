export default function KpiCard({ label, value, detail = "", tone = "slate" }) {
  const tones = {
    blue: "border-blue-200 bg-blue-50 text-blue-800",
    green: "border-emerald-200 bg-emerald-50 text-emerald-800",
    amber: "border-amber-200 bg-amber-50 text-amber-800",
    red: "border-red-200 bg-red-50 text-red-800",
    wait: "border-[#B0B0B0] bg-[#B0B0B0] text-slate-950",
    spare: "border-[#C5E1FF] bg-[#C5E1FF] text-slate-950",
    unplanned: "border-[#CBD5E1] bg-[#E5E7EB] text-slate-900",
    slate: "border-slate-200 bg-white text-slate-800"
  };
  const patternStyle = tone === "unplanned"
    ? {
        backgroundImage: "repeating-linear-gradient(135deg, rgba(100,116,139,0.38) 0 1px, transparent 1px 5px)",
      }
    : undefined;

  return (
    <article className={`rounded-lg border p-3 shadow-sm ${tones[tone] || tones.slate}`} style={patternStyle}>
      <p className="text-xs font-semibold uppercase tracking-normal opacity-75">{label}</p>
      <p className="mt-2 text-2xl font-semibold tracking-normal">{value}</p>
      {detail && <p className="mt-1 truncate text-xs font-semibold opacity-70">{detail}</p>}
    </article>
  );
}
