export default function KpiCard({ label, value, tone = "slate" }) {
  const tones = {
    blue: "border-blue-200 bg-blue-50 text-blue-800",
    green: "border-emerald-200 bg-emerald-50 text-emerald-800",
    amber: "border-amber-200 bg-amber-50 text-amber-800",
    red: "border-red-200 bg-red-50 text-red-800",
    slate: "border-slate-200 bg-white text-slate-800"
  };

  return (
    <article className={`rounded-lg border p-4 shadow-sm ${tones[tone] || tones.slate}`}>
      <p className="text-xs font-semibold uppercase tracking-normal opacity-75">{label}</p>
      <p className="mt-2 text-2xl font-semibold tracking-normal">{value}</p>
    </article>
  );
}
