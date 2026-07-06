export default function KpiCard({ label, value, detail = "", tone = "slate" }) {
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
    <article className={`rounded-lg border p-3 shadow-sm ${selectedTone.className}`} style={style}>
      <p className="text-xs font-semibold uppercase tracking-normal opacity-75">{label}</p>
      <p className="mt-2 text-2xl font-semibold tracking-normal">{value}</p>
      {detail && <p className="mt-1 truncate text-xs font-semibold opacity-70">{detail}</p>}
    </article>
  );
}
