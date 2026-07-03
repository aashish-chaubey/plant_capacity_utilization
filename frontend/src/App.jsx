import {
  Activity,
  AlertCircle,
  BarChart2,
  Check,
  ChevronDown,
  FileSpreadsheet,
  Gauge,
  Info,
  Layers,
  Loader2,
  RotateCcw,
  ShieldCheck,
  UploadCloud,
  X
} from "lucide-react";
import { useEffect, useMemo, useRef, useState } from "react";

import Dashboard from "./components/Dashboard.jsx";

const API_BASE_URL = normalizeApiBaseUrl(import.meta.env.VITE_API_BASE_URL);
const FISCAL_YEAR_START_MONTH = 4;
const CAPACITY_WINDOW_MINUTES = 240;
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
    term: "Utilized Time / Utilisation",
    definition: "Runtime (SNP + GNP) + Loss Time + Downtime. Wait Time, Spare Time, and Unplanned Time are not included."
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

function delay(ms) {
  return new Promise((resolve) => setTimeout(resolve, ms));
}

export default function App() {
  const fileInputRef = useRef(null);
  const folderMenuRef = useRef(null);
  const [result, setResult] = useState(null);
  const [jobId, setJobId] = useState("");
  const [errors, setErrors] = useState([]);
  const [loading, setLoading] = useState(false);
  const [uploadProgress, setUploadProgress] = useState(null);
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
    if (!filteredResult?.daily?.length || filteredResult.processing_complete === false) {
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
    setUploadProgress({ progress: 0, message: "Uploading workbook" });
    setErrors([]);
    setIntelligence(null);
    setIntelligenceError("");
    setIntelligenceLoading(false);
    setFileName(file.name);
    setTimeframe(createDefaultTimeframe());
    setSelectedPlant("");
    setSelectedFolders([]);
    setJobId("");

    const formData = new FormData();
    formData.append("file", file);

    try {
      const response = await fetch(`${API_BASE_URL}/api/upload/start`, {
        method: "POST",
        body: formData
      });
      if (!response.ok) {
        throw new Error(await readApiError(response, `Upload failed with status ${response.status}`));
      }
      const startPayload = await response.json();
      if (!startPayload.valid || !startPayload.job_id) {
        setResult(null);
        setIntelligence(null);
        setJobId("");
        setErrors(startPayload.errors || ["The workbook could not be processed."]);
        return;
      }
      setJobId(startPayload.job_id);

      let finished = false;
      while (!finished) {
        await delay(900);
        const statusResponse = await fetch(`${API_BASE_URL}/api/upload/status/${startPayload.job_id}`);
        if (!statusResponse.ok) {
          throw new Error(await readApiError(statusResponse, `Upload status failed with status ${statusResponse.status}`));
        }
        const statusPayload = await statusResponse.json();
        setUploadProgress({
          progress: statusPayload.progress || 0,
          message: statusPayload.message || "Processing workbook"
        });

        if (statusPayload.result?.valid) {
          setResult(statusPayload.result);
        }

        if (statusPayload.status === "complete") {
          finished = true;
          if (statusPayload.result?.valid) {
            setResult(statusPayload.result);
          }
        } else if (statusPayload.status === "error" || !statusPayload.valid) {
          setResult(null);
          setIntelligence(null);
          setJobId("");
          setErrors(statusPayload.errors || [statusPayload.message || "The workbook could not be processed."]);
          finished = true;
        }
      }
    } catch (error) {
      setResult(null);
      setIntelligence(null);
      setJobId("");
      setErrors([error.message || "Unable to connect to the backend API."]);
    } finally {
      setLoading(false);
      setUploadProgress(null);
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
  const showLandingPage = !result && !loading && errors.length === 0;

  return (
    <div className="min-h-screen bg-[#f6f8fb] text-slate-900">
      {/* ── Header ──────────────────────────────────────────────── */}
      {!showLandingPage && (
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
      )}

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
      <main className={showLandingPage ? "w-full" : "mx-auto w-full max-w-[1800px] px-4 py-6 sm:px-5 xl:px-6"}>

        {/* Empty state */}
        {showLandingPage && (
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

        {loading && uploadProgress && (
          <section className="mb-4 rounded-xl border border-blue-100 bg-white p-4 shadow-soft">
            <div className="flex flex-wrap items-center justify-between gap-3">
              <div>
                <p className="text-sm font-semibold text-slate-950">
                  {result ? "Finishing full dashboard data" : "Processing uploaded report"}
                </p>
                <p className="mt-1 text-sm text-slate-500">{uploadProgress.message}</p>
              </div>
              <span className="text-sm font-bold text-blue-700">{Math.round(uploadProgress.progress || 0)}%</span>
            </div>
            <div className="mt-3 h-2 overflow-hidden rounded-full bg-slate-100">
              <div
                className="h-full rounded-full bg-blue-600 transition-all"
                style={{ width: `${Math.min(Math.max(Number(uploadProgress.progress || 0), 0), 100)}%` }}
              />
            </div>
          </section>
        )}

        {result?.partial && (
          <section className="mb-4 rounded-xl border border-amber-200 bg-amber-50 p-4 text-amber-950">
            <div className="flex flex-wrap items-start justify-between gap-3">
              <div>
                <p className="text-sm font-semibold">Showing partial data: latest fiscal quarter loaded.</p>
                <p className="mt-1 text-sm text-amber-800">
                  Remaining quarters are still processing. {formatPartialLoadSummary(result)}
                </p>
              </div>
              <span className="rounded-full border border-amber-300 bg-white px-3 py-1 text-xs font-bold text-amber-800">
                {Math.min(result.loaded_quarters?.length || 0, result.total_quarters || 0)} / {result.total_quarters || 0} quarters
              </span>
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
            jobId={jobId}
            selectedPlant={selectedPlant}
            selectedFolders={selectedFolders}
            timeframeMode={timeframe.mode}
            timeframeRange={timeframeRange}
          />
        )}

        {/* Empty timeframe */}
        {!loading && selectedPlant && filteredResult && filteredResult.daily.length === 0 && errors.length === 0 && (
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
    <section className="relative min-h-screen overflow-hidden bg-[#eef6ff] text-slate-950">
      <input
        ref={inputRef}
        type="file"
        accept=".xlsx,.xls"
        className="hidden"
        onChange={(event) => submitFile(event.target.files?.[0])}
      />

      <div className="absolute inset-x-0 bottom-0 h-52 opacity-70" aria-hidden="true">
        <div className="h-full w-full bg-[linear-gradient(165deg,transparent_0_38%,rgba(37,99,235,0.10)_38.4%,transparent_39%,transparent_45%,rgba(37,99,235,0.08)_45.4%,transparent_46%,transparent_52%,rgba(14,165,233,0.08)_52.4%,transparent_53%)]" />
      </div>

      <div className="relative mx-auto flex min-h-screen w-full max-w-[1200px] flex-col px-5 py-5 sm:px-8">
        <nav className="flex items-center justify-between gap-4">
          <div className="flex min-w-0 items-center gap-3">
            <div className="flex h-11 w-11 shrink-0 items-center justify-center rounded-xl bg-blue-600 text-white shadow-[0_12px_28px_rgba(37,99,235,0.25)]">
              <BarChart2 className="h-6 w-6" aria-hidden="true" />
            </div>
            <div className="min-w-0">
              <p className="truncate text-base font-extrabold text-slate-950">Plant Capacity Utilization</p>
              <p className="truncate text-xs font-semibold text-blue-600">Tower-level capacity intelligence</p>
            </div>
          </div>

          <button
            type="button"
            onClick={() => inputRef.current?.click()}
            className="inline-flex h-10 shrink-0 items-center justify-center gap-2 rounded-xl bg-slate-950 px-4 text-sm font-bold text-white shadow-sm transition hover:bg-blue-700"
          >
            <UploadCloud className="h-4 w-4" aria-hidden="true" />
            Upload
          </button>
        </nav>

        <div className="grid flex-1 items-center gap-10 py-10 lg:grid-cols-[0.92fr_1.08fr] lg:py-8">
          <div className="max-w-xl">
            <h1 className="text-[44px] font-black leading-[1.04] text-slate-950 sm:text-[58px] lg:text-[64px]">
              Understand Your Capacity. <span className="text-blue-600">Instantly.</span>
            </h1>
            <p className="mt-6 max-w-lg text-base font-medium leading-8 text-slate-700 sm:text-lg">
              Upload an Excel production report to map plant capacity by tower, folder, product mix, and the 00:00-04:00 operating window.
            </p>

            <div id="features" className="mt-8 grid grid-cols-2 gap-x-5 gap-y-5 sm:grid-cols-4">
              {[
                { icon: Layers, title: "Tower Mapping", detail: "Plant capacity by tower" },
                { icon: Gauge, title: "Live KPIs", detail: "Runtime, loss, spare" },
                { icon: Activity, title: "AI Insights", detail: "Ask the data" },
                { icon: ShieldCheck, title: "Scoped Views", detail: "Plant and period filters" },
              ].map(({ icon: Icon, title, detail }) => (
                <div key={title} className="min-w-0">
                  <div className="mb-3 flex h-10 w-10 items-center justify-center rounded-xl border border-blue-100 bg-white text-blue-600 shadow-sm">
                    <Icon className="h-5 w-5" aria-hidden="true" />
                  </div>
                  <p className="text-sm font-extrabold text-slate-950">{title}</p>
                  <p className="mt-1 text-xs font-medium leading-4 text-slate-500">{detail}</p>
                </div>
              ))}
            </div>

            <div
              onDragOver={(event) => { event.preventDefault(); setDragging(true); }}
              onDragLeave={() => setDragging(false)}
              onDrop={(event) => {
                event.preventDefault();
                setDragging(false);
                submitFile(event.dataTransfer.files?.[0]);
              }}
              className={`mt-10 max-w-[360px] rounded-xl border-2 border-dashed bg-white/84 p-6 text-center shadow-[0_16px_40px_rgba(37,99,235,0.10)] transition ${
                dragging ? "border-blue-500 ring-4 ring-blue-100" : "border-blue-200"
              }`}
            >
              <div className="mx-auto flex h-14 w-14 items-center justify-center rounded-2xl bg-blue-50 text-blue-600">
                <UploadCloud className="h-8 w-8" aria-hidden="true" />
              </div>
              <p className="mt-4 text-base font-extrabold text-slate-950">
                {dragging ? "Release to upload" : "Upload Excel Files"}
              </p>
              <p className="mt-1 text-sm leading-5 text-slate-500">
                Drag and drop production reports here or choose a workbook.
              </p>
              <button
                type="button"
                onClick={() => inputRef.current?.click()}
                className="mt-4 inline-flex h-10 items-center justify-center rounded-lg bg-blue-600 px-7 text-sm font-bold text-white shadow-sm transition hover:bg-blue-700"
              >
                Choose Files
              </button>
              <p className="mt-2 text-xs font-medium text-slate-400">.xlsx and .xls supported</p>
            </div>
          </div>

          <LandingDashboardPreview />
        </div>
      </div>
    </section>
  );
}

function LandingDashboardPreview() {
  const capacityBars = [
    { label: "01", snp: 54, gnp: 18, idle: 28 },
    { label: "05", snp: 46, gnp: 32, idle: 22 },
    { label: "09", snp: 58, gnp: 14, idle: 28 },
    { label: "13", snp: 42, gnp: 36, idle: 22 },
    { label: "17", snp: 62, gnp: 20, idle: 18 },
    { label: "21", snp: 51, gnp: 27, idle: 22 },
  ];
  const topTowers = [
    ["Tower 04", 92],
    ["Tower 07", 87],
    ["Tower 12", 81],
    ["Tower 02", 75],
  ];

  return (
    <div className="relative mx-auto w-full max-w-[590px]">
      <div className="absolute left-0 top-6 hidden h-[520px] w-16 rounded-2xl bg-slate-950 shadow-[0_22px_60px_rgba(15,23,42,0.30)] lg:block">
        <div className="flex h-full flex-col items-center gap-6 py-8 text-slate-500">
          {[BarChart2, Gauge, Layers, Activity, ShieldCheck].map((Icon, index) => (
            <div
              key={index}
              className={`flex h-8 w-8 items-center justify-center rounded-lg ${index === 0 ? "bg-blue-600 text-white" : "text-slate-400"}`}
            >
              <Icon className="h-4 w-4" aria-hidden="true" />
            </div>
          ))}
        </div>
      </div>

      <div className="relative rounded-2xl border border-white/80 bg-white p-4 shadow-[0_28px_70px_rgba(30,64,175,0.18)] lg:ml-11">
        <div className="mb-4 flex items-center justify-between">
          <div>
            <p className="text-sm font-extrabold text-slate-950">Overview</p>
            <p className="text-xs font-medium text-slate-400">Selected plant capacity snapshot</p>
          </div>
          <div className="flex h-8 items-center gap-1 rounded-full border border-slate-100 bg-slate-50 px-2">
            <span className="h-1.5 w-1.5 rounded-full bg-blue-500" />
            <span className="h-1.5 w-1.5 rounded-full bg-slate-300" />
            <span className="h-1.5 w-1.5 rounded-full bg-slate-300" />
          </div>
        </div>

        <div id="kpis" className="grid grid-cols-2 gap-3 sm:grid-cols-4">
          {[
            ["Total Towers", "17", "text-blue-600"],
            ["Runtime", "49%", "text-indigo-600"],
            ["Spare Capacity", "18%", "text-amber-500"],
            ["Utilization", "82%", "text-emerald-600"],
          ].map(([label, value, color]) => (
            <div key={label} className="rounded-lg border border-slate-100 bg-white p-3 shadow-[0_8px_20px_rgba(15,23,42,0.04)]">
              <p className="text-[11px] font-bold text-slate-400">{label}</p>
              <p className={`mt-2 text-2xl font-black ${color}`}>{value}</p>
            </div>
          ))}
        </div>

        <div className="mt-3 grid gap-3 lg:grid-cols-[1.08fr_0.92fr]">
          <div className="rounded-lg border border-slate-100 bg-white p-3 shadow-[0_8px_20px_rgba(15,23,42,0.04)]">
            <div className="flex items-center justify-between">
              <p className="text-xs font-extrabold text-slate-700">Capacity by Tower</p>
              <span className="text-[11px] font-bold text-blue-600">100% split</span>
            </div>
            <div className="mt-4 flex h-32 items-end gap-3">
              {capacityBars.map((bar) => (
                <div key={bar.label} className="flex h-full flex-1 flex-col justify-end gap-1">
                  <div className="flex h-[104px] flex-col justify-end overflow-hidden rounded-t-md bg-slate-100">
                    <div className="bg-blue-600" style={{ height: `${bar.snp}%` }} />
                    <div className="bg-sky-300" style={{ height: `${bar.gnp}%` }} />
                    <div className="bg-slate-200" style={{ height: `${bar.idle}%` }} />
                  </div>
                  <span className="text-center text-[10px] font-bold text-slate-400">{bar.label}</span>
                </div>
              ))}
            </div>
          </div>

          <div className="rounded-lg border border-slate-100 bg-white p-3 shadow-[0_8px_20px_rgba(15,23,42,0.04)]">
            <p className="text-xs font-extrabold text-slate-700">Capacity by Folder</p>
            <div className="mt-4 flex items-center gap-4">
              <div className="grid h-28 w-28 shrink-0 place-items-center rounded-full bg-[conic-gradient(#2563eb_0_45%,#38bdf8_45%_72%,#8b5cf6_72%_100%)]">
                <div className="grid h-16 w-16 place-items-center rounded-full bg-white text-xl font-black text-slate-950">128</div>
              </div>
              <div className="space-y-2 text-xs font-bold text-slate-500">
                <p><span className="mr-2 inline-block h-2 w-2 rounded-full bg-blue-600" />High</p>
                <p><span className="mr-2 inline-block h-2 w-2 rounded-full bg-sky-400" />Medium</p>
                <p><span className="mr-2 inline-block h-2 w-2 rounded-full bg-violet-500" />Low</p>
              </div>
            </div>
          </div>

          <div id="workflow" className="rounded-lg border border-slate-100 bg-white p-3 shadow-[0_8px_20px_rgba(15,23,42,0.04)]">
            <p className="text-xs font-extrabold text-slate-700">Utilization Trend</p>
            <svg className="mt-3 h-28 w-full" viewBox="0 0 220 110" role="img" aria-label="Utilization trend preview">
              <path d="M5 90 L34 72 L62 78 L90 54 L118 66 L146 34 L174 48 L210 18" fill="none" stroke="#2563eb" strokeWidth="4" strokeLinecap="round" strokeLinejoin="round" />
              <path d="M5 90 L34 72 L62 78 L90 54 L118 66 L146 34 L174 48 L210 18 L210 108 L5 108 Z" fill="#dbeafe" opacity="0.9" />
            </svg>
          </div>

          <div id="security" className="rounded-lg border border-slate-100 bg-white p-3 shadow-[0_8px_20px_rgba(15,23,42,0.04)]">
            <p className="text-xs font-extrabold text-slate-700">Top Towers by Utilization</p>
            <div className="mt-4 space-y-3">
              {topTowers.map(([name, value]) => (
                <div key={name} className="grid grid-cols-[68px_1fr_34px] items-center gap-2">
                  <span className="text-[11px] font-bold text-slate-500">{name}</span>
                  <span className="h-2 overflow-hidden rounded-full bg-slate-100">
                    <span className="block h-full rounded-full bg-blue-600" style={{ width: `${value}%` }} />
                  </span>
                  <span className="text-right text-[11px] font-black text-slate-700">{value}%</span>
                </div>
              ))}
            </div>
          </div>
        </div>
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
  const plantSource = Array.isArray(result.available_plants) && result.available_plants.length > 0
    ? result.available_plants
    : (result.details || []).map((row) => row.plant_name);
  return Array.from(
    new Set(plantSource.filter(Boolean))
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

function formatPartialLoadSummary(result) {
  const quarters = Array.isArray(result?.loaded_quarters) ? result.loaded_quarters : [];
  const quarterText = quarters.length > 0 ? `Loaded: ${quarters.join(", ")}.` : "";
  const range = result?.loaded_date_range || {};
  const rangeText = range.start && range.end
    ? `Current loaded date range: ${formatDateRangeLabel(range.start, range.end)}.`
    : "";
  return [quarterText, rangeText].filter(Boolean).join(" ");
}

function filterCapacityDataByScope(result, selectedPlant, selectedFolders) {
  if (!result || !selectedPlant) return null;
  const selectedFolderSet = new Set(selectedFolders);
  const plantDetails = (result.details || []).filter((row) => row.plant_name === selectedPlant);
  const dateUniverse = Array.from(new Set(plantDetails.map((row) => row.run_date).filter(Boolean))).sort();
  const details = selectedFolderSet.size > 0
    ? plantDetails.filter((row) => selectedFolderSet.has(row.folder))
    : plantDetails;
  const plantTowerDetails = (result.tower_details || []).filter((row) => row.plant_name === selectedPlant);
  const towerDetails = selectedFolderSet.size > 0
    ? plantTowerDetails.filter((row) => selectedFolderSet.has(row.folder))
    : plantTowerDetails;
  const daily = buildDailyRowsFromTowerDetails(towerDetails, dateUniverse);
  return { ...result, summary: calculateSummary(daily), daily, details, tower_details: towerDetails };
}

function buildDailyRowsFromTowerDetails(towerRows, dateUniverse) {
  const dates = (dateUniverse?.length ? dateUniverse : Array.from(
    new Set((towerRows || []).map((row) => row.run_date).filter(Boolean))
  )).sort();
  const towerKeys = Array.from(
    new Set((towerRows || []).map((row) => row.tower).filter(Boolean))
  ).sort((a, b) => a.localeCompare(b));
  const capacityTowersCount = towerKeys.length;

  if (!dates.length || capacityTowersCount <= 0) return [];

  const rowsByDateTower = new Map();
  for (const row of towerRows || []) {
    if (!row.run_date || !row.tower) continue;
    const key = `${row.run_date}||${row.tower}`;
    const rows = rowsByDateTower.get(key) || [];
    rows.push(row);
    rowsByDateTower.set(key, rows);
  }

  return dates.map((runDate) => {
    const towerDayRows = towerKeys.map((towerKey) => {
      const rows = rowsByDateTower.get(`${runDate}||${towerKey}`) || [];
      return rows.length > 0
        ? buildTowerDayCapacityValues(rows)
        : buildIdleTowerDayCapacityValues(towerKey);
    });
    const availableCapacity = capacityTowersCount * CAPACITY_WINDOW_MINUTES;
    const rawValues = {
      waiting_time: sumBy(towerDayRows, "waiting_time"),
      loss_time: sumBy(towerDayRows, "lost_time"),
      downtime: sumBy(towerDayRows, "downtime"),
      runtime: sumBy(towerDayRows, "runtime"),
      buffer_time: sumBy(towerDayRows, "buffer_time"),
      idle_time: sumBy(towerDayRows, "idle_time"),
    };
    const values = normalizeCapacityBucketValues(rawValues, availableCapacity);
    const utilizedTime = values.runtime + values.downtime + values.loss_time;
    const activeTowersCount = towerDayRows.filter((row) => row.active).length;

    return {
      run_date: runDate,
      active_towers_count: activeTowersCount,
      capacity_towers_count: capacityTowersCount,
      available_capacity: cleanNumber(availableCapacity),
      waiting_time: cleanNumber(values.waiting_time),
      runtime: cleanNumber(values.runtime),
      lost_time: cleanNumber(values.loss_time),
      downtime: cleanNumber(values.downtime),
      buffer_time: cleanNumber(values.buffer_time),
      idle_time: cleanNumber(values.idle_time),
      runtime_segments: aggregateRuntimeSegmentsForCapacity(towerDayRows, values.runtime),
      utilization_percentage: cleanNumber(
        availableCapacity > 0 ? Math.min((utilizedTime / availableCapacity) * 100, 100) : 0
      )
    };
  });
}

function buildTowerDayCapacityValues(rows) {
  const rawValues = {
    waiting_time: sumBy(rows, "waiting_time"),
    loss_time: rows.reduce((total, row) => total + getLossTime(row), 0),
    downtime: sumBy(rows, "downtime"),
    runtime: sumBy(rows, "runtime"),
    buffer_time: sumBy(rows, "buffer_time"),
    idle_time: sumBy(rows, "idle_time"),
  };
  const values = normalizeCapacityBucketValues(rawValues, CAPACITY_WINDOW_MINUTES);

  return {
    tower: rows[0]?.tower || "",
    active: rows.some(isActiveCapacityDetailRow),
    waiting_time: cleanNumber(values.waiting_time),
    lost_time: cleanNumber(values.loss_time),
    downtime: cleanNumber(values.downtime),
    runtime: cleanNumber(values.runtime),
    buffer_time: cleanNumber(values.buffer_time),
    idle_time: cleanNumber(values.idle_time),
    runtime_segments: aggregateRuntimeSegmentsForCapacity(rows, values.runtime),
  };
}

function buildIdleTowerDayCapacityValues(towerKey) {
  return {
    tower: towerKey,
    active: false,
    waiting_time: 0,
    lost_time: 0,
    downtime: 0,
    runtime: 0,
    buffer_time: 0,
    idle_time: CAPACITY_WINDOW_MINUTES,
    runtime_segments: [],
  };
}

function normalizeCapacityBucketValues(values, availableCapacity) {
  const capacity = Math.max(Number(availableCapacity || 0), 0);
  const normalized = {
    waiting_time: cleanNumber(Math.max(Number(values.waiting_time || 0), 0)),
    loss_time: cleanNumber(Math.max(Number(values.loss_time || 0), 0)),
    downtime: cleanNumber(Math.max(Number(values.downtime || 0), 0)),
    runtime: cleanNumber(Math.max(Number(values.runtime || 0), 0)),
    buffer_time: cleanNumber(Math.max(Number(values.buffer_time || 0), 0)),
    idle_time: cleanNumber(Math.max(Number(values.idle_time || 0), 0)),
  };

  if (capacity <= 0) return normalized;

  const componentKeys = ["waiting_time", "loss_time", "downtime", "runtime", "buffer_time", "idle_time"];
  let total = cleanNumber(componentKeys.reduce((sum, key) => sum + normalized[key], 0));

  if (total < capacity) {
    normalized.buffer_time = cleanNumber(normalized.buffer_time + capacity - total);
    return normalized;
  }

  if (total <= capacity) return normalized;

  let overage = cleanNumber(total - capacity);

  for (const key of ["idle_time", "buffer_time", "runtime", "downtime", "loss_time", "waiting_time"]) {
    if (overage <= 0) break;

    const reduction = Math.min(normalized[key], overage);
    normalized[key] = cleanNumber(normalized[key] - reduction);
    overage = cleanNumber(overage - reduction);
  }

  return normalized;
}

function aggregateRuntimeSegmentsForCapacity(rows, targetRuntime) {
  const runtime = Math.max(Number(targetRuntime || 0), 0);
  if (runtime <= 0 || !Array.isArray(rows) || rows.length === 0) return [];

  const buckets = new Map();

  for (const row of rows) {
    for (const segment of row.runtime_segments || []) {
      const minutes = Math.max(Number(segment.minutes || 0), 0);
      if (minutes <= 0) continue;
      const bucketKey = runtimeSegmentBucketKey(segment);
      const bucket = buckets.get(bucketKey) || {
        key: bucketKey,
        label: segment.label || runtimeSegmentLabel(bucketKey),
        minutes: 0,
        is_complex: Boolean(segment.is_complex),
        effective_speed: 0,
        print_order: 0,
        complexity_code: segment.complexity_code || "",
      };

      bucket.minutes = cleanNumber(bucket.minutes + minutes);
      bucket.is_complex = bucket.is_complex || Boolean(segment.is_complex);
      bucket.effective_speed = Math.max(bucket.effective_speed || 0, Number(segment.effective_speed || 0));
      bucket.print_order = cleanNumber((bucket.print_order || 0) + Number(segment.print_order || 0));
      if (!bucket.complexity_code && segment.complexity_code) bucket.complexity_code = segment.complexity_code;
      buckets.set(bucketKey, bucket);
    }
  }

  const orderedKeys = ["snp", "snp_complex", "gnp", "gnp_complex", "unknown"];
  const segments = orderedKeys
    .filter((key) => buckets.has(key))
    .map((key) => buckets.get(key));
  const totalMinutes = cleanNumber(segments.reduce((sum, segment) => sum + Number(segment.minutes || 0), 0));

  if (totalMinutes <= 0) return [];

  const scale = runtime / totalMinutes;
  return segments.map((segment) => ({
    ...segment,
    minutes: cleanNumber(segment.minutes * scale)
  }));
}

function runtimeSegmentBucketKey(segment) {
  const text = `${segment.key || ""} ${segment.type || ""} ${segment.label || ""}`.toLowerCase();
  const isComplex = Boolean(segment.is_complex || segment.isComplex || text.includes("complex"));
  const runtimeType = text.includes("snp")
    ? "snp"
    : text.includes("gnp")
      ? "gnp"
      : "unknown";

  if (runtimeType === "unknown") return runtimeType;
  return isComplex ? `${runtimeType}_complex` : runtimeType;
}

function runtimeSegmentLabel(key) {
  if (key.startsWith("snp")) return key.includes("complex") ? "SNP Complex" : "SNP";
  if (key.startsWith("gnp")) return key.includes("complex") ? "GNP Complex" : "GNP";
  return "Run Time";
}

function getLossTime(row) {
  const provided = Number(row.lost_time);
  if (Number.isFinite(provided) && provided > 0) return provided;

  return (
    Number(row.change_over_time || 0)
    + Number(row.reflong_related_downtime || 0)
    + Number(row.late_start_time || 0)
  );
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
    const availableCapacity = capacityFoldersCount * CAPACITY_WINDOW_MINUTES;
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
    const actualBounds = getDateBoundsInRange(dailyRows, { start, end });
    const displayStart = actualBounds?.start || start;
    const displayEnd = actualBounds?.end || end;
    return { key: "custom", start: displayStart, end: displayEnd, label: formatDateRangeLabel(displayStart, displayEnd) };
  }

  const options = periodOptions[timeframe.mode] || [];
  const selectedKey = timeframe.periods[timeframe.mode] || "";
  const selectedOption = options.find((option) => option.key === selectedKey)
    || buildPeriodOptionFromKey(timeframe.mode, selectedKey)
    || options[0];
  if (!selectedOption) return null;
  const actualBounds = getDateBoundsInRange(dailyRows, selectedOption);
  const displayStart = actualBounds?.start || selectedOption.start;
  const displayEnd = actualBounds?.end || selectedOption.end;
  return {
    ...selectedOption,
    start: displayStart,
    end: displayEnd,
    label: formatDateRangeLabel(displayStart, displayEnd)
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
  const totalWaitingTime = sumBy(dailyRows, "waiting_time");
  const totalRuntime = sumBy(dailyRows, "runtime");
  const totalLostTime = sumBy(dailyRows, "lost_time");
  const totalDowntime = sumBy(dailyRows, "downtime");
  const totalBufferTime = sumBy(dailyRows, "buffer_time");
  const totalIdleTime = sumBy(dailyRows, "idle_time");
  const plannedAvailableTime = Math.max(totalAvailable - totalIdleTime, 0);
  const utilizedTime = totalRuntime + totalDowntime + totalLostTime;
  return {
    total_available_capacity: cleanNumber(totalAvailable),
    total_waiting_time: cleanNumber(totalWaitingTime),
    total_runtime: cleanNumber(totalRuntime),
    total_lost_time: cleanNumber(totalLostTime),
    total_downtime: cleanNumber(totalDowntime),
    total_utilized_time: cleanNumber(utilizedTime),
    total_buffer_time: cleanNumber(totalBufferTime),
    total_idle_time: cleanNumber(totalIdleTime),
    average_utilization_percentage: cleanNumber(totalAvailable > 0 ? Math.min((utilizedTime / totalAvailable) * 100, 100) : 0),
    spare_capacity_percentage: cleanNumber(plannedAvailableTime > 0 ? Math.min((totalBufferTime / plannedAvailableTime) * 100, 100) : 0),
    idle_capacity_percentage: cleanNumber(totalAvailable > 0 ? Math.min((totalIdleTime / totalAvailable) * 100, 100) : 0),
    active_folder_days: cleanNumber(sumBy(dailyRows, "active_folders_count")),
    active_tower_days: cleanNumber(sumBy(dailyRows, "active_towers_count")),
    capacity_tower_days: cleanNumber(sumBy(dailyRows, "capacity_towers_count"))
  };
}

function getDateBounds(dailyRows) {
  const dates = dailyRows.map((row) => row.run_date).filter(Boolean).sort();
  return { start: dates[0] || "", end: dates[dates.length - 1] || "" };
}

function getDateBoundsInRange(dailyRows, range) {
  const dates = dailyRows
    .map((row) => row.run_date)
    .filter((date) => isDateInRange(date, range))
    .sort();
  if (dates.length === 0) return null;
  return { start: dates[0], end: dates[dates.length - 1] };
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
