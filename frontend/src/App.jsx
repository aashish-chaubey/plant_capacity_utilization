import { AlertCircle } from "lucide-react";
import { useEffect, useMemo, useState } from "react";

import Dashboard from "./components/Dashboard.jsx";
import TimeframeFilter from "./components/TimeframeFilter.jsx";
import UploadPanel from "./components/UploadPanel.jsx";

const API_BASE_URL = import.meta.env.VITE_API_BASE_URL || "http://127.0.0.1:8000";
const PERIOD_MODES = ["annual", "half", "quarter", "month"];
const MODE_LABELS = {
  annual: "Annual",
  half: "Half yearly",
  quarter: "Quarterly",
  month: "Monthly"
};

export default function App() {
  const [result, setResult] = useState(null);
  const [errors, setErrors] = useState([]);
  const [loading, setLoading] = useState(false);
  const [fileName, setFileName] = useState("");
  const [timeframe, setTimeframe] = useState(createDefaultTimeframe);

  const periodOptions = useMemo(
    () => buildPeriodOptions(result?.daily || []),
    [result]
  );

  useEffect(() => {
    if (!result?.daily?.length) return;

    setTimeframe((current) => {
      const nextPeriods = { ...current.periods };
      let changed = false;

      for (const mode of PERIOD_MODES) {
        const options = periodOptions[mode] || [];
        const hasCurrentPeriod = options.some((option) => option.key === nextPeriods[mode]);
        const nextKey = hasCurrentPeriod ? nextPeriods[mode] : options[0]?.key || "";

        if (nextPeriods[mode] !== nextKey) {
          nextPeriods[mode] = nextKey;
          changed = true;
        }
      }

      const bounds = getDateBounds(result.daily);
      const customStart = current.customStart || bounds.start;
      const customEnd = current.customEnd || bounds.end;

      if (customStart !== current.customStart || customEnd !== current.customEnd) {
        changed = true;
      }

      if (!changed) return current;

      return {
        ...current,
        periods: nextPeriods,
        customStart,
        customEnd
      };
    });
  }, [periodOptions, result]);

  const timeframeRange = useMemo(
    () => resolveTimeframeRange(timeframe, periodOptions, result?.daily || []),
    [periodOptions, result, timeframe]
  );

  const filteredResult = useMemo(
    () => filterCapacityData(result, timeframeRange),
    [result, timeframeRange]
  );

  async function handleUpload(file) {
    if (!file) return;

    setLoading(true);
    setErrors([]);
    setFileName(file.name);
    setTimeframe(createDefaultTimeframe());

    const formData = new FormData();
    formData.append("file", file);

    try {
      const response = await fetch(`${API_BASE_URL}/api/upload`, {
        method: "POST",
        body: formData
      });

      if (!response.ok) {
        throw new Error(`Upload failed with status ${response.status}`);
      }

      const payload = await response.json();
      if (!payload.valid) {
        setResult(null);
        setErrors(payload.errors || ["The workbook could not be processed."]);
        return;
      }

      setResult(payload);
    } catch (error) {
      setResult(null);
      setErrors([error.message || "Unable to connect to the backend API."]);
    } finally {
      setLoading(false);
    }
  }

  function handleModeChange(mode) {
    setTimeframe((current) => ({ ...current, mode, isCleared: false }));
  }

  function handlePeriodChange(periodKey) {
    setTimeframe((current) => ({
      ...current,
      isCleared: false,
      periods: {
        ...current.periods,
        [current.mode]: periodKey
      }
    }));
  }

  function handleCustomRangeChange(field, value) {
    setTimeframe((current) => ({
      ...current,
      isCleared: false,
      [field]: value
    }));
  }

  function handleClearTimeframe() {
    setTimeframe((current) => ({
      ...current,
      isCleared: true
    }));
  }

  return (
    <div className="min-h-screen bg-[#f6f8fb] text-slate-900">
      <header className="border-b border-slate-200 bg-white">
        <div className="mx-auto flex max-w-7xl flex-col gap-3 px-4 py-5 sm:px-6 lg:flex-row lg:items-center lg:justify-between lg:px-8">
          <div>
            <h1 className="text-2xl font-semibold tracking-normal text-slate-950">
              Plant Capacity Utilization
            </h1>
            <p className="mt-1 text-sm text-slate-500">
              Daily 00:00-04:00 production window across active machine-folder units
            </p>
          </div>
          <div className="text-sm text-slate-500">
            {fileName ? <span className="font-medium text-slate-700">{fileName}</span> : "No report loaded"}
          </div>
        </div>
      </header>

      <main className="mx-auto max-w-7xl px-4 py-6 sm:px-6 lg:px-8">
        <div className={result ? "grid gap-5 xl:grid-cols-[0.8fr_1.2fr]" : ""}>
          <UploadPanel onUpload={handleUpload} loading={loading} compact={Boolean(result)} />

          {result && (
            <TimeframeFilter
              mode={timeframe.mode}
              periodKey={timeframe.periods[timeframe.mode] || ""}
              periodOptions={periodOptions[timeframe.mode] || []}
              customStart={timeframe.customStart}
              customEnd={timeframe.customEnd}
              isCleared={timeframe.isCleared}
              rangeLabel={timeframeRange?.label || "No production dates loaded"}
              onModeChange={handleModeChange}
              onPeriodChange={handlePeriodChange}
              onCustomRangeChange={handleCustomRangeChange}
              onClear={handleClearTimeframe}
            />
          )}
        </div>

        {errors.length > 0 && (
          <section className="mt-5 rounded-lg border border-red-200 bg-red-50 p-4 text-red-900">
            <div className="flex items-start gap-3">
              <AlertCircle className="mt-0.5 h-5 w-5 shrink-0" aria-hidden="true" />
              <div>
                <h2 className="text-sm font-semibold">Upload issue</h2>
                <ul className="mt-2 space-y-1 text-sm">
                  {errors.map((error) => (
                    <li key={error}>{error}</li>
                  ))}
                </ul>
              </div>
            </div>
          </section>
        )}

        {filteredResult?.daily?.length > 0 && <Dashboard data={filteredResult} />}

        {filteredResult && filteredResult.daily.length === 0 && errors.length === 0 && (
          <section className="mt-6 rounded-lg border border-slate-200 bg-white p-6 text-slate-700 shadow-soft">
            <h2 className="text-base font-semibold text-slate-950">No rows in this timeframe</h2>
            <p className="mt-1 text-sm text-slate-500">
              The selected range does not contain production dates from the uploaded report.
            </p>
          </section>
        )}
      </main>
    </div>
  );
}

