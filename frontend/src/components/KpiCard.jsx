import { Clock } from "lucide-react";

export default function KpiCard({
  label,
  value,
  valueLabel = "",
  detail = "",
  tone = "slate",
  utilizationDetail = "",
  availableDetail = ""
}) {
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
  const showCapacityDetails = Boolean(utilizationDetail && availableDetail);
  const style = selectedTone.bg
    ? {
        backgroundColor: selectedTone.bg,
        borderColor: selectedTone.border || selectedTone.bg,
        backgroundImage: selectedTone.pattern || "none",
      }
    : undefined;
  const valueStyle = { fontSize: "clamp(1.25rem, 1.6vw, 1.675rem)" };
  const secondaryStyle = { fontSize: "clamp(0.65rem, 0.75vw, 0.875rem)" };
  const utilizationCapacityDetail = splitDurationDetail(utilizationDetail);
  const availableCapacityDetail = splitDurationDetail(availableDetail);

  return (
    <article className={`min-w-[165px] rounded-lg border p-3 shadow-sm ${selectedTone.className}`} style={style}>
      {!showCapacityDetails && (
        <p className="text-xs font-bold uppercase leading-snug tracking-normal opacity-75">{label}</p>
      )}
      {showCapacityDetails ? (
        <>
          <div>
            <p className="text-xs font-bold uppercase leading-snug tracking-normal opacity-75">Capacity Utilization</p>
            <p className="mt-2 whitespace-nowrap text-slate-950">
              <span className="font-bold leading-none tracking-tight" style={valueStyle}>{utilizationCapacityDetail.amount}</span>
              <span className="ml-1 font-medium leading-snug text-slate-800 opacity-75" style={secondaryStyle}>
                {utilizationCapacityDetail.unit}
              </span>
            </p>
          </div>
          <div className="my-3 h-px bg-slate-900/10" />
          <div>
            <p className="text-xs font-bold uppercase leading-snug tracking-normal opacity-75">Available Time</p>
            <p className="mt-2 whitespace-nowrap text-slate-950">
              <span className="font-bold leading-none tracking-tight" style={valueStyle}>{availableCapacityDetail.amount}</span>
              <span className="ml-1 font-medium leading-snug text-slate-800 opacity-75" style={secondaryStyle}>
                {availableCapacityDetail.unit}
              </span>
            </p>
          </div>
        </>
      ) : (
        <>
          <p className="mt-3 whitespace-nowrap">
            <span className="font-bold leading-none tracking-tight" style={valueStyle}>{value}</span>
            {valueLabel && (
              <span className="ml-1 font-medium leading-snug opacity-60" style={secondaryStyle}>{valueLabel}</span>
            )}
          </p>
          {detail && (
            <p className="mt-3 flex min-w-0 items-center gap-1.5 text-xs font-semibold opacity-75">
              <Clock className="h-3.5 w-3.5 shrink-0" aria-hidden="true" />
              <span className="truncate">{detail}</span>
            </p>
          )}
        </>
      )}
    </article>
  );
}

function splitDurationDetail(value) {
  const text = String(value || "").trim();
  const match = text.match(/^([\d,.]+)(.*)$/);
  if (!match) return { amount: text, unit: "" };
  return { amount: match[1], unit: match[2].trim() };
}
