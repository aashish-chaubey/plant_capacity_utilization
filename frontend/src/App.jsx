import { AlertCircle, BarChart2, Check, ChevronDown, FileSpreadsheet, Info, Loader2, RotateCcw, UploadCloud, X } from "lucide-react";
import { useEffect, useMemo, useRef, useState } from "react";

import Dashboard from "./components/Dashboard.jsx";

const API_BASE_URL = normalizeApiBaseUrl(import.meta.env.VITE_API_BASE_URL);
const FISCAL_YEAR_START_MONTH = 4;
const PERIOD_MODES = ["annual", "half", "quarter", "month"];
const TIMEFRAME_TABS = [
  ["annual", "Fiscal yr"],
  ["half", "Half-yr"],
  ["quarter", "Qtr"],
  ["month", "Month"],
  ["custom", "Custom"]
];
const METRIC_DEFINITIONS = [
  {
    term: "Wait Time",
    definition: "Idle time at the start of the 00:00 window where the press cannot operate because editorial LPR has not been issued. Wait ends when LPR is issued. If an earlier edition finishes before LPR for the next edition, the PF-to-LPR gap also counts as Wait."
  },
  {
    term: "Loss Time",
    definition: "Preparation time after editorial release and before printing. Components are Makeready from LPR to Press Start, Changeover from Print Finish to Press Start when a physical change is required, and Reflong changeover losses."
  },
  {
    term: "Downtime",
    definition: "Unplanned stoppages that occur during an active run."
  },
  {
    term: "Run Time",
    definition: "Net productive print time when the press is actively printing. For editions already printing before midnight, only the portion from midnight to Print Finish is counted."
  },
  {
    term: "Spare Time",
    definition: "Unused capacity remaining within the 00:00-04:00 reference window after all other components are accounted for. Formula: Spare Time = 240 - (Wait + Loss + Downtime + Run). Spare Time cannot be negative."
  },
  {
    term: "Unplanned Time",
    definition: "Periods where the folder or tower was not scheduled or available for production."
  },
  {
    term: "Spare Capacity",
    definition: "The efficiency ratio of spare time relative to the window that was actually available. Formula: Spare Capacity = (Spare Time / (Total Available Time - Unplanned Time)) * 100."
  },
  {
    term: "Utilisation",
    definition: "Loss Time + Downtime + Run Time. Wait Time, Spare Time, and Unplanned Time are not included."
  },
  {
    term: "GNP/UV Night",
    definition: "Any night where at least one folder runs a GNP or GNP Complex edition, meaning C5-C15. If no GNP or GNP Complex edition runs, the night is SNP/non-UV."
  },
  {
    term: "MALT",
    definition: "Maximum Allowable Loss Time. Formula: MALT = 240 - P50(Wait) - P85(MOT) - P30(Spare), where MOT = Run Time + Downtime. It is calibrated per plant and complexity using on-time nights only."
  }
];

function normalizeApiBaseUrl(value) {
  return String(value || "").replace(/\/+$/, "");
}

async function readApiError(response, fallbackMessage) {
  try {
    const payload = await response.clone().json();
    const details = Array.isArray(payload?.errors)
      ? payload.errors.filter(Boolean).join(" ")
      : payload?.detail || payload?.message;
    if (details) return `${fallbackMessage}: ${details}`;
  } catch {
    // Fall through to text parsing.
  }

  try {
    const text = (await response.text()).trim();
    if (text) return `${fallbackMessage}: ${text.slice(0, 300)}`;
  } catch {
    // Fall through to the default message.
  }

  return fallbackMessage;
}