function createDefaultTimeframe() {
  return {
    mode: "annual",
    isCleared: false,
    periods: {
      annual: "",
      half: "",
      quarter: "",
      month: ""
    },
    customStart: "",
    customEnd: ""
  };
}

function buildPeriodOptions(dailyRows) {
  const periodMaps = {
    annual: new Map(),
    half: new Map(),
    quarter: new Map(),
    month: new Map()
  };

  for (const row of dailyRows) {
    const dateParts = parseDateKey(row.run_date);
    if (!dateParts) continue;

    const { year, month } = dateParts;
    const half = month <= 6 ? 1 : 2;
    const quarter = Math.ceil(month / 3);

    addPeriod(periodMaps.annual, {
      key: String(year),
      label: String(year),
      start: formatDateKey(year, 1, 1),
      end: formatDateKey(year, 12, 31)
    });

    addPeriod(periodMaps.half, {
      key: `${year}-H${half}`,
      label: `H${half} ${year}`,
      start: formatDateKey(year, half === 1 ? 1 : 7, 1),
      end: formatDateKey(year, half === 1 ? 6 : 12, half === 1 ? 30 : 31)
    });

    const quarterStartMonth = (quarter - 1) * 3 + 1;
    const quarterEndMonth = quarterStartMonth + 2;
    addPeriod(periodMaps.quarter, {
      key: `${year}-Q${quarter}`,
      label: `Q${quarter} ${year}`,
      start: formatDateKey(year, quarterStartMonth, 1),
      end: formatDateKey(year, quarterEndMonth, daysInMonth(year, quarterEndMonth))
    });

    addPeriod(periodMaps.month, {
      key: `${year}-${pad(month)}`,
      label: formatMonthLabel(year, month),
      start: formatDateKey(year, month, 1),
      end: formatDateKey(year, month, daysInMonth(year, month))
    });
  }

  return {
    annual: sortPeriodOptions(periodMaps.annual),
    half: sortPeriodOptions(periodMaps.half),
    quarter: sortPeriodOptions(periodMaps.quarter),
    month: sortPeriodOptions(periodMaps.month)
  };
}

function addPeriod(periodMap, option) {
  if (!periodMap.has(option.key)) {
    periodMap.set(option.key, option);
  }
}

function sortPeriodOptions(periodMap) {
  return Array.from(periodMap.values()).sort((a, b) => b.start.localeCompare(a.start));
}

