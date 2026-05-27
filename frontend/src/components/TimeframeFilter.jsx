import { CalendarRange, RotateCcw } from "lucide-react";

const MODES = [
  ["annual", "Annually"],
  ["half", "Half yearly"],
  ["quarter", "Quarterly"],
  ["month", "Monthly"],
  ["custom", "Custom"]
];

export default function TimeframeFilter({
  mode,
  periodKey,
  periodOptions,
  customStart,
  customEnd,
  isCleared,
  rangeLabel,
  onModeChange,
  onPeriodChange,
  onCustomRangeChange,
  onClear
}) {
  const isCustom = mode === "custom";

  return (
    <section className="rounded-lg border border-slate-200 bg-white px-4 py-3 shadow-soft">
      <div className="flex flex-col gap-3">
        <div className="flex min-w-0 items-center justify-between gap-3">
          <div className="flex min-w-0 items-center gap-2">
            <CalendarRange className="h-4 w-4 shrink-0 text-slate-500" aria-hidden="true" />
            <h2 className="text-sm font-semibold text-slate-950">Timeframe</h2>
          </div>
          <div className="flex min-w-0 items-center gap-2">
            <p className="truncate text-xs font-medium text-slate-500">{rangeLabel}</p>
            <button
              type="button"
              onClick={onClear}
              disabled={isCleared}
              className="inline-flex min-h-8 shrink-0 items-center gap-1.5 rounded-md border border-slate-200 px-2.5 text-xs font-semibold text-slate-600 transition hover:border-slate-300 hover:bg-slate-50 hover:text-slate-950 disabled:cursor-not-allowed disabled:opacity-50"
            >
              <RotateCcw className="h-3.5 w-3.5" aria-hidden="true" />
              Clear
            </button>
          </div>
        </div>

        <div className="flex flex-col gap-3 2xl:flex-row 2xl:items-center 2xl:justify-between">
          <div className="grid gap-1 rounded-lg border border-slate-200 bg-slate-50 p-1 sm:grid-cols-5">
            {MODES.map(([value, label]) => (
              <button
                key={value}
                type="button"
                onClick={() => onModeChange(value)}
                className={`min-h-9 whitespace-nowrap rounded-md px-3 text-sm font-semibold transition ${
                  !isCleared && mode === value
                    ? "bg-blue-700 text-white shadow-sm"
                    : "text-slate-600 hover:bg-white hover:text-slate-950"
                }`}
              >
                {label}
              </button>
            ))}
          </div>

          {isCustom ? (
            <div className="grid gap-2 sm:grid-cols-2">
              <label className="flex min-h-10 items-center gap-2 rounded-lg border border-slate-200 bg-white px-3 text-sm text-slate-600">
                <span className="shrink-0 text-xs font-semibold uppercase tracking-normal text-slate-500">From</span>
                <input
                  type="date"
                  value={customStart}
                  onChange={(event) => onCustomRangeChange("customStart", event.target.value)}
                  className="min-h-8 min-w-0 flex-1 rounded-md border-0 bg-transparent px-0 text-slate-900 outline-none"
                />
              </label>
              <label className="flex min-h-10 items-center gap-2 rounded-lg border border-slate-200 bg-white px-3 text-sm text-slate-600">
                <span className="shrink-0 text-xs font-semibold uppercase tracking-normal text-slate-500">To</span>
                <input
                  type="date"
                  value={customEnd}
                  onChange={(event) => onCustomRangeChange("customEnd", event.target.value)}
                  className="min-h-8 min-w-0 flex-1 rounded-md border-0 bg-transparent px-0 text-slate-900 outline-none"
                />
              </label>
            </div>
          ) : (
            <label className="flex min-h-10 items-center gap-2 rounded-lg border border-slate-200 bg-white px-3 text-sm text-slate-600 2xl:min-w-56">
              <span className="shrink-0 text-xs font-semibold uppercase tracking-normal text-slate-500">Period</span>
              <select
                value={periodKey}
                onChange={(event) => onPeriodChange(event.target.value)}
                className="min-h-8 min-w-0 flex-1 rounded-md border-0 bg-transparent px-0 text-slate-900 outline-none"
              >
                {periodOptions.length === 0 && <option value="">No dates</option>}
                {periodOptions.map((option) => (
                  <option key={option.key} value={option.key}>
                    {option.label}
                  </option>
                ))}
              </select>
            </label>
          )}
        </div>
      </div>
    </section>
  );
}