export default function App() {
  const fileInputRef = useRef(null);
  const folderMenuRef = useRef(null);
  const [result, setResult] = useState(null);
  const [errors, setErrors] = useState([]);
  const [loading, setLoading] = useState(false);
  const [intelligence, setIntelligence] = useState(null);
  const [intelligenceLoading, setIntelligenceLoading] = useState(false);
  const [intelligenceError, setIntelligenceError] = useState("");
  const [fileName, setFileName] = useState("");
  const [timeframe, setTimeframe] = useState(createDefaultTimeframe);
  const [selectedPlant, setSelectedPlant] = useState("");
  const [selectedFolders, setSelectedFolders] = useState([]);
  const [folderMenuOpen, setFolderMenuOpen] = useState(false);
  const [definitionsOpen, setDefinitionsOpen] = useState(false);

  const plantOptions = useMemo(() => buildPlantOptions(result), [result]);
  const folderOptions = useMemo(
    () => buildFolderOptions(result, selectedPlant),
    [result, selectedPlant]
  );
  const scopedResult = useMemo(
    () => filterCapacityDataByScope(result, selectedPlant, selectedFolders),
    [result, selectedFolders, selectedPlant]
  );
  const periodOptions = useMemo(
    () => buildPeriodOptions(scopedResult?.daily || []),
    [scopedResult]
  );
  const periodSelectOptions = useMemo(
    () =>
      getVisiblePeriodOptions(
        periodOptions[timeframe.mode] || [],
        timeframe.mode,
        timeframe.periods[timeframe.mode] || ""
      ),
    [periodOptions, timeframe.mode, timeframe.periods]
  );

  useEffect(() => {
    if (!result) {
      setSelectedPlant("");
      setSelectedFolders([]);
      return;
    }
    setSelectedPlant((current) => {
      if (current && plantOptions.some((option) => option.value === current)) return current;
      return plantOptions.length === 1 ? plantOptions[0].value : "";
    });
  }, [plantOptions, result]);

  useEffect(() => {
    setSelectedFolders((current) => {
      const validFolders = new Set(folderOptions.map((option) => option.value));
      return current.filter((folder) => validFolders.has(folder));
    });
  }, [folderOptions]);

  useEffect(() => {
    if (!folderMenuOpen) return undefined;

    function handlePointerDown(event) {
      if (folderMenuRef.current?.contains(event.target)) return;
      setFolderMenuOpen(false);
    }

    function handleKeyDown(event) {
      if (event.key === "Escape") setFolderMenuOpen(false);
    }

    document.addEventListener("pointerdown", handlePointerDown);
    document.addEventListener("keydown", handleKeyDown);
    return () => {
      document.removeEventListener("pointerdown", handlePointerDown);
      document.removeEventListener("keydown", handleKeyDown);
    };
  }, [folderMenuOpen]);

  useEffect(() => {
    if (!scopedResult?.daily?.length) return;
    setTimeframe((current) => {
      const nextPeriods = { ...current.periods };
      let changed = false;
      for (const mode of PERIOD_MODES) {
        const options = periodOptions[mode] || [];
        const nextKey = nextPeriods[mode] || options[0]?.key || "";
        if (nextPeriods[mode] !== nextKey) {
          nextPeriods[mode] = nextKey;
          changed = true;
        }
      }
      const bounds = getDateBounds(scopedResult.daily);
      const customStart = current.customStart || bounds.start;
      const customEnd = current.customEnd || bounds.end;
      if (customStart !== current.customStart || customEnd !== current.customEnd) changed = true;
      if (!changed) return current;
      return { ...current, periods: nextPeriods, customStart, customEnd };
    });
  }, [periodOptions, scopedResult]);

  const timeframeRange = useMemo(
    () => resolveTimeframeRange(timeframe, periodOptions, scopedResult?.daily || []),
    [periodOptions, scopedResult, timeframe]
  );

  const filteredResult = useMemo(
    () => filterCapacityData(scopedResult, timeframeRange),
    [scopedResult, timeframeRange]
  );

  useEffect(() => {
    if (!filteredResult?.daily?.length) {
      setIntelligence(null);
      setIntelligenceLoading(false);
      setIntelligenceError("");
      return;
    }
    const controller = new AbortController();
    setIntelligenceLoading(true);
    setIntelligenceError("");

    async function loadIntelligence() {
      try {
        const response = await fetch(`${API_BASE_URL}/api/intelligence`, {
          method: "POST",
          headers: { "Content-Type": "application/json" },
          body: JSON.stringify({
            summary: filteredResult.summary,
            daily: filteredResult.daily,
            details: filteredResult.details,
            tower_details: filteredResult.tower_details || [],
            scope_label: timeframeRange?.label || "Selected timeframe"
          }),
          signal: controller.signal
        });
        if (!response.ok) {
          throw new Error(await readApiError(response, `Intelligence failed with status ${response.status}`));
        }
        const payload = await response.json();
        if (!payload.valid) throw new Error(payload.errors?.[0] || "Intelligence generation failed.");
        setIntelligence(payload.intelligence);
      } catch (error) {
        if (error.name === "AbortError") return;
        setIntelligence(null);
        setIntelligenceError(error.message || "Unable to load capacity intelligence.");
      } finally {
        if (!controller.signal.aborted) setIntelligenceLoading(false);
      }
    }

    loadIntelligence();
    return () => controller.abort();
  }, [filteredResult, timeframeRange?.label]);

  async function handleUpload(file) {
    if (!file) return;
    setLoading(true);
    setErrors([]);
    setIntelligence(null);
    setIntelligenceError("");
    setIntelligenceLoading(false);
    setFileName(file.name);
    setTimeframe(createDefaultTimeframe());
    setSelectedPlant("");
    setSelectedFolders([]);

    const formData = new FormData();
    formData.append("file", file);

    try {
      const response = await fetch(`${API_BASE_URL}/api/upload`, {
        method: "POST",
        body: formData
      });
      if (!response.ok) {
        throw new Error(await readApiError(response, `Upload failed with status ${response.status}`));
      }
      const payload = await response.json();
      if (!payload.valid) {
        setResult(null);
        setIntelligence(null);
        setErrors(payload.errors || ["The workbook could not be processed."]);
        return;
      }
      setResult(payload);
    } catch (error) {
      setResult(null);
      setIntelligence(null);
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
      periods: { ...current.periods, [current.mode]: periodKey }
    }));
  }

  function handleCustomRangeChange(field, value) {
    setTimeframe((current) => ({ ...current, isCleared: false, [field]: value }));
  }

  function handleClearTimeframe() {
    setTimeframe((current) => ({ ...current, isCleared: true }));
  }

  function handlePlantChange(plantName) {
    setSelectedPlant(plantName);
    setSelectedFolders([]);
    setFolderMenuOpen(false);
  }

  function handleFolderToggle(folderName) {
    setSelectedFolders((current) =>
      current.includes(folderName)
        ? current.filter((folder) => folder !== folderName)
        : [...current, folderName]
    );
  }

  function handleClearFolders() {
    setSelectedFolders([]);
    setFolderMenuOpen(false);
  }

  const showControlStrip = Boolean(result && selectedPlant);
  const folderSelectionLabel = formatFolderSelectionLabel(selectedFolders);

  return (
    <div className="min-h-screen bg-[#f6f8fb] text-slate-900">
      {/* ── Header ──────────────────────────────────────────────── */}
      <header className="sticky top-0 z-30 border-b border-slate-200 bg-white shadow-[0_1px_4px_rgba(0,0,0,0.06)]">
        <div className="mx-auto flex h-14 w-full max-w-[1800px] items-center gap-4 px-4 sm:px-5 xl:px-6">
          {/* Brand */}
          <div className="flex min-w-0 flex-1 items-center gap-3">
            <div className="flex h-8 w-8 shrink-0 items-center justify-center rounded-lg bg-blue-600 shadow-sm">
              <BarChart2 className="h-[18px] w-[18px] text-white" aria-hidden="true" />
            </div>
            <div className="min-w-0">
              <h1 className="truncate text-sm font-bold leading-snug text-slate-950">
                Plant Capacity Utilization
              </h1>
              <p className="hidden text-[11px] leading-snug text-slate-400 sm:block">
                00:00 – 04:00 production window
              </p>
            </div>
          </div>

          {/* Right: filename badge + upload button */}
          <div className="flex shrink-0 items-center gap-2.5">
            {fileName && !loading && (
              <span className="hidden max-w-[180px] items-center gap-1.5 rounded-full border border-slate-200 bg-slate-50 px-3 py-1 text-xs font-medium text-slate-500 sm:flex">
                <FileSpreadsheet className="h-3 w-3 shrink-0 text-slate-400" aria-hidden="true" />
                <span className="truncate">{fileName}</span>
              </span>
            )}
            <input
              ref={fileInputRef}
              type="file"
              accept=".xlsx,.xls"
              className="hidden"
              onChange={(event) => handleUpload(event.target.files?.[0])}
            />
            <button
              type="button"
              onClick={() => fileInputRef.current?.click()}
              disabled={loading}
              className="inline-flex h-9 items-center gap-1.5 rounded-lg bg-blue-600 px-4 text-sm font-semibold text-white shadow-sm transition hover:bg-blue-700 active:bg-blue-800 disabled:cursor-not-allowed disabled:bg-slate-300"
            >
              {loading ? (
                <Loader2 className="h-4 w-4 animate-spin" aria-hidden="true" />
              ) : (
                <UploadCloud className="h-4 w-4" aria-hidden="true" />
              )}
              <span>{loading ? "Processing…" : result ? "Replace report" : "Upload report"}</span>
            </button>
          </div>
        </div>
      </header>

      {/* ── Control strip (sticky below header) ─────────────────── */}
      {showControlStrip && (
        <div className="sticky top-14 z-20 border-b border-slate-200 bg-white/95 shadow-[0_1px_3px_rgba(0,0,0,0.04)] backdrop-blur-sm">
          <div className="mx-auto w-full max-w-[1800px] px-4 py-2.5 sm:px-5 xl:px-6">
            <div className="flex flex-wrap items-start gap-2">

              {/* ── Plant ── */}
              {plantOptions.length > 1 && (
                <div className="flex shrink-0 items-center gap-2 rounded-xl border border-slate-200 bg-slate-50 px-3 py-2">
                  <span className="text-[11px] font-bold uppercase tracking-wider text-slate-400">Plant</span>
                  <select
                    value={selectedPlant}
                    onChange={(event) => handlePlantChange(event.target.value)}
                    className="h-8 rounded-lg border border-slate-200 bg-white px-2 text-sm font-semibold text-slate-800 outline-none transition focus:border-blue-400 focus:ring-1 focus:ring-blue-200"
                  >
                    <option value="">Select plant</option>
                    {plantOptions.map((option) => (
                      <option key={option.value} value={option.value}>{option.label}</option>
                    ))}
                  </select>
                </div>
              )}

              {/* ── Folders ── */}
              <div
                ref={folderMenuRef}
                className="relative flex min-w-[220px] shrink-0 items-center gap-2.5 rounded-xl border border-slate-200 bg-slate-50 px-3 py-2"
              >
                <span className="shrink-0 pt-1 text-[11px] font-bold uppercase tracking-wider text-slate-400">
                  Folders
                </span>
                <button
                  type="button"
                  onClick={() => setFolderMenuOpen((open) => !open)}
                  className="flex h-8 w-[180px] items-center justify-between gap-2 rounded-lg border border-slate-200 bg-white px-2.5 text-left text-sm font-semibold text-slate-800 outline-none transition hover:border-blue-200 hover:bg-blue-50 focus:border-blue-400 focus:ring-1 focus:ring-blue-200"
                  aria-haspopup="listbox"
                  aria-expanded={folderMenuOpen}
                >
                  <span className="min-w-0 truncate">{folderSelectionLabel}</span>
                  <ChevronDown
                    className={`h-3.5 w-3.5 shrink-0 text-slate-400 transition ${folderMenuOpen ? "rotate-180" : ""}`}
                    aria-hidden="true"
                  />
                </button>
                {selectedFolders.length > 0 && (
                  <button
                    type="button"
                    onClick={handleClearFolders}
                    className="flex h-7 w-7 shrink-0 items-center justify-center rounded-lg text-slate-400 transition hover:bg-red-50 hover:text-red-500"
                    title="Clear folder filter"
                  >
                    <X className="h-3.5 w-3.5" aria-hidden="true" />
                  </button>
                )}
                {folderMenuOpen && (
                  <div className="absolute left-0 top-[calc(100%+6px)] z-40 w-[300px] overflow-hidden rounded-xl border border-slate-200 bg-white shadow-lg">
                    <div className="flex items-center justify-between border-b border-slate-100 px-3 py-2">
                      <span className="text-xs font-semibold text-slate-500">
                        {selectedFolders.length === 0
                          ? "All folders selected"
                          : `${selectedFolders.length} of ${folderOptions.length} selected`}
                      </span>
                      {selectedFolders.length > 0 && (
                        <button
                          type="button"
                          onClick={handleClearFolders}
                          className="text-xs font-semibold text-blue-600 transition hover:text-blue-700"
                        >
                          Select all
                        </button>
                      )}
                    </div>
                    <div className="max-h-64 overflow-y-auto py-1" role="listbox" aria-multiselectable="true">
                      {folderOptions.map((option) => {
                        const active = selectedFolders.includes(option.value);
                        return (
                          <button
                            key={option.value}
                            type="button"
                            onClick={() => handleFolderToggle(option.value)}
                            className={`flex w-full items-center gap-2 px-3 py-2 text-left text-sm transition ${
                              active
                                ? "bg-blue-50 text-blue-700"
                                : "text-slate-700 hover:bg-slate-50"
                            }`}
                            role="option"
                            aria-selected={active}
                          >
                            <span className={`flex h-4 w-4 shrink-0 items-center justify-center rounded border ${
                              active ? "border-blue-600 bg-blue-600" : "border-slate-300 bg-white"
                            }`}>
                              {active && <Check className="h-3 w-3 text-white" aria-hidden="true" />}
                            </span>
                            <span className="min-w-0 truncate">{option.label}</span>
                          </button>
                        );
                      })}
                    </div>
                  </div>
                )}
              </div>

              {/* ── Timeframe ── */}
              <div className="flex shrink-0 flex-wrap items-center gap-2 rounded-xl border border-slate-200 bg-slate-50 px-3 py-2">
                <span className="text-[11px] font-bold uppercase tracking-wider text-slate-400">Period</span>

                {/* Mode tabs */}
                <div className="flex items-center gap-0.5 rounded-lg border border-slate-200 bg-white p-0.5">
                  {TIMEFRAME_TABS.map(([value, label]) => (
                    <button
                      key={value}
                      type="button"
                      onClick={() => handleModeChange(value)}
                      className={`h-8 rounded-md px-3 text-xs font-semibold whitespace-nowrap transition ${
                        !timeframe.isCleared && timeframe.mode === value
                          ? "bg-blue-600 text-white shadow-sm"
                          : "text-slate-500 hover:bg-slate-100 hover:text-slate-800"
                      }`}
                    >
                      {label}
                    </button>
                  ))}
                </div>

                {/* Period selector or date range */}
                {timeframe.mode === "custom" ? (
                  <div className="flex items-center gap-1.5">
                    <input
                      type="date"
                      value={timeframe.customStart}
                      onChange={(event) => handleCustomRangeChange("customStart", event.target.value)}
                      className="h-8 rounded-lg border border-slate-200 bg-white px-2 text-sm text-slate-800 outline-none focus:border-blue-400 focus:ring-1 focus:ring-blue-200"
                    />
                    <span className="text-sm font-medium text-slate-400">–</span>
                    <input
                      type="date"
                      value={timeframe.customEnd}
                      onChange={(event) => handleCustomRangeChange("customEnd", event.target.value)}
                      className="h-8 rounded-lg border border-slate-200 bg-white px-2 text-sm text-slate-800 outline-none focus:border-blue-400 focus:ring-1 focus:ring-blue-200"
                    />
                  </div>
                ) : (
                  <select
                    value={timeframe.periods[timeframe.mode] || ""}
                    onChange={(event) => handlePeriodChange(event.target.value)}
                    className="h-8 min-w-[130px] rounded-lg border border-slate-200 bg-white px-2 text-sm font-medium text-slate-800 outline-none transition focus:border-blue-400 focus:ring-1 focus:ring-blue-200"
                  >
                    {periodSelectOptions.length === 0 && (
                      <option value="">No dates</option>
                    )}
                    {periodSelectOptions.map((option) => (
                      <option key={option.key} value={option.key}>{option.label}</option>
                    ))}
                  </select>
                )}

                {/* Range label */}
                {timeframeRange?.label && (
                  <span className="hidden max-w-[200px] truncate text-xs text-slate-400 xl:block">
                    {timeframeRange.label}
                  </span>
                )}

                {/* Reset */}
                <button
                  type="button"
                  onClick={handleClearTimeframe}
                  disabled={timeframe.isCleared}
                  title="Show all dates"
                  className="flex h-8 w-8 items-center justify-center rounded-lg border border-slate-200 bg-white text-slate-400 transition hover:border-blue-200 hover:bg-blue-50 hover:text-blue-600 disabled:cursor-not-allowed disabled:opacity-40"
                >
                  <RotateCcw className="h-3.5 w-3.5" aria-hidden="true" />
                </button>
              </div>

              <button
                type="button"
                onClick={() => setDefinitionsOpen(true)}
                className="flex h-12 shrink-0 items-center gap-2 rounded-xl border border-slate-200 bg-white px-3 text-sm font-semibold text-slate-700 shadow-sm transition hover:border-blue-200 hover:bg-blue-50 hover:text-blue-700 focus:outline-none focus:ring-2 focus:ring-blue-100"
                title="View metric definitions"
              >
                <Info className="h-4 w-4" aria-hidden="true" />
                <span>Definitions</span>
              </button>

            </div>
          </div>
        </div>
      )}

      {definitionsOpen && (
        <MetricDefinitionsModal onClose={() => setDefinitionsOpen(false)} />
      )}

      {/* ── Main ────────────────────────────────────────────────── */}
      <main className="mx-auto w-full max-w-[1800px] px-4 py-6 sm:px-5 xl:px-6">

        {/* Empty state */}
        {!result && !loading && errors.length === 0 && (
          <EmptyDropZone onUpload={handleUpload} />
        )}

        {/* Error banner */}
        {errors.length > 0 && (
          <section className="rounded-xl border border-red-200 bg-red-50 p-4 text-red-900">
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

        {/* Multi-plant selection required */}
        {result && plantOptions.length > 1 && !selectedPlant && errors.length === 0 && (
          <section className="rounded-xl border border-amber-200 bg-amber-50 p-6 text-amber-900">
            <h2 className="text-base font-semibold">Select a plant to continue</h2>
            <p className="mt-1 text-sm">
              This report contains multiple plants. Choose one using the{" "}
              <span className="font-semibold">Plant</span> selector in the control bar above.
            </p>
            <div className="mt-4 flex flex-wrap gap-2">
              {plantOptions.map((option) => (
                <button
                  key={option.value}
                  type="button"
                  onClick={() => handlePlantChange(option.value)}
                  className="rounded-lg border border-amber-300 bg-white px-4 py-2 text-sm font-semibold text-amber-800 shadow-sm transition hover:bg-amber-100"
                >
                  {option.label}
                </button>
              ))}
            </div>
          </section>
        )}

        {/* Dashboard */}
        {selectedPlant && filteredResult?.daily?.length > 0 && (
          <Dashboard
            data={filteredResult}
            intelligence={intelligence}
            intelligenceLoading={intelligenceLoading}
            intelligenceError={intelligenceError}
          />
        )}

        {/* Empty timeframe */}
        {selectedPlant && filteredResult && filteredResult.daily.length === 0 && errors.length === 0 && (
          <section className="mt-2 rounded-xl border border-slate-200 bg-white p-8 text-center shadow-soft">
            <p className="text-sm font-semibold text-slate-950">No rows in this timeframe</p>
            <p className="mt-1 text-sm text-slate-500">
              The selected range contains no production dates from the uploaded report.
            </p>
          </section>
        )}
      </main>
    </div>
  );
}

function MetricDefinitionsModal({ onClose }) {
  return (
    <div className="fixed inset-0 z-50 flex items-start justify-center bg-slate-950/35 px-4 py-8 backdrop-blur-sm sm:items-center">
      <section className="flex max-h-[86vh] w-full max-w-4xl flex-col overflow-hidden rounded-xl border border-slate-200 bg-white shadow-2xl">
        <div className="flex items-start justify-between gap-4 border-b border-slate-100 px-5 py-4">
          <div>
            <h2 className="text-base font-semibold text-slate-950">Metric definitions</h2>
            <p className="mt-1 text-sm text-slate-500">
              Definitions used across the dashboard and AI chat.
            </p>
          </div>
          <button
            type="button"
            onClick={onClose}
            className="flex h-8 w-8 shrink-0 items-center justify-center rounded-lg text-slate-400 transition hover:bg-slate-100 hover:text-slate-700"
            aria-label="Close metric definitions"
          >
            <X className="h-4 w-4" aria-hidden="true" />
          </button>
        </div>

        <div className="overflow-y-auto px-5 py-4">
          <div className="grid gap-3 md:grid-cols-2">
            {METRIC_DEFINITIONS.map((item) => (
              <article key={item.term} className="rounded-lg border border-slate-100 bg-slate-50 px-3 py-2.5">
                <h3 className="text-sm font-bold text-slate-900">{item.term}</h3>
                <p className="mt-1 text-sm leading-5 text-slate-600">{item.definition}</p>
              </article>
            ))}
          </div>
        </div>
      </section>
    </div>
  );
}

// ── Empty drop zone ────────────────────────────────────────────────
function EmptyDropZone({ onUpload }) {
  const inputRef = useRef(null);
  const [dragging, setDragging] = useState(false);

  function submitFile(file) {
    if (file) onUpload(file);
  }

  return (
    <div
      onDragOver={(event) => { event.preventDefault(); setDragging(true); }}
      onDragLeave={() => setDragging(false)}
      onDrop={(event) => { event.preventDefault(); setDragging(false); submitFile(event.dataTransfer.files?.[0]); }}
      onClick={() => inputRef.current?.click()}
      className={`flex cursor-pointer flex-col items-center justify-center rounded-2xl border-2 border-dashed transition ${
        dragging
          ? "border-blue-400 bg-blue-50"
          : "border-slate-200 bg-white hover:border-blue-200 hover:bg-slate-50/60"
      }`}
      style={{ minHeight: "calc(100vh - 120px)" }}
    >
      <input
        ref={inputRef}
        type="file"
        accept=".xlsx,.xls"
        className="hidden"
        onChange={(event) => submitFile(event.target.files?.[0])}
      />
      <div className="flex flex-col items-center gap-5 text-center px-6">
        <div className={`rounded-2xl p-5 transition ${dragging ? "bg-blue-100" : "bg-slate-100"}`}>
          <UploadCloud className={`h-10 w-10 transition ${dragging ? "text-blue-500" : "text-slate-400"}`} aria-hidden="true" />
        </div>
        <div className="space-y-1.5">
          <p className="text-base font-semibold text-slate-700">
            {dragging ? "Release to upload" : "Drop your production report here"}
          </p>
          <p className="max-w-xs text-sm text-slate-400">
            Or use the{" "}
            <span className="font-semibold text-blue-600">Upload report</span>{" "}
            button in the top-right corner
          </p>
        </div>
        <p className="text-xs text-slate-300">Accepts .xlsx and .xls files</p>
      </div>
    </div>
  );
}

// ── Helpers ─────────────────────────────────────────────────────────

function createDefaultTimeframe() {
  return {
    mode: "annual",
    isCleared: false,
    periods: { annual: "", half: "", quarter: "", month: "" },
    customStart: "",
    customEnd: ""
  };
}

function buildPlantOptions(result) {
  if (!result) return [];
  return Array.from(
    new Set((result.details || []).map((row) => row.plant_name).filter(Boolean))
  )
    .sort((a, b) => a.localeCompare(b))
    .map((plantName) => ({ value: plantName, label: plantName }));
}

function buildFolderOptions(result, selectedPlant) {
  if (!result || !selectedPlant) return [];
  return Array.from(
    new Set(
      (result.details || [])
        .filter((row) => row.plant_name === selectedPlant)
        .map((row) => row.folder)
        .filter(Boolean)
    )
  )
    .sort((a, b) => a.localeCompare(b))
    .map((folderName) => ({ value: folderName, label: formatResourceLabel(folderName) }));
}

function filterCapacityDataByScope(result, selectedPlant, selectedFolders) {
  if (!result || !selectedPlant) return null;
  const selectedFolderSet = new Set(selectedFolders);
  const plantDetails = (result.details || []).filter((row) => row.plant_name === selectedPlant);
  const dateUniverse = Array.from(new Set(plantDetails.map((row) => row.run_date).filter(Boolean))).sort();
  const details = selectedFolderSet.size > 0
    ? plantDetails.filter((row) => selectedFolderSet.has(row.folder))
    : plantDetails;
  const towerDetails = (result.tower_details || [])
    .filter((row) => row.plant_name === selectedPlant)
    .filter((row) => selectedFolderSet.size === 0 || selectedFolderSet.has(row.folder));
  const fixedCapacityFolders = selectedFolderSet.size > 0 ? selectedFolderSet.size : null;
  const daily = buildDailyRowsFromDetails(details, dateUniverse, fixedCapacityFolders);
  return { ...result, summary: calculateSummary(daily), daily, details, tower_details: towerDetails };
}

function buildDailyRowsFromDetails(detailRows, dateUniverse, fixedCapacityFolders = null) {
  const dates = (dateUniverse?.length ? dateUniverse : Array.from(
    new Set((detailRows || []).map((row) => row.run_date).filter(Boolean))
  )).sort();

  if (!dates.length) return [];

  const detailsByDate = new Map();
  for (const row of detailRows || []) {
    if (!row.run_date) continue;
    const rows = detailsByDate.get(row.run_date) || [];
    rows.push(row);
    detailsByDate.set(row.run_date, rows);
  }

  const maxCapacityFolders = Math.max(
    0,
    ...dates.map((runDate) => new Set((detailsByDate.get(runDate) || []).map((row) => row.folder)).size)
  );
  const capacityFoldersCount = Math.max(Number(fixedCapacityFolders || 0), maxCapacityFolders);
  if (capacityFoldersCount <= 0) return [];

  return dates.map((runDate) => {
    const rows = detailsByDate.get(runDate) || [];
    const activeFoldersCount = new Set(
      rows.filter(isActiveCapacityDetailRow).map((row) => row.folder)
    ).size;
    const activeAvailableCapacity = sumBy(rows, "available_capacity");
    const availableCapacity = capacityFoldersCount * 240;
    const runtime = sumBy(rows, "runtime");
    const lostTime = sumBy(rows, "lost_time");
    const downtime = sumBy(rows, "downtime");
    const utilizedTime = runtime + downtime + lostTime;
    const bufferTime = sumBy(rows, "buffer_time");
    const activeIdleTime = sumBy(rows, "idle_time");
    const idleTime = Math.max(availableCapacity - activeAvailableCapacity, 0) + activeIdleTime;
    return {
      run_date: runDate,
      active_folders_count: activeFoldersCount,
      capacity_folders_count: capacityFoldersCount,
      available_capacity: cleanNumber(availableCapacity),
      runtime: cleanNumber(runtime),
      lost_time: cleanNumber(lostTime),
      downtime: cleanNumber(downtime),
      buffer_time: cleanNumber(bufferTime),
      idle_time: cleanNumber(idleTime),
      utilization_percentage: cleanNumber(
        availableCapacity > 0 ? Math.min((utilizedTime / availableCapacity) * 100, 100) : 0
      )
    };
  });
}

function buildPeriodOptions(dailyRows) {
  const periodMaps = { annual: new Map(), half: new Map(), quarter: new Map(), month: new Map() };

  for (const row of dailyRows) {
    const dateParts = parseDateKey(row.run_date);
    if (!dateParts) continue;
    const { year, month } = dateParts;
    const fiscalPeriod = getFiscalPeriodParts(year, month);
    const fiscalYearLabel = formatFiscalYearLabel(fiscalPeriod.fiscalYearStart);
    const annualRange = getFiscalAnnualRange(fiscalPeriod.fiscalYearStart);
    const halfRange = getFiscalSubPeriodRange(fiscalPeriod.fiscalYearStart, fiscalPeriod.half === 1 ? 1 : 7, fiscalPeriod.half === 1 ? 6 : 12);
    const quarterStartFiscalMonth = (fiscalPeriod.quarter - 1) * 3 + 1;
    const quarterRange = getFiscalSubPeriodRange(
      fiscalPeriod.fiscalYearStart,
      quarterStartFiscalMonth,
      quarterStartFiscalMonth + 2
    );

    addPeriod(periodMaps.annual, {
      key: String(fiscalPeriod.fiscalYearStart),
      label: fiscalYearLabel,
      ...annualRange
    });
    addPeriod(periodMaps.half, {
      key: `${fiscalPeriod.fiscalYearStart}-H${fiscalPeriod.half}`,
      label: `H${fiscalPeriod.half} ${fiscalYearLabel}`,
      ...halfRange
    });
    addPeriod(periodMaps.quarter, {
      key: `${fiscalPeriod.fiscalYearStart}-Q${fiscalPeriod.quarter}`,
      label: `Q${fiscalPeriod.quarter} ${fiscalYearLabel}`,
      ...quarterRange
    });
    addPeriod(periodMaps.month, {
      key: `${year}-${pad(month)}`,
      label: `${formatMonthLabel(year, month)} (${fiscalYearLabel})`,
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
  if (!periodMap.has(option.key)) periodMap.set(option.key, option);
}

function sortPeriodOptions(periodMap) {
  return Array.from(periodMap.values()).sort((a, b) => b.start.localeCompare(a.start));
}

function getVisiblePeriodOptions(options, mode, selectedKey) {
  if (!selectedKey || options.some((option) => option.key === selectedKey)) return options;

  const selectedOption = buildPeriodOptionFromKey(mode, selectedKey);
  if (!selectedOption) return options;

  const periodMap = new Map(options.map((option) => [option.key, option]));
  periodMap.set(selectedOption.key, selectedOption);
  return sortPeriodOptions(periodMap);
}

function resolveTimeframeRange(timeframe, periodOptions, dailyRows) {
  if (!dailyRows.length) return null;

  if (timeframe.isCleared) {
    const bounds = getDateBounds(dailyRows);
    return {
      key: "all", start: bounds.start, end: bounds.end,
      label: formatDateRangeLabel(bounds.start, bounds.end)
    };
  }

  if (timeframe.mode === "custom") {
    const bounds = getDateBounds(dailyRows);
    const firstDate = timeframe.customStart || bounds.start;
    const secondDate = timeframe.customEnd || bounds.end;
    const start = firstDate <= secondDate ? firstDate : secondDate;
    const end = firstDate <= secondDate ? secondDate : firstDate;
    return { key: "custom", start, end, label: formatDateRangeLabel(start, end) };
  }

  const options = periodOptions[timeframe.mode] || [];
  const selectedKey = timeframe.periods[timeframe.mode] || "";
  const selectedOption = options.find((option) => option.key === selectedKey)
    || buildPeriodOptionFromKey(timeframe.mode, selectedKey)
    || options[0];
  if (!selectedOption) return null;
  return {
    ...selectedOption,
    label: formatDateRangeLabel(selectedOption.start, selectedOption.end)
  };
}

function formatDateRangeLabel(start, end) {
  return `${formatDisplayDate(start)} to ${formatDisplayDate(end)}`;
}

function filterCapacityData(result, range) {
  if (!result) return null;
  if (!range) return result;
  const daily = result.daily.filter((row) => isDateInRange(row.run_date, range));
  const details = result.details.filter((row) => isDateInRange(row.run_date, range));
  const towerDetails = (result.tower_details || []).filter((row) => isDateInRange(row.run_date, range));
  return { ...result, summary: calculateSummary(daily), daily, details, tower_details: towerDetails };
}

function calculateSummary(dailyRows) {
  const totalAvailable = sumBy(dailyRows, "available_capacity");
  const totalRuntime = sumBy(dailyRows, "runtime");
  const totalLostTime = sumBy(dailyRows, "lost_time");
  const totalDowntime = sumBy(dailyRows, "downtime");
  const totalBufferTime = sumBy(dailyRows, "buffer_time");
  const totalIdleTime = sumBy(dailyRows, "idle_time");
  const plannedAvailableTime = Math.max(totalAvailable - totalIdleTime, 0);
  const utilizedTime = totalRuntime + totalDowntime + totalLostTime;
  return {
    total_available_capacity: cleanNumber(totalAvailable),
    total_runtime: cleanNumber(totalRuntime),
    total_lost_time: cleanNumber(totalLostTime),
    total_downtime: cleanNumber(totalDowntime),
    total_buffer_time: cleanNumber(totalBufferTime),
    total_idle_time: cleanNumber(totalIdleTime),
    average_utilization_percentage: cleanNumber(totalAvailable > 0 ? Math.min((utilizedTime / totalAvailable) * 100, 100) : 0),
    spare_capacity_percentage: cleanNumber(plannedAvailableTime > 0 ? Math.min((totalBufferTime / plannedAvailableTime) * 100, 100) : 0),
    idle_capacity_percentage: cleanNumber(totalAvailable > 0 ? Math.min((totalIdleTime / totalAvailable) * 100, 100) : 0),
    active_folder_days: cleanNumber(sumBy(dailyRows, "active_folders_count"))
  };
}

function getDateBounds(dailyRows) {
  const dates = dailyRows.map((row) => row.run_date).filter(Boolean).sort();
  return { start: dates[0] || "", end: dates[dates.length - 1] || "" };
}

function isDateInRange(value, range) {
  return Boolean(value && value >= range.start && value <= range.end);
}

function formatResourceLabel(value) {
  return String(value || "").split("\n").map((part) => part.trim()).filter(Boolean).join(" / ");
}

function formatFolderSelectionLabel(selectedFolders) {
  if (selectedFolders.length === 0) return "All folders";
  if (selectedFolders.length === 1) return formatResourceLabel(selectedFolders[0]);
  return `${selectedFolders.length} selected`;
}

function sumBy(rows, key) {
  return rows.reduce((total, row) => total + Number(row[key] || 0), 0);
}

function isActiveCapacityDetailRow(row) {
  const activeMinutes = [
    "runtime",
    "lost_time",
    "downtime",
    "buffer_time",
    "change_over_time",
    "waiting_time",
    "reflong_related_downtime",
    "late_start_time",
    "gross_runtime",
    "scheduled_runtime",
    "overlap_minutes"
  ].reduce((total, key) => total + Number(row[key] || 0), 0);
  const availableCapacity = Number(row.available_capacity || 0);
  const idleTime = Number(row.idle_time || 0);
  return !(availableCapacity > 0 && idleTime >= availableCapacity && activeMinutes <= 0);
}

function cleanNumber(value) {
  const numeric = Number.isFinite(Number(value)) ? Number(value) : 0;
  const rounded = Math.round(numeric * 100) / 100;
  return Number.isInteger(rounded) ? rounded : Number(rounded.toFixed(2));
}

function buildPeriodOptionFromKey(mode, key) {
  if (!key) return null;

  if (mode === "annual") {
    const match = /^(\d{4})$/.exec(key);
    if (!match) return null;
    const fiscalYearStart = Number(match[1]);
    return {
      key,
      label: formatFiscalYearLabel(fiscalYearStart),
      ...getFiscalAnnualRange(fiscalYearStart)
    };
  }

  if (mode === "half") {
    const match = /^(\d{4})-H([12])$/.exec(key);
    if (!match) return null;
    const fiscalYearStart = Number(match[1]);
    const half = Number(match[2]);
    return {
      key,
      label: `H${half} ${formatFiscalYearLabel(fiscalYearStart)}`,
      ...getFiscalSubPeriodRange(fiscalYearStart, half === 1 ? 1 : 7, half === 1 ? 6 : 12)
    };
  }

  if (mode === "quarter") {
    const match = /^(\d{4})-Q([1-4])$/.exec(key);
    if (!match) return null;
    const fiscalYearStart = Number(match[1]);
    const quarter = Number(match[2]);
    const quarterStartFiscalMonth = (quarter - 1) * 3 + 1;
    return {
      key,
      label: `Q${quarter} ${formatFiscalYearLabel(fiscalYearStart)}`,
      ...getFiscalSubPeriodRange(fiscalYearStart, quarterStartFiscalMonth, quarterStartFiscalMonth + 2)
    };
  }

  if (mode === "month") {
    const match = /^(\d{4})-(\d{2})$/.exec(key);
    if (!match) return null;
    const year = Number(match[1]);
    const month = Number(match[2]);
    if (month < 1 || month > 12) return null;
    const fiscalPeriod = getFiscalPeriodParts(year, month);
    return {
      key,
      label: `${formatMonthLabel(year, month)} (${formatFiscalYearLabel(fiscalPeriod.fiscalYearStart)})`,
      start: formatDateKey(year, month, 1),
      end: formatDateKey(year, month, daysInMonth(year, month))
    };
  }

  return null;
}

function getFiscalPeriodParts(year, month) {
  const fiscalYearStart = month >= FISCAL_YEAR_START_MONTH ? year : year - 1;
  const fiscalMonth = ((month - FISCAL_YEAR_START_MONTH + 12) % 12) + 1;
  return {
    fiscalYearStart,
    fiscalMonth,
    half: fiscalMonth <= 6 ? 1 : 2,
    quarter: Math.ceil(fiscalMonth / 3)
  };
}

function getFiscalAnnualRange(fiscalYearStart) {
  return getFiscalSubPeriodRange(fiscalYearStart, 1, 12);
}

function getFiscalSubPeriodRange(fiscalYearStart, startFiscalMonth, endFiscalMonth) {
  const startPeriod = getFiscalCalendarMonth(fiscalYearStart, startFiscalMonth);
  const endPeriod = getFiscalCalendarMonth(fiscalYearStart, endFiscalMonth);
  return {
    start: formatDateKey(startPeriod.year, startPeriod.month, 1),
    end: formatDateKey(endPeriod.year, endPeriod.month, daysInMonth(endPeriod.year, endPeriod.month))
  };
}

function getFiscalCalendarMonth(fiscalYearStart, fiscalMonth) {
  const zeroBasedCalendarMonth = FISCAL_YEAR_START_MONTH - 1 + fiscalMonth - 1;
  return {
    year: fiscalYearStart + Math.floor(zeroBasedCalendarMonth / 12),
    month: (zeroBasedCalendarMonth % 12) + 1
  };
}

function formatFiscalYearLabel(fiscalYearStart) {
  return `FY ${fiscalYearStart}-${String(fiscalYearStart + 1).slice(-2)}`;
}

function parseDateKey(value) {
  const match = /^(\d{4})-(\d{2})-(\d{2})$/.exec(String(value || ""));
  if (!match) return null;
  return { year: Number(match[1]), month: Number(match[2]), day: Number(match[3]) };
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
  return new Intl.DateTimeFormat("en-US", { month: "short", year: "numeric" }).format(new Date(year, month - 1, 1));
}

function formatDisplayDate(value) {
  const dateParts = parseDateKey(value);
  if (!dateParts) return value;
  return new Intl.DateTimeFormat("en-US", { month: "short", day: "numeric", year: "numeric" }).format(
    new Date(dateParts.year, dateParts.month - 1, dateParts.day)
  );
}