function resolveTimeframeRange(timeframe, periodOptions, dailyRows) {
  if (!dailyRows.length) return null;

  if (timeframe.isCleared) {
    const bounds = getDateBounds(dailyRows);

    return {
      key: "all",
      start: bounds.start,
      end: bounds.end,
      label: `All dates: ${formatDisplayDate(bounds.start)} to ${formatDisplayDate(bounds.end)}`
    };
  }

  if (timeframe.mode === "custom") {
    const bounds = getDateBounds(dailyRows);
    const firstDate = timeframe.customStart || bounds.start;
    const secondDate = timeframe.customEnd || bounds.end;
    const start = firstDate <= secondDate ? firstDate : secondDate;
    const end = firstDate <= secondDate ? secondDate : firstDate;

    return {
      key: "custom",
      start,
      end,
      label: `Custom: ${formatDisplayDate(start)} to ${formatDisplayDate(end)}`
    };
  }

  const options = periodOptions[timeframe.mode] || [];
  const selectedOption =
    options.find((option) => option.key === timeframe.periods[timeframe.mode]) || options[0];

  if (!selectedOption) return null;

  return {
    ...selectedOption,
    label: `${MODE_LABELS[timeframe.mode]}: ${selectedOption.label}`
  };
}

function filterCapacityData(result, range) {
  if (!result) return null;
  if (!range) return result;

  const daily = result.daily.filter((row) => isDateInRange(row.run_date, range));
  const details = result.details.filter((row) => isDateInRange(row.run_date, range));
  const towerDetails = (result.tower_details || []).filter((row) => isDateInRange(row.run_date, range));
  const complexityTiming = (result.complexity_timing || []).filter((row) => isDateInRange(row.run_date, range));

  return {
    ...result,
    summary: calculateSummary(daily),
    daily,
    details,
    tower_details: towerDetails,
    complexity_timing: complexityTiming
  };
}

function calculateSummary(dailyRows) {
  const totalAvailable = sumBy(dailyRows, "available_capacity");
  const totalRuntime = sumBy(dailyRows, "runtime");
  const rawPercentage = totalAvailable > 0 ? (totalRuntime / totalAvailable) * 100 : 0;
  const cappedPercentage = Math.min(rawPercentage, 100);

  return {
    total_available_capacity: cleanNumber(totalAvailable),
    total_runtime: cleanNumber(totalRuntime),
    total_lost_time: cleanNumber(sumBy(dailyRows, "lost_time")),
    total_downtime: cleanNumber(sumBy(dailyRows, "downtime")),
    total_buffer_time: cleanNumber(sumBy(dailyRows, "buffer_time")),
    average_utilization_percentage: cleanNumber(cappedPercentage),
    active_folder_days: cleanNumber(sumBy(dailyRows, "active_folders_count"))
  };
}

function getDateBounds(dailyRows) {
  const dates = dailyRows
    .map((row) => row.run_date)
    .filter(Boolean)
    .sort();

  return {
    start: dates[0] || "",
    end: dates[dates.length - 1] || ""
  };
}

function isDateInRange(value, range) {
  return Boolean(value && value >= range.start && value <= range.end);
}

function sumBy(rows, key) {
  return rows.reduce((total, row) => total + Number(row[key] || 0), 0);
}

function cleanNumber(value) {
  const numeric = Number.isFinite(Number(value)) ? Number(value) : 0;
  const rounded = Math.round(numeric * 100) / 100;
  return Number.isInteger(rounded) ? rounded : Number(rounded.toFixed(2));
}

function parseDateKey(value) {
  const match = /^(\d{4})-(\d{2})-(\d{2})$/.exec(String(value || ""));
  if (!match) return null;

  return {
    year: Number(match[1]),
    month: Number(match[2]),
    day: Number(match[3])
  };
}

function formatDateKey(year, month, day) {
  return `${year}-${pad(month)}-${pad(day)}`;
}

function pad(value) {
  return String(value).padStart(2, "0");
}

function daysInMonth(year, month) {
  return new Date(year, month, 0).getDate();
}

function formatMonthLabel(year, month) {
  return new Intl.DateTimeFormat("en-US", {
    month: "short",
    year: "numeric"
  }).format(new Date(year, month - 1, 1));
}

function formatDisplayDate(value) {
  const dateParts = parseDateKey(value);
  if (!dateParts) return value;

  return new Intl.DateTimeFormat("en-US", {
    month: "short",
    day: "numeric",
    year: "numeric"
  }).format(new Date(dateParts.year, dateParts.month - 1, dateParts.day));
}
