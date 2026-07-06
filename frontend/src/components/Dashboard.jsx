import {
  Bar,
  BarChart,
  CartesianGrid,
  Cell,
  Legend,
  Line,
  LineChart,
  Pie,
  PieChart,
  ResponsiveContainer,
  Tooltip,
  XAxis,
  YAxis
} from "recharts";
import { ChevronLeft, ChevronRight, MessageSquare, Send, Trash2, X, ZoomIn, ZoomOut } from "lucide-react";
import { useEffect, useMemo, useRef, useState } from "react";
import ReactMarkdown from "react-markdown";
import remarkGfm from "remark-gfm";

const CHAT_API_BASE = String(import.meta.env.VITE_API_BASE_URL || "").replace(/\/+$/, "");

import CapacityIntelligenceWidget from "./CapacityIntelligenceWidget.jsx";
import LossTimeThresholdWidget from "./LossTimeThresholdWidget.jsx";
import KpiCard from "./KpiCard.jsx";

const CAPACITY_WINDOW_MINUTES = 240;
const CAPACITY_PAGE_SIZE = 7;
const PLANT_CAPACITY_PAGE_SIZE = 31;
const DAILY_CAPACITY_FOLDER_VIEW_ENABLED = false;

const CAPACITY_SPLIT_COLORS = {
  waiting_time: "#B0B0B0",
  loss_time: "#F3C97B",
  downtime: "#FF9AA2",
  runtime: "#B2CFB2",
  runtime_snp: "#CCDCCC",
  runtime_gnp: "#88AA88",
  spare_time: "#C5E1FF",
  idle_time: "#E5E7EB",
  idle_stripe: "#B4BBC7",
  window_line: "#234775",
  twin_folder: "#2563eb"
};

const CAPACITY_SPLIT_LEGEND = [
  { key: "waiting_time", label: "Wait Time", color: CAPACITY_SPLIT_COLORS.waiting_time },
  { key: "loss_time", label: "Loss Time", color: CAPACITY_SPLIT_COLORS.loss_time },
  { key: "downtime", label: "Downtime", color: CAPACITY_SPLIT_COLORS.downtime },
  { key: "runtime_snp", label: "Run Time: SNP", color: CAPACITY_SPLIT_COLORS.runtime_snp },
  { key: "runtime_gnp", label: "Run Time: GNP", color: CAPACITY_SPLIT_COLORS.runtime_gnp },
  { key: "spare_time", label: "Spare Time", color: CAPACITY_SPLIT_COLORS.spare_time },
  { key: "idle_time", label: "Unplanned Time", color: CAPACITY_SPLIT_COLORS.idle_time, pattern: "idle" },
  { key: "complex_prints", label: "Complex prints", marker: "triangle" },
  { key: "twin_folder", label: "Twin folders", marker: "twin" }
];

const RUNTIME_SEGMENT_STYLES = {
  snp: {
    color: CAPACITY_SPLIT_COLORS.runtime_snp,
    textColor: "#0f172a",
    label: "Run Time: SNP"
  },
  snp_complex: {
    color: CAPACITY_SPLIT_COLORS.runtime_snp,
    textColor: "#0f172a",
    label: "Run Time: SNP Complex",
    isComplex: true
  },
  gnp: {
    color: CAPACITY_SPLIT_COLORS.runtime_gnp,
    textColor: "#0f172a",
    label: "Run Time: GNP"
  },
  gnp_complex: {
    color: CAPACITY_SPLIT_COLORS.runtime_gnp,
    textColor: "#0f172a",
    label: "Run Time: GNP Complex",
    isComplex: true
  },
  unknown: {
    color: CAPACITY_SPLIT_COLORS.runtime,
    textColor: "#14532d",
    label: "Run"
  }
};

const FOLDER_ALIAS_COLORS = ["#2563eb", "#7c3aed", "#dc2626", "#d97706", "#059669", "#0f766e", "#475569"];

const BREAKDOWN_STACKS = [
  { key: "waiting_time", label: "Wait time", color: CAPACITY_SPLIT_COLORS.waiting_time },
  { key: "loss_time", label: "Loss time", color: CAPACITY_SPLIT_COLORS.loss_time },
  { key: "downtime", label: "Downtime", color: CAPACITY_SPLIT_COLORS.downtime },
  { key: "runtime_snp", label: "Run Time: SNP", color: RUNTIME_SEGMENT_STYLES.snp.color },
  { key: "runtime_gnp", label: "Run Time: GNP", color: RUNTIME_SEGMENT_STYLES.gnp.color },
  { key: "spare_time", label: "Spare time", color: CAPACITY_SPLIT_COLORS.spare_time }
];
const UNPLANNED_BREAKDOWN_STACK = {
  key: "idle_time",
  label: "Unplanned Time",
  color: CAPACITY_SPLIT_COLORS.idle_time
};
const FOLDER_BREAKDOWN_STACKS = [...BREAKDOWN_STACKS, UNPLANNED_BREAKDOWN_STACK];
const DEFAULT_BREAKDOWN_KEYS = FOLDER_BREAKDOWN_STACKS.map((stack) => stack.key);

export default function Dashboard({
  data,
  intelligence,
  intelligenceLoading,
  intelligenceError,
  jobId,
  selectedPlant,
  selectedFolders,
  timeframeMode,
  timeframeRange,
}) {
  const [focusedDay, setFocusedDay] = useState("");
  const [selectedBreakdownKeys, setSelectedBreakdownKeys] = useState(DEFAULT_BREAKDOWN_KEYS);
  const [chatOpen, setChatOpen] = useState(false);
  const [chatMessages, setChatMessages] = useState([]);
  const [chatInput, setChatInput] = useState("");
  const [chatLoading, setChatLoading] = useState(false);
  const [forceLLM, setForceLLM] = useState(true);
  const [chatSize, setChatSize] = useState({ width: 360, height: 480 });
  const chatEndRef = useRef(null);
  const prevIntelligenceRef = useRef(null);
  const chatSizeRef = useRef({ width: 360, height: 480 });

  useEffect(() => {
    chatEndRef.current?.scrollIntoView({ behavior: "smooth" });
  }, [chatMessages]);

  // When the timeframe/plant/folder changes, intelligence is re-fetched.
  // Clear stale chat history so the LLM doesn't see answers from the old context.
  useEffect(() => {
    if (
      intelligence !== null &&
      prevIntelligenceRef.current !== null &&
      prevIntelligenceRef.current !== intelligence
    ) {
      setChatMessages([]);
    }
    prevIntelligenceRef.current = intelligence;
  }, [intelligence]);

  function startChatResize(e, direction) {
    e.preventDefault();
    e.stopPropagation();
    const startX = e.clientX;
    const startY = e.clientY;
    const startW = chatSizeRef.current.width;
    const startH = chatSizeRef.current.height;

    function onMove(ev) {
      const dx = ev.clientX - startX;
      const dy = ev.clientY - startY;
      const next = {
        width: (direction === "nw" || direction === "w") ? Math.max(280, Math.min(900, startW - dx)) : startW,
        height: (direction === "nw" || direction === "n") ? Math.max(300, Math.min(900, startH - dy)) : startH,
      };
      chatSizeRef.current = next;
      setChatSize(next);
    }

    function onUp() {
      document.removeEventListener("mousemove", onMove);
      document.removeEventListener("mouseup", onUp);
    }

    document.addEventListener("mousemove", onMove);
    document.addEventListener("mouseup", onUp);
  }

  async function handleChatSend() {
    const text = chatInput.trim();
    if (!text || chatLoading) return;
    if (!jobId) {
      setChatMessages([
        ...chatMessages,
        { role: "user", content: text },
        {
          role: "assistant",
          content: "This dashboard session is no longer available. Please re-upload the file.",
          plan: null,
        },
      ]);
      setChatInput("");
      return;
    }
    setChatInput("");
    const nextMessages = [...chatMessages, { role: "user", content: text }];
    setChatMessages(nextMessages);
    setChatLoading(true);
    try {
      const response = await fetch(`${CHAT_API_BASE}/api/chat`, {
        method: "POST",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify({
          job_id: jobId,
          message: text,
          selected_plant: selectedPlant || "",
          selected_folders: selectedFolders || [],
          timeframe: timeframeRange ? { start: timeframeRange.start, end: timeframeRange.end } : null,
          history: chatMessages.map(({ role, content }) => ({ role, content })),
          force_full_llm: forceLLM,
        }),
      });
      const payload = await response.json();
      setChatMessages([...nextMessages, {
        role: "assistant",
        content: payload.answer || "No response.",
        plan: payload.plan || null,
        chart: payload.chart || null,
        confidence: payload.confidence ?? null,
        refined: payload.refined ?? false,
      }]);
    } catch {
      setChatMessages([...nextMessages, {
        role: "assistant",
        content: "The assistant service did not respond. Please try again.",
        plan: null,
      }]);
    } finally {
      setChatLoading(false);
    }
  }

  useEffect(() => {
    if (focusedDay && !data.daily.some((day) => day.run_date === focusedDay)) {
      setFocusedDay("");
    }
  }, [data.daily, focusedDay]);

  const breakdownDetails = useMemo(
    () =>
      focusedDay
        ? data.details.filter((row) => row.run_date === focusedDay)
        : data.details,
    [data.details, focusedDay]
  );

  const breakdownTowerDetails = useMemo(
    () => buildTowerBreakdownSourceRows(data.tower_details || [], focusedDay),
    [data.tower_details, focusedDay]
  );

  const breakdownProductionDays = focusedDay ? 1 : data.daily.length;

  const towerBreakdown = useMemo(() => {
    const rows = aggregateResourceCapacitySplit(breakdownTowerDetails, "tower", breakdownProductionDays);
    return rows.map((row) => {
      if (row.uv_tower) return row;
      // GNP production runs only on UV towers — absorb GNP minutes into SNP for non-UV rows
      return {
        ...row,
        runtime_snp: row.runtime_snp + row.runtime_gnp,
        runtime_snp_percentage: cleanNumber((row.runtime_snp_percentage || 0) + (row.runtime_gnp_percentage || 0)),
        runtime_gnp: 0,
        runtime_gnp_percentage: 0,
      };
    });
  }, [breakdownTowerDetails, breakdownProductionDays]);
  const folderBreakdown = useMemo(
    () => aggregateResourceCapacitySplit(breakdownDetails, "folder", breakdownProductionDays),
    [breakdownDetails, breakdownProductionDays]
  );
  const totalTowerCapacity = useMemo(
    () => calculateTotalTowerCapacity(data.daily),
    [data.daily]
  );
  const selectedTowerBreakdownStacks = useMemo(
    () => FOLDER_BREAKDOWN_STACKS.filter((stack) => selectedBreakdownKeys.includes(stack.key)),
    [selectedBreakdownKeys]
  );
  const selectedFolderBreakdownStacks = useMemo(
    () => FOLDER_BREAKDOWN_STACKS.filter((stack) => selectedBreakdownKeys.includes(stack.key)),
    [selectedBreakdownKeys]
  );

  const breakdownScope = focusedDay ? focusedDay : "Selected timeframe";

  function toggleBreakdownComponent(componentKey) {
    setSelectedBreakdownKeys((current) => {
      if (current.includes(componentKey)) {
        return current.length === 1 ? current : current.filter((key) => key !== componentKey);
      }

      return FOLDER_BREAKDOWN_STACKS
        .map((stack) => stack.key)
        .filter((key) => key === componentKey || current.includes(key));
    });
  }

  const totalAvailableCapacity = Number(data.summary.total_available_capacity || 0);
  const totalActiveTowerCapacity = Number(data.summary.active_tower_days || 0);
  const kpis = [
    ["Available Time", formatPercent(totalAvailableCapacity > 0 ? 100 : 0), "blue", formatKpiDuration(totalAvailableCapacity)],
    ["Runtime", formatPercent(calculatePercentage(data.summary.total_runtime, totalAvailableCapacity)), "green", formatKpiDuration(data.summary.total_runtime)],
    ["Wait Time", formatPercent(calculatePercentage(data.summary.total_waiting_time, totalAvailableCapacity)), "wait", formatKpiDuration(data.summary.total_waiting_time)],
    ["Lost Time", formatPercent(calculatePercentage(data.summary.total_lost_time, totalAvailableCapacity)), "amber", formatKpiDuration(data.summary.total_lost_time)],
    ["Downtime", formatPercent(calculatePercentage(data.summary.total_downtime, totalAvailableCapacity)), "red", formatKpiDuration(data.summary.total_downtime)],
    ["Spare Time", formatPercent(calculatePercentage(data.summary.total_buffer_time, totalAvailableCapacity)), "spare", formatKpiDuration(data.summary.total_buffer_time)],
    ["Unplanned Time", formatPercent(calculatePercentage(data.summary.total_idle_time, totalAvailableCapacity)), "unplanned", formatKpiDuration(data.summary.total_idle_time)],
    [
      "Spare Capacity",
      formatPercent(
        data.summary.spare_capacity_percentage
          ?? calculatePercentage(
            data.summary.total_buffer_time,
            totalAvailableCapacity - Number(data.summary.total_idle_time || 0)
          )
      ),
      "slate",
      "of planned tower time"
    ]
  ];

  return (
    <div className="mt-5 space-y-5">
      <section className="grid gap-3 sm:grid-cols-2 lg:grid-cols-4 2xl:grid-cols-8">
        {kpis.map(([label, value, tone, detail]) => (
          <KpiCard key={label} label={label} value={value} detail={detail} tone={tone} />
        ))}
      </section>

      <section className="rounded-lg border border-slate-200 bg-white p-3 shadow-soft">
        <div className="mb-3 flex flex-col gap-2 sm:flex-row sm:items-end sm:justify-between">
          <div>
            <h2 className="text-base font-semibold text-slate-950">Daily capacity split</h2>
            <p className="mt-1 text-sm text-slate-500">Tower-based plant capacity by Run Date</p>
          </div>
          {focusedDay && (
            <button
              type="button"
              onClick={() => setFocusedDay("")}
              className="inline-flex min-h-9 items-center justify-center gap-2 rounded-lg border border-slate-200 bg-white px-3 text-sm font-semibold text-slate-700 shadow-sm transition hover:bg-slate-50 hover:text-slate-950"
            >
              <X className="h-4 w-4" aria-hidden="true" />
              Clear day
            </button>
          )}
        </div>

        <CapacitySplitChart
          daily={data.daily}
          details={data.details}
          towerDetails={data.tower_details || []}
          timeframeMode={timeframeMode}
          timeframeRange={timeframeRange}
          selectedDay={focusedDay}
          onSelectDay={setFocusedDay}
        />
      </section>

      <section className="space-y-3">
        <div className="flex flex-col gap-2 sm:flex-row sm:items-center sm:justify-between">
          <div>
            <h2 className="text-base font-semibold text-slate-950">Drilldown charts</h2>
            <p className="mt-1 text-sm text-slate-500">
              {focusedDay ? `Showing ${focusedDay}` : "Showing selected timeframe"}
            </p>
          </div>
          {focusedDay && (
            <button
              type="button"
              onClick={() => setFocusedDay("")}
              className="inline-flex min-h-9 items-center justify-center gap-2 rounded-lg border border-slate-200 bg-white px-3 text-sm font-semibold text-slate-700 shadow-sm transition hover:bg-slate-50 hover:text-slate-950"
            >
              <X className="h-4 w-4" aria-hidden="true" />
              Clear day
            </button>
          )}
        </div>

        <BreakdownComponentSelector
          options={FOLDER_BREAKDOWN_STACKS}
          selectedKeys={selectedBreakdownKeys}
          onToggle={toggleBreakdownComponent}
        />

        <div className="grid gap-4 xl:grid-cols-2">
          <UtilizationBreakdownChart
            title="Tower utilization"
            subtitle={focusedDay ? "Tower capacity split for selected day" : "Average tower capacity split across the selected timeframe"}
            data={towerBreakdown}
            nameKey="tower"
            selectedStacks={selectedTowerBreakdownStacks}
            barSize={24}
            rowHeight={36}
            emptyMessage="No tower usage found for this selection."
            showPlannedNights={!focusedDay}
            patternedUnplanned
          />
          <UtilizationBreakdownChart
            title="Folder utilization"
            subtitle={focusedDay ? "Folder capacity split for selected day" : "Average folder capacity split across the selected timeframe"}
            data={folderBreakdown}
            nameKey="folder"
            selectedStacks={selectedFolderBreakdownStacks}
            barSize={24}
            rowHeight={54}
            emptyMessage="No folder usage found for this selection."
            showPlannedNights={!focusedDay}
            patternedUnplanned
          />
        </div>
      </section>

      <CapacityIntelligenceWidget
        intelligence={intelligence}
        loading={intelligenceLoading}
        error={intelligenceError}
        details={data.details}
        towerDetails={data.tower_details || []}
        daily={data.daily}
      />

      {/* <LossTimeThresholdWidget details={data.details} /> */}

      {/* ── AI Chatbot ─────────────────────────────────────────── */}
      <div style={{ position: "fixed", bottom: "24px", right: "24px", zIndex: 50 }}>
        {chatOpen && (
          <div
            className="absolute bottom-16 right-0 flex flex-col overflow-hidden rounded-2xl border border-slate-200 bg-white shadow-2xl"
            style={{ width: chatSize.width, height: chatSize.height }}
          >
            {/* Resize: left edge */}
            <div onMouseDown={(e) => startChatResize(e, "w")} style={{ position: "absolute", top: 16, left: 0, bottom: 0, width: 5, cursor: "ew-resize", zIndex: 20 }} />
            {/* Resize: top edge */}
            <div onMouseDown={(e) => startChatResize(e, "n")} style={{ position: "absolute", top: 0, left: 16, right: 0, height: 5, cursor: "ns-resize", zIndex: 20 }} />
            {/* Resize: top-left corner grip */}
            <div
              onMouseDown={(e) => startChatResize(e, "nw")}
              title="Drag to resize"
              style={{ position: "absolute", top: 0, left: 0, width: 16, height: 16, cursor: "nw-resize", zIndex: 21, display: "flex", alignItems: "center", justifyContent: "center" }}
            >
              <svg width="9" height="9" viewBox="0 0 9 9" fill="none" style={{ opacity: 0.35 }}>
                <circle cx="2" cy="2" r="1.2" fill="#64748b" />
                <circle cx="7" cy="2" r="1.2" fill="#64748b" />
                <circle cx="2" cy="7" r="1.2" fill="#64748b" />
                <circle cx="7" cy="7" r="1.2" fill="#64748b" />
              </svg>
            </div>
            {/* Header */}
            <div className="flex shrink-0 items-center justify-between border-b border-slate-100 bg-slate-50 px-4 py-3">
              <div className="flex items-center gap-2">
                <MessageSquare className="h-4 w-4 text-blue-600" aria-hidden="true" />
                <span className="text-sm font-semibold text-slate-800">Ask the data</span>
              </div>
              <div className="flex items-center gap-1">
                {chatMessages.length > 0 && (
                  <button
                    type="button"
                    onClick={() => setChatMessages([])}
                    title="Clear conversation"
                    className="flex h-7 w-7 items-center justify-center rounded-lg text-slate-400 transition hover:bg-slate-200 hover:text-slate-600"
                  >
                    <Trash2 className="h-4 w-4" aria-hidden="true" />
                  </button>
                )}
                <button
                  type="button"
                  onClick={() => setChatOpen(false)}
                  title="Close chat"
                  className="flex h-7 w-7 items-center justify-center rounded-lg text-slate-400 transition hover:bg-slate-200 hover:text-slate-600"
                >
                  <X className="h-4 w-4" aria-hidden="true" />
                </button>
              </div>
            </div>

            {/* Messages */}
            <div className="flex-1 space-y-3 overflow-y-auto px-4 py-3">
              {chatMessages.length === 0 && (
                <p className="mt-8 text-center text-xs text-slate-400">
                  Ask about towers, folders, utilization, or loss time.
                </p>
              )}
              {chatMessages.map((msg, idx) => (
                <div key={idx} className={`flex ${msg.role === "user" ? "justify-end" : "justify-start"}`}>
                  <div className={`max-w-[82%] ${msg.role === "user" ? "" : "flex flex-col gap-1"}`}>
                    <div
                      className={`rounded-2xl px-3 py-2 text-sm leading-relaxed ${
                        msg.role === "user"
                          ? "whitespace-pre-wrap rounded-br-sm bg-blue-600 text-white"
                          : "rounded-bl-sm bg-slate-100 text-slate-800"
                      }`}
                    >
                      <ChatMessageContent content={msg.content} role={msg.role} />
                    </div>
                    {msg.role === "assistant" && msg.chart && (
                      <ChatChart chart={msg.chart} />
                    )}
                    {msg.role === "assistant" && msg.refined && (
                      <div className="ml-1 mt-1 flex items-center gap-1.5 text-xs text-violet-600">
                        <svg className="h-3 w-3 shrink-0" viewBox="0 0 12 12" fill="none" stroke="currentColor" strokeWidth="1.5" strokeLinecap="round" strokeLinejoin="round">
                          <path d="M6 1l1.2 2.4L10 4.2l-2 1.95.47 2.75L6 7.6l-2.47 1.3L4 6.15 2 4.2l2.8-.8z"/>
                        </svg>
                        Enhanced with full AI analysis
                      </div>
                    )}
                    {msg.role === "assistant" && !msg.refined && msg.confidence !== null && msg.confidence !== undefined && (
                      <div className="ml-1 mt-1 text-xs text-slate-400">
                        Confidence: {Math.round(msg.confidence * 100)}%
                      </div>
                    )}
                    {msg.role === "assistant" && msg.plan && (
                      <details className="ml-1">
                        <summary className="cursor-pointer select-none text-xs text-slate-400 hover:text-slate-600 list-none flex items-center gap-1">
                          <svg className="h-3 w-3" viewBox="0 0 12 12" fill="none" stroke="currentColor" strokeWidth="1.5" strokeLinecap="round" strokeLinejoin="round">
                            <path d="M4 2l4 4-4 4"/>
                          </svg>
                          How I answered this
                        </summary>
                        <div className="mt-1 rounded-xl border border-slate-200 bg-slate-50 px-3 py-2 text-xs text-slate-600 space-y-1">
                          {msg.plan.intent && <div><span className="font-medium text-slate-500">Intent:</span> {msg.plan.intent}</div>}
                          {msg.plan.primary_source && (
                            <div><span className="font-medium text-slate-500">Source:</span> {msg.plan.primary_source}{msg.plan.secondary_sources?.length > 0 ? `, ${msg.plan.secondary_sources.join(", ")}` : ""}</div>
                          )}
                          {msg.plan.metrics?.length > 0 && (
                            <div><span className="font-medium text-slate-500">Metrics:</span> {msg.plan.metrics.map(m => typeof m === "object" ? (m.label || m.field) : m).join(", ")}</div>
                          )}
                          {msg.plan.conditions?.length > 0 && (
                            <div><span className="font-medium text-slate-500">Conditions ({msg.plan.condition_logic || "AND"}):</span> {msg.plan.conditions.map(c => c.label || `${c.field} ${c.op} ${c.value}`).join(` ${msg.plan.condition_logic || "AND"} `)}</div>
                          )}
                          {msg.plan.filters && Object.keys(msg.plan.filters).length > 0 && (
                            <div><span className="font-medium text-slate-500">Filters:</span> {Object.entries(msg.plan.filters).map(([k, v]) => `${k}${formatPlanFilterValue(v)}`).join(", ")}</div>
                          )}
                          {msg.plan.time_scope?.type && msg.plan.time_scope.type !== "none" && (
                            <div><span className="font-medium text-slate-500">Time scope:</span> {msg.plan.time_scope.weekdays?.join(", ") || msg.plan.time_scope.months?.join(", ") || msg.plan.time_scope.specific_date || `${msg.plan.time_scope.date_from || ""} → ${msg.plan.time_scope.date_to || ""}`}</div>
                          )}
                          {msg.plan.entities?.length > 0 && (
                            <div><span className="font-medium text-slate-500">Entities:</span> {msg.plan.entities.map(e => `${e.type}: ${e.value}`).join(", ")}</div>
                          )}
                          {msg.plan.group_by && msg.plan.group_by !== "none" && <div><span className="font-medium text-slate-500">Group by:</span> {msg.plan.group_by}</div>}
                          {msg.plan.computation && <div><span className="font-medium text-slate-500">Computation:</span> {msg.plan.computation}</div>}
                          {msg.plan.output_format && <div><span className="font-medium text-slate-500">Output:</span> {msg.plan.output_format}</div>}
                        </div>
                      </details>
                    )}
                  </div>
                </div>
              ))}
              {chatLoading && (
                <div className="flex justify-start">
                  <div className="rounded-2xl rounded-bl-sm bg-slate-100 px-3 py-2.5">
                    <div className="flex items-center gap-1">
                      <span className="h-1.5 w-1.5 animate-bounce rounded-full bg-slate-400" style={{ animationDelay: "0ms" }} />
                      <span className="h-1.5 w-1.5 animate-bounce rounded-full bg-slate-400" style={{ animationDelay: "150ms" }} />
                      <span className="h-1.5 w-1.5 animate-bounce rounded-full bg-slate-400" style={{ animationDelay: "300ms" }} />
                    </div>
                  </div>
                </div>
              )}
              <div ref={chatEndRef} />
            </div>

            {/* Input */}
            <div className="shrink-0 border-t border-slate-100 p-3">
              <div className="flex items-end gap-2">
                <textarea
                  rows={2}
                  value={chatInput}
                  onChange={(e) => setChatInput(e.target.value)}
                  onKeyDown={(e) => { if (e.key === "Enter" && !e.shiftKey) { e.preventDefault(); handleChatSend(); } }}
                  placeholder={intelligenceLoading ? "Loading new data…" : "Ask a question… (Shift+Enter for new line)"}
                  disabled={chatLoading || intelligenceLoading}
                  className="min-w-0 flex-1 resize-y rounded-xl border border-slate-200 bg-slate-50 px-3 py-2 text-sm outline-none transition focus:border-blue-400 focus:bg-white focus:ring-1 focus:ring-blue-100 disabled:opacity-60"
                  style={{ minHeight: "2.5rem", maxHeight: "12rem" }}
                />
                <button
                  type="button"
                  onClick={() => setForceLLM((v) => !v)}
                  title={forceLLM ? "Full AI mode on — click to use fast mode" : "Fast mode — click to use full AI"}
                  className={`flex h-9 w-9 shrink-0 items-center justify-center rounded-xl transition ${
                    forceLLM
                      ? "bg-violet-600 text-white hover:bg-violet-700"
                      : "bg-slate-100 text-slate-400 hover:bg-slate-200 hover:text-slate-600"
                  }`}
                >
                  <svg className="h-4 w-4" viewBox="0 0 16 16" fill="none" stroke="currentColor" strokeWidth="1.5" strokeLinecap="round" strokeLinejoin="round">
                    <path d="M8 1.5l1.5 3L13 5.5l-2.5 2.4.6 3.4L8 9.8l-3.1 1.5.6-3.4L3 5.5l3.5-1z"/>
                    <path d="M4 12l-2 2M12 12l2 2"/>
                  </svg>
                </button>
                <button
                  type="button"
                  onClick={handleChatSend}
                  disabled={chatLoading || intelligenceLoading || !chatInput.trim()}
                  className="flex h-9 w-9 shrink-0 items-center justify-center rounded-xl bg-blue-600 text-white transition hover:bg-blue-700 disabled:bg-slate-200 disabled:text-slate-400"
                >
                  <Send className="h-4 w-4" aria-hidden="true" />
                </button>
              </div>
              {forceLLM && (
                <p className="mt-1.5 text-center text-xs text-violet-500">Full AI mode — bypassing fast path</p>
              )}
            </div>
          </div>
        )}

        {/* Toggle button */}
        <button
          type="button"
          onClick={() => setChatOpen((open) => !open)}
          className={`flex h-14 w-14 items-center justify-center rounded-full shadow-lg transition ${
            chatOpen ? "bg-slate-700 hover:bg-slate-800" : "bg-blue-600 hover:bg-blue-700"
          }`}
          title={chatOpen ? "Close chat" : "Ask the data"}
        >
          {chatOpen
            ? <X className="h-6 w-6 text-white" aria-hidden="true" />
            : <MessageSquare className="h-6 w-6 text-white" aria-hidden="true" />
          }
        </button>
      </div>
    </div>
  );
}

const CHAT_MARKDOWN_COMPONENTS = {
  p: ({ children }) => <p className="mb-2 last:mb-0">{children}</p>,
  strong: ({ children }) => <strong className="font-semibold text-slate-900">{children}</strong>,
  em: ({ children }) => <em className="italic">{children}</em>,
  a: ({ children, href }) => (
    <a href={href} target="_blank" rel="noreferrer" className="text-blue-600 underline hover:text-blue-700">
      {children}
    </a>
  ),
  ul: ({ children }) => <ul className="mb-2 list-disc space-y-0.5 pl-4 last:mb-0">{children}</ul>,
  ol: ({ children }) => <ol className="mb-2 list-decimal space-y-0.5 pl-4 last:mb-0">{children}</ol>,
  li: ({ children }) => <li>{children}</li>,
  h1: ({ children }) => <h1 className="mb-1 text-sm font-bold">{children}</h1>,
  h2: ({ children }) => <h2 className="mb-1 text-sm font-bold">{children}</h2>,
  h3: ({ children }) => <h3 className="mb-1 text-sm font-semibold">{children}</h3>,
  blockquote: ({ children }) => (
    <blockquote className="mb-2 border-l-2 border-slate-300 pl-2 text-slate-600 last:mb-0">{children}</blockquote>
  ),
  code: ({ children }) => (
    <code className="rounded bg-slate-200/70 px-1 py-0.5 text-[0.8em] text-slate-800">{children}</code>
  ),
  hr: () => <hr className="my-2 border-slate-200" />,
  table: ({ children }) => (
    <div className="mb-2 max-w-full overflow-x-auto rounded-md border border-slate-200 bg-white last:mb-0">
      <table className="min-w-full border-collapse text-left text-xs">{children}</table>
    </div>
  ),
  thead: ({ children }) => <thead className="bg-slate-50 text-slate-600">{children}</thead>,
  tbody: ({ children }) => <tbody className="divide-y divide-slate-100">{children}</tbody>,
  tr: ({ children }) => <tr className="odd:bg-white even:bg-slate-50/70">{children}</tr>,
  th: ({ children }) => (
    <th scope="col" className="border-b border-slate-200 px-2 py-1.5 font-semibold whitespace-nowrap">
      {children}
    </th>
  ),
  td: ({ children }) => <td className="px-2 py-1.5 align-top text-slate-700">{children}</td>,
};

const CHAT_CHART_PALETTE = ["#2563eb", "#16a34a", "#f59e0b", "#ef4444", "#8b5cf6", "#06b6d4", "#ec4899", "#84cc16", "#f97316", "#64748b"];
const CHAT_CAPACITY_SLICE_COLORS = {
  "Run Time": CAPACITY_SPLIT_COLORS.runtime,
  "Run Time: SNP": CAPACITY_SPLIT_COLORS.runtime_snp,
  "Run Time: GNP": CAPACITY_SPLIT_COLORS.runtime_gnp,
  "Loss Time": CAPACITY_SPLIT_COLORS.loss_time,
  "Downtime": CAPACITY_SPLIT_COLORS.downtime,
  "Wait Time": CAPACITY_SPLIT_COLORS.waiting_time,
  "Spare Time": CAPACITY_SPLIT_COLORS.spare_time,
  "Unplanned Time": CAPACITY_SPLIT_COLORS.idle_time,
};

function ChatChart({ chart }) {
  if (!chart || !Array.isArray(chart.data) || chart.data.length === 0) return null;
  const unitSuffix = chart.unit ? ` ${chart.unit}` : "";
  const tooltipFormatter = (value) => [`${value}${unitSuffix}`, chart.metric_label || "Value"];

  return (
    <div className="mt-1 rounded-xl border border-slate-200 bg-white p-2">
      {chart.title && <p className="mb-1 text-[11px] font-semibold text-slate-600">{chart.title}</p>}
      <div style={{ width: "100%", height: chart.type === "pie" ? 220 : 200 }}>
        <ResponsiveContainer>
          {chart.type === "line" ? (
            <LineChart data={chart.data} margin={{ top: 4, right: 8, left: -16, bottom: 0 }}>
              <CartesianGrid strokeDasharray="3 3" stroke="#e2e8f0" />
              <XAxis dataKey="label" tick={{ fontSize: 10 }} interval="preserveStartEnd" />
              <YAxis tick={{ fontSize: 10 }} />
              <Tooltip formatter={tooltipFormatter} />
              <Line type="monotone" dataKey="value" stroke="#2563eb" strokeWidth={2} dot={{ r: 2 }} />
            </LineChart>
          ) : chart.type === "bar" ? (
            <BarChart data={chart.data} margin={{ top: 4, right: 8, left: -16, bottom: 24 }}>
              <CartesianGrid strokeDasharray="3 3" stroke="#e2e8f0" />
              <XAxis dataKey="label" tick={{ fontSize: 9 }} interval={0} angle={-30} textAnchor="end" height={50} />
              <YAxis tick={{ fontSize: 10 }} />
              <Tooltip formatter={tooltipFormatter} />
              <Bar dataKey="value" fill="#2563eb" radius={[4, 4, 0, 0]} />
            </BarChart>
          ) : (
            <PieChart>
              <Pie
                data={chart.data}
                dataKey="value"
                nameKey="label"
                cx="50%"
                cy="50%"
                outerRadius={70}
                label={({ percent }) => `${(percent * 100).toFixed(0)}%`}
                labelLine={false}
              >
                {chart.data.map((entry, idx) => (
                  <Cell
                    key={entry.label}
                    fill={CHAT_CAPACITY_SLICE_COLORS[entry.label] || CHAT_CHART_PALETTE[idx % CHAT_CHART_PALETTE.length]}
                  />
                ))}
              </Pie>
              <Tooltip formatter={tooltipFormatter} />
              <Legend wrapperStyle={{ fontSize: 10 }} />
            </PieChart>
          )}
        </ResponsiveContainer>
      </div>
    </div>
  );
}

function formatPlanFilterValue(value) {
  if (value && typeof value === "object" && "op" in value && "value" in value) {
    return ` ${value.op} ${value.value}`;
  }
  return `=${value}`;
}

function ChatMessageContent({ content, role }) {
  const text = String(content || "");
  if (role !== "assistant") {
    return text;
  }

  return (
    <div className="space-y-1 [&>*:last-child]:mb-0">
      <ReactMarkdown remarkPlugins={[remarkGfm]} components={CHAT_MARKDOWN_COMPONENTS}>
        {text}
      </ReactMarkdown>
    </div>
  );
}

function CapacitySplitChart({ daily, details, towerDetails, timeframeMode, timeframeRange, selectedDay, onSelectDay }) {
  const [pageStart, setPageStart] = useState(0);
  const [viewMode, setViewMode] = useState("plant");
  const [zoomedMonth, setZoomedMonth] = useState("");
  const [summaryPopover, setSummaryPopover] = useState(null);
  const chartFrameRef = useRef(null);
  const returnPageStartRef = useRef(0);
  const effectiveViewMode = DAILY_CAPACITY_FOLDER_VIEW_ENABLED ? viewMode : "plant";

  const chartModel = useMemo(
    () => buildCapacitySplitModel(daily, details, towerDetails),
    [daily, details, towerDetails]
  );

  const { days, folders, rows, plantRows, totalTowerCount } = chartModel;
  const isPlantView = effectiveViewMode === "plant";
  const capacityPeriods = useMemo(
    () => buildCapacityPeriodRows(plantRows, {
      timeframeMode,
      timeframeRange,
      zoomedMonth,
    }),
    [plantRows, timeframeMode, timeframeRange, zoomedMonth]
  );
  const chartKeys = isPlantView
    ? capacityPeriods.rows.map((row) => row.period_key)
    : days;
  const pageSize = isPlantView
    ? (capacityPeriods.grain === "month" ? 36 : PLANT_CAPACITY_PAGE_SIZE)
    : CAPACITY_PAGE_SIZE;
  const pageUnitLabel = capacityPeriods.grain === "month" ? "months" : "days";
  const maxPageStart = Math.max(chartKeys.length - pageSize, 0);
  const safePageStart = Math.min(pageStart, maxPageStart);
  const visibleDays = chartKeys.slice(safePageStart, safePageStart + pageSize);
  const selectedChartKey = summaryPopover?.day || selectedDay;
  const returnViewLabel = formatCapacityReturnViewLabel(timeframeMode, timeframeRange);
  const returnViewTitle = formatCapacityReturnViewTitle(timeframeMode, timeframeRange);

  useEffect(() => {
    setPageStart(0);
  }, [chartKeys.length, folders.length, effectiveViewMode, capacityPeriods.grain, zoomedMonth]);

  useEffect(() => {
    if (!zoomedMonth) return;
    if (plantRows.some((row) => getMonthKey(row.run_date) === zoomedMonth)) return;
    setZoomedMonth("");
  }, [plantRows, zoomedMonth]);

  const width = isPlantView ? 1800 : 1380;
  const height = 500;
  const margins = isPlantView
    ? { top: 18, right: 36, bottom: 76, left: 64 }
    : { top: 18, right: 8, bottom: 78, left: 62 };
  const dayLabelYOffset = isPlantView ? 28 : 58;
  const monthLabelGap = 14;
  const yAxisTitleX = 14;
  const plotWidth = width - margins.left - margins.right;
  const plotHeight = height - margins.top - margins.bottom;
  const yMax = isPlantView ? 100 : 270;
  const yTicks = isPlantView ? [0, 25, 50, 75, 100] : [0, 60, 120, 180, 240];
  const dayCount = Math.max(visibleDays.length, 1);
  const groupWidth = plotWidth / dayCount;
  const barsPerDay = isPlantView ? 1 : Math.max(folders.length, 1);
  const dayGap = isPlantView ? 8 : 28;
  const barGap = isPlantView ? 0 : 4;
  const availableGroupWidth = Math.max(groupWidth - dayGap, isPlantView ? 10 : 28);
  const barWidth = Math.min(
    isPlantView ? 44 : 38,
    Math.max(isPlantView ? 8 : 10, (availableGroupWidth - barGap * (barsPerDay - 1)) / barsPerDay)
  );
  const actualGroupWidth = barWidth * barsPerDay + barGap * (barsPerDay - 1);
  const viewRows = isPlantView
    ? capacityPeriods.rows.filter((row) => visibleDays.includes(row.period_key))
    : rows.filter((row) => visibleDays.includes(row.run_date));
  const twinMarkers = isPlantView ? [] : buildTwinFolderMarkers(rows, visibleDays);
  const twinMarkerFolderKeys = new Set(
    twinMarkers.flatMap((m) =>
      Array.from(m.folderIndexes).map((fi) => `${m.run_date}||${fi}`)
    )
  );
  const soloTwinFolderKeys = new Set(
    viewRows
      .filter((row) => row.twin_folder_mode && !twinMarkerFolderKeys.has(`${row.run_date}||${row.folderIndex}`))
      .map((row) => `${row.run_date}||${row.folderIndex}`)
  );
  const selectedHighlightColor = "#475569";
  const selectedDaySummary = useMemo(
    () => isPlantView
      ? buildPlantCapacityPeriodSummary(summaryPopover?.day, capacityPeriods.rows)
      : buildCapacityDaySummary(summaryPopover?.day, rows, folders.length),
    [capacityPeriods.rows, folders.length, isPlantView, rows, summaryPopover?.day]
  );

  useEffect(() => {
    if (!selectedDay) {
      setSummaryPopover((current) => current?.periodType === "day" ? null : current);
      return;
    }

    setSummaryPopover((current) => {
      if (!current || current.day === selectedDay) return current;
      return null;
    });
  }, [selectedDay]);

  useEffect(() => {
    setZoomedMonth("");
    setSummaryPopover(null);
    setPageStart(0);
    returnPageStartRef.current = 0;
  }, [timeframeMode, timeframeRange?.start, timeframeRange?.end]);

  function xFor(dayIndex, folderIndex = 0) {
    const groupStart = margins.left + dayIndex * groupWidth + (groupWidth - actualGroupWidth) / 2;
    return groupStart + folderIndex * (barWidth + barGap);
  }

  function groupStartFor(dayIndex) {
    return margins.left + dayIndex * groupWidth + (groupWidth - actualGroupWidth) / 2;
  }

  function daySeparatorXFor(dayIndex) {
    return margins.left + (dayIndex + 1) * groupWidth;
  }

  function yFor(minutes) {
    return margins.top + plotHeight - (Math.min(Math.max(minutes, 0), yMax) / yMax) * plotHeight;
  }

  function heightFor(minutes) {
    return (Math.min(Math.max(minutes, 0), yMax) / yMax) * plotHeight;
  }

  function setChartView(nextViewMode) {
    setViewMode(nextViewMode);
    setPageStart(0);
    setSummaryPopover(null);
  }

  function setPreviousPage() {
    setSummaryPopover(null);
    setPageStart((current) => Math.max(current - pageSize, 0));
  }

  function setNextPage() {
    setSummaryPopover(null);
    setPageStart((current) => Math.min(current + pageSize, maxPageStart));
  }

  function zoomIntoMonth(monthKey) {
    if (!monthKey) return;
    returnPageStartRef.current = safePageStart;
    setZoomedMonth(monthKey);
    setPageStart(0);
    setSummaryPopover(null);
    onSelectDay("");
  }

  function zoomOutMonth() {
    setZoomedMonth("");
    setPageStart(returnPageStartRef.current || 0);
    setSummaryPopover(null);
    onSelectDay("");
  }

  function handleBarClick(event, row) {
    const bounds = chartFrameRef.current?.getBoundingClientRect();
    if (isPlantView && row.period_type === "month") {
      onSelectDay("");
    } else {
      onSelectDay(row.run_date);
    }

    if (!bounds) {
      setSummaryPopover({ day: row.period_key || row.run_date, periodType: row.period_type || "day", left: 12, top: 12 });
      return;
    }

    const cardWidth = 400;
    const cardHeight = 360;
    const localX = event.clientX - bounds.left;
    const localY = event.clientY - bounds.top;
    const left = localX + cardWidth + 24 > bounds.width
      ? Math.max(12, localX - cardWidth - 12)
      : Math.max(12, localX + 12);
    const top = Math.min(
      Math.max(12, localY - 24),
      Math.max(12, bounds.height - cardHeight - 12)
    );

    setSummaryPopover({ day: row.period_key || row.run_date, periodType: row.period_type || "day", left, top });
  }

  function handleBarDoubleClick(event, row) {
    if (!isPlantView || row.period_type !== "month") return;
    event.preventDefault();
    event.stopPropagation();
    zoomIntoMonth(row.month_key);
  }

  if (!days.length || (isPlantView ? !capacityPeriods.rows.length : !folders.length)) {
    return (
      <div className="flex h-80 items-center justify-center rounded-lg border border-dashed border-slate-200 bg-slate-50 text-sm text-slate-500">
        No tower capacity data found for this selection.
      </div>
    );
  }

  return (
    <div className="space-y-3">
      <div className="flex flex-col gap-3 lg:flex-row lg:items-center lg:justify-between">
        <div className="flex flex-wrap gap-x-4 gap-y-2 text-xs text-slate-700">
          {CAPACITY_SPLIT_LEGEND
            .filter((item) => DAILY_CAPACITY_FOLDER_VIEW_ENABLED || item.key !== "twin_folder")
            .map((item) => (
            <div key={item.key} className="inline-flex items-center gap-1.5">
              {item.marker === "triangle" ? (
                <span className="text-[11px] font-black leading-none text-slate-950">▲</span>
              ) : item.marker === "twin" ? (
                <svg className="h-4 w-8 shrink-0" viewBox="0 0 32 16" aria-hidden="true">
                  <path
                    d="M 4 5 Q 16 14, 28 5"
                    fill="none"
                    stroke={CAPACITY_SPLIT_COLORS.twin_folder}
                    strokeLinecap="round"
                    strokeWidth="2.3"
                    opacity="0.9"
                  />
                  <circle cx="4" cy="5" r="2.6" fill={CAPACITY_SPLIT_COLORS.twin_folder} opacity="0.9" />
                  <circle cx="28" cy="5" r="2.6" fill={CAPACITY_SPLIT_COLORS.twin_folder} opacity="0.9" />
                </svg>
              ) : (
                <span
                  className="h-3 w-3 rounded-sm border border-slate-300"
                  style={{
                    backgroundColor: item.color,
                    backgroundImage: item.pattern === "idle"
                      ? `repeating-linear-gradient(135deg, ${CAPACITY_SPLIT_COLORS.idle_stripe} 0 1px, transparent 1px 5px)`
                      : "none"
                  }}
                />
              )}
              <span>{item.label}</span>
            </div>
          ))}
          {isPlantView ? (
            <div className="inline-flex items-center gap-1.5">
              <span className="h-3 w-3 rounded-sm border border-slate-300 bg-white" />
              <span>Plant capacity: {formatNumber(totalTowerCount)} towers × 240 min</span>
            </div>
          ) : (
            <div className="inline-flex items-center gap-1.5">
              <span
                className="h-0 w-7 border-t border-dashed"
                style={{ borderColor: CAPACITY_SPLIT_COLORS.window_line }}
              />
              <span>4-hr Window</span>
            </div>
          )}
        </div>

        <div className="inline-flex flex-wrap items-center justify-end gap-2">
          {zoomedMonth && (
            <button
              type="button"
              onClick={zoomOutMonth}
              className="inline-flex min-h-9 max-w-full items-center justify-center gap-2 rounded-lg border border-blue-200 bg-white px-3 text-xs font-semibold text-blue-700 shadow-sm transition hover:bg-blue-50 hover:text-blue-800 sm:max-w-[320px]"
              title={`Back to ${returnViewTitle}`}
            >
              <ZoomOut className="h-4 w-4" aria-hidden="true" />
              <span className="truncate">Back to {returnViewLabel}</span>
            </button>
          )}
          {DAILY_CAPACITY_FOLDER_VIEW_ENABLED && (
          <div className="inline-flex rounded-lg border border-slate-200 bg-white p-0.5 shadow-sm">
            {[
              ["folder", "Folder"],
              ["plant", "Plant"]
            ].map(([mode, label]) => {
              const selected = effectiveViewMode === mode;

              return (
                <button
                  key={mode}
                  type="button"
                  onClick={() => setChartView(mode)}
                  className={`min-h-8 rounded-md px-3 text-xs font-semibold transition ${
                    selected
                      ? "bg-slate-900 text-white"
                      : "text-slate-600 hover:bg-slate-50 hover:text-slate-950"
                  }`}
                >
                  {label}
                </button>
              );
            })}
          </div>
          )}
          <button
            type="button"
            onClick={setPreviousPage}
            disabled={safePageStart === 0}
            aria-label={`Previous ${pageSize} ${pageUnitLabel}`}
            className="inline-flex h-9 w-9 items-center justify-center rounded-lg border border-slate-200 text-slate-600 transition hover:bg-slate-50 disabled:cursor-not-allowed disabled:opacity-40"
          >
            <ChevronLeft className="h-4 w-4" aria-hidden="true" />
          </button>
          <span className="min-w-28 text-center text-xs font-semibold text-slate-500">
            {safePageStart + 1}-{Math.min(safePageStart + pageSize, chartKeys.length)} of {chartKeys.length}
          </span>
          <button
            type="button"
            onClick={setNextPage}
            disabled={safePageStart >= maxPageStart}
            aria-label={`Next ${pageSize} ${pageUnitLabel}`}
            className="inline-flex h-9 w-9 items-center justify-center rounded-lg border border-slate-200 text-slate-600 transition hover:bg-slate-50 disabled:cursor-not-allowed disabled:opacity-40"
          >
            <ChevronRight className="h-4 w-4" aria-hidden="true" />
          </button>
        </div>
      </div>

      {!isPlantView && DAILY_CAPACITY_FOLDER_VIEW_ENABLED && (
        <div className="flex flex-wrap gap-x-4 gap-y-2 text-xs text-slate-700">
          {folders.map((folder) => (
            <span
              key={folder.key}
              className="inline-flex items-center gap-1.5 font-semibold"
            >
              <span
                className="h-3 w-3 rounded-sm border border-slate-300"
                style={{ backgroundColor: folder.color }}
                aria-hidden="true"
              />
              <span>{folder.alias}: {folder.shortName}</span>
            </span>
          ))}
        </div>
      )}

      <div ref={chartFrameRef} className="relative overflow-visible rounded-lg border border-slate-100 bg-[#f3f6fa] p-0.5">
        <svg
          className="h-[500px] w-full"
          viewBox={`0 0 ${width} ${height}`}
          role="img"
          aria-label={isPlantView ? "Daily plant capacity split" : "Daily machine-folder capacity split"}
        >
          <defs>
            <pattern id="idlePattern" patternUnits="userSpaceOnUse" width="5" height="5" patternTransform="rotate(35)">
              <rect width="5" height="5" fill={CAPACITY_SPLIT_COLORS.idle_time} />
              <line x1="0" y1="0" x2="0" y2="5" stroke={CAPACITY_SPLIT_COLORS.idle_stripe} strokeWidth="1" />
            </pattern>
          </defs>

          <rect x="0" y="0" width={width} height={height} fill="#f3f6fa" />

          {yTicks.map((tick) => (
            <g key={tick}>
              <line
                x1={margins.left}
                x2={width - margins.right}
                y1={yFor(tick)}
                y2={yFor(tick)}
                stroke={!isPlantView && tick === CAPACITY_WINDOW_MINUTES ? CAPACITY_SPLIT_COLORS.window_line : "#d9e1ea"}
                strokeDasharray={!isPlantView && tick === CAPACITY_WINDOW_MINUTES ? "6 5" : ""}
                strokeWidth={!isPlantView && tick === CAPACITY_WINDOW_MINUTES ? 1.8 : 1}
              />
              <text
                x={margins.left - 8}
                y={yFor(tick) + 4}
                textAnchor="end"
                fontSize="12"
                fill="#334155"
              >
                  {isPlantView ? formatPercent(tick) : formatHourTick(tick)}
              </text>
            </g>
          ))}

          <line
            x1={margins.left}
            x2={margins.left}
            y1={margins.top}
            y2={margins.top + plotHeight}
            stroke="#cbd5e1"
          />

          {visibleDays.slice(0, -1).map((day, dayIndex) => {
            const x = daySeparatorXFor(dayIndex);

            return (
              <line
                key={`${day}-day-separator`}
                x1={x}
                x2={x}
                y1={margins.top}
                y2={margins.top + plotHeight}
                stroke="#64748b"
                strokeDasharray="2 5"
                strokeWidth="0.9"
                opacity="0.55"
              />
            );
          })}

          {visibleDays.map((day, dayIndex) => {
            if (day !== selectedChartKey) return null;

            return (
              <rect
                key={`selected-${day}`}
                x={groupStartFor(dayIndex) - 8}
                y={margins.top - 5}
                width={actualGroupWidth + 16}
                height={plotHeight + 10}
                rx="6"
                fill="#ffffff"
                fillOpacity="0.45"
                stroke={selectedHighlightColor}
                strokeOpacity="0.65"
                strokeWidth="1"
              />
            );
          })}

          <text
            x={yAxisTitleX}
            y={margins.top + plotHeight / 2}
            textAnchor="middle"
            fontSize="12"
            fill="#0f172a"
            transform={`rotate(-90 ${yAxisTitleX} ${margins.top + plotHeight / 2})`}
          >
            {isPlantView ? "Share" : "Time"}
          </text>

          {viewRows.map((row) => {
            const rowKey = row.period_key || row.run_date;
            const dayIndex = visibleDays.indexOf(rowKey);
            const folder = isPlantView ? { alias: "Plant", color: "#334155" } : folders[row.folderIndex];
            const x = xFor(dayIndex, isPlantView ? 0 : row.folderIndex);
            const rowCapacity = Math.max(Number(row.total_capacity || CAPACITY_WINDOW_MINUTES), 1);
            let cursorY = yFor(0);
            const segmentLayouts = (row.segments || [])
              .map((segment) => {
                const segmentChartValue = isPlantView
                  ? calculateRawPercentage(segment.value, rowCapacity)
                  : segment.value;
                const segmentHeight = heightFor(segmentChartValue);
                const y = cursorY - segmentHeight;
                cursorY = y;
                const sparePercent = row.isIdle ? 0 : calculatePercentage(row.spare_time, rowCapacity);
                const runtimeLabelText = formatRuntimeSegmentLabel(segment);
                const runtimeLabelFontSize = calculateRuntimeLabelFontSize(runtimeLabelText, segmentHeight, barWidth);
                const canShowComplexIcon = segment.runtimeSegment && segment.isComplex && segmentHeight >= 10 && barWidth >= 8;
                const canShowRuntimeLabel = canShowComplexIcon || (
                  segment.runtimeSegment
                  && runtimeLabelText
                  && runtimeLabelFontSize >= 8
                );

                return {
                  segment,
                  segmentHeight,
                  y,
                  sparePercent,
                  runtimeLabelText,
                  runtimeLabelFontSize,
                  canShowComplexIcon,
                  canShowRuntimeLabel,
                };
              })
              .filter((layout) => Number(layout.segment.value || 0) > 0 && layout.segmentHeight > 0);
            const externalComplexMarkers = layoutExternalComplexMarkers(
              segmentLayouts.filter((layout) => layout.segment.runtimeSegment && layout.segment.isComplex && !layout.canShowRuntimeLabel),
              {
                x,
                barWidth,
                plotTop: margins.top,
                plotBottom: margins.top + plotHeight,
                chartRight: width - margins.right,
              }
            );

            return (
              <g
                key={`${rowKey}-${row.folderKey}`}
                onClick={(event) => handleBarClick(event, row)}
                onDoubleClick={(event) => handleBarDoubleClick(event, row)}
                className="cursor-pointer"
              >
                {segmentLayouts.map((layout) => {
                  const { segment, segmentHeight, y, sparePercent, runtimeLabelText, runtimeLabelFontSize, canShowComplexIcon, canShowRuntimeLabel } = layout;
                  const fill = getSegmentFill(segment);
                  const canShowSpareLabel = segment.key === "spare_time" && sparePercent > 0;
                  const showSpareLabelAboveBar = canShowSpareLabel && segmentHeight < 22;
                  const spareLabel = `${Math.round(sparePercent)}%`;
                  const spareLabelY = Math.max(margins.top + 10, y - 8);
                  const textRotation = `rotate(-90 ${x + barWidth / 2} ${y + segmentHeight / 2})`;

                  return (
                    <g key={segment.key}>
                      <rect
                        x={x}
                        y={y}
                        width={barWidth}
                        height={segmentHeight}
                        fill={fill}
                        stroke={segment.key === "idle_time" ? "#cbd5e1" : "rgba(255,255,255,0.75)"}
                        strokeWidth="0.6"
                      >
                        <title>
                          {formatCapacitySegmentTitle({
                            segment,
                            rowCapacity,
                            folderAlias: folder.alias,
                            isPlantView,
                          })}
                        </title>
                      </rect>
                      {canShowComplexIcon && (
                        <ComplexPrintStackIcon
                          x={x + barWidth / 2}
                          y={y + segmentHeight / 2}
                          color={segment.textColor || "#0f172a"}
                        />
                      )}
                      {canShowRuntimeLabel && !canShowComplexIcon && runtimeLabelText !== "▲" && (
                        <text
                          x={x + barWidth / 2}
                          y={y + segmentHeight / 2 + 3}
                          textAnchor="middle"
                          fontSize={runtimeLabelFontSize}
                          fontWeight="800"
                          fill={segment.textColor || "#14532d"}
                          transform={textRotation}
                          pointerEvents="none"
                        >
                          {runtimeLabelText}
                        </text>
                      )}
                      {canShowSpareLabel && showSpareLabelAboveBar && (
                        <g pointerEvents="none">
                          <rect
                            x={x + barWidth / 2 - 14}
                            y={spareLabelY - 13}
                            width="28"
                            height="16"
                            rx="3"
                            fill="#ffffff"
                            fillOpacity="0.88"
                            stroke="#cbd5e1"
                            strokeWidth="0.5"
                          />
                          <text
                            x={x + barWidth / 2}
                            y={spareLabelY}
                            textAnchor="middle"
                            fontSize="10"
                            fontWeight="800"
                            fill="#1e3a5f"
                          >
                            {spareLabel}
                          </text>
                        </g>
                      )}
                      {canShowSpareLabel && !showSpareLabelAboveBar && (
                        <g pointerEvents="none">
                          {barWidth < 30 && (
                            <rect
                              x={x + barWidth / 2 - 13}
                              y={y + segmentHeight / 2 - 8}
                              width="26"
                              height="15"
                              rx="3"
                              fill="#ffffff"
                              fillOpacity="0.72"
                            />
                          )}
                          <text
                            x={x + barWidth / 2}
                            y={y + segmentHeight / 2 + 4}
                            textAnchor="middle"
                            fontSize="11"
                            fontWeight="800"
                            fill="#1e3a5f"
                          >
                            {spareLabel}
                          </text>
                        </g>
                      )}
                    </g>
                  );
                })}
                {externalComplexMarkers.map((marker) => (
                  <ComplexPrintMarker
                    key={`complex-marker-${marker.segment.key}-${marker.index}`}
                    x={marker.x}
                    y={marker.y}
                    side={marker.side}
                    label={formatRuntimeSegmentShortType(marker.segment)}
                    color={marker.segment.textColor || "#0f172a"}
                  />
                ))}
              </g>
            );
          })}

          {visibleDays.map((day, dayIndex) => {
            const groupCenter = margins.left + dayIndex * groupWidth + groupWidth / 2;
            const dayLabelY = margins.top + plotHeight + dayLabelYOffset;
            const monthLabelY = dayLabelY + monthLabelGap;
            const weekdayLabelY = monthLabelY + monthLabelGap;
            const axisLabel = formatCapacityAxisLabel(day, capacityPeriods.grain);
            const dayTwinMarkers = twinMarkers.filter((marker) => marker.run_date === day);
            const axisLabelColor = axisLabel.isWeekend ? "#ea580c" : "#334155";
            return (
              <g key={day}>
                {!isPlantView && folders.map((folder, folderIndex) => (
                  <text
                    key={`${day}-${folder.key}`}
                    x={xFor(dayIndex, folderIndex) + barWidth / 2}
                    y={margins.top + plotHeight + 18}
                    textAnchor="middle"
                    fontSize="11"
                    fontWeight="700"
                    fill={folder.color}
                  >
                    {folder.alias}
                  </text>
                ))}
                {!isPlantView && dayTwinMarkers.map((marker, markerIndex) => {
                  const startX = xFor(dayIndex, marker.startFolderIndex) + barWidth / 2;
                  const endX = xFor(dayIndex, marker.endFolderIndex) + barWidth / 2;
                  const connectorY = margins.top + plotHeight + 36;
                  const curveDepth = 7;

                  return (
                    <g key={`${marker.twinGroup}-${markerIndex}`} pointerEvents="none">
                      <path
                        d={`M ${startX} ${connectorY} Q ${(startX + endX) / 2} ${connectorY + curveDepth}, ${endX} ${connectorY}`}
                        fill="none"
                        stroke="#ffffff"
                        strokeLinecap="round"
                        strokeWidth="5"
                        opacity="0.9"
                      />
                      <path
                        d={`M ${startX} ${connectorY} Q ${(startX + endX) / 2} ${connectorY + curveDepth}, ${endX} ${connectorY}`}
                        fill="none"
                        stroke="#2563eb"
                        strokeLinecap="round"
                        strokeWidth="2.3"
                        opacity="0.9"
                      />
                      <circle cx={startX} cy={connectorY} r="2.6" fill="#2563eb" opacity="0.9" />
                      <circle cx={endX} cy={connectorY} r="2.6" fill="#2563eb" opacity="0.9" />
                    </g>
                  );
                })}
                {!isPlantView && folders.map((folder, folderIndex) => {
                  if (!soloTwinFolderKeys.has(`${day}||${folderIndex}`)) return null;
                  const cx = xFor(dayIndex, folderIndex) + barWidth / 2;
                  const dotY = margins.top + plotHeight + 36;
                  return (
                    <circle
                      key={`solo-twin-${day}-${folderIndex}`}
                      cx={cx}
                      cy={dotY}
                      r="3"
                      fill="#2563eb"
                      opacity="0.9"
                      pointerEvents="none"
                    />
                  );
                })}
                <text
                  x={groupCenter}
                  y={dayLabelY}
                  textAnchor="middle"
                  fontSize="12"
                  fontWeight="800"
                  fill={axisLabelColor}
                >
                  {axisLabel.day}
                </text>
                <text
                  x={groupCenter}
                  y={monthLabelY}
                  textAnchor="middle"
                  fontSize="10"
                  fontWeight="700"
                  fill="#64748b"
                >
                  {axisLabel.month}
                </text>
                {axisLabel.weekday && (
                  <text
                    x={groupCenter}
                    y={weekdayLabelY}
                    textAnchor="middle"
                    fontSize="10"
                    fontWeight="800"
                    fill={axisLabelColor}
                  >
                    {axisLabel.weekday}
                  </text>
                )}
              </g>
            );
          })}
        </svg>

        {summaryPopover && selectedDaySummary && (
          <CapacityDaySummary
            summary={selectedDaySummary}
            style={{ left: summaryPopover.left, top: summaryPopover.top }}
            onClose={() => setSummaryPopover(null)}
            onZoomIn={selectedDaySummary.canZoom ? () => zoomIntoMonth(selectedDaySummary.zoomMonthKey) : null}
          />
        )}
      </div>
    </div>
  );
}

function CapacityDaySummary({ summary, style, onClose, onZoomIn }) {
  return (
    <section
      className="absolute z-50 max-h-[min(440px,calc(100vh-2rem))] w-[400px] max-w-[calc(100%-1rem)] overflow-y-auto rounded-lg border border-slate-200 bg-white p-3 pr-10 text-sm shadow-xl"
      style={style}
    >
      <button
        type="button"
        aria-label="Close selected day summary"
        onClick={onClose}
        className="absolute right-2 top-2 inline-flex h-7 w-7 shrink-0 items-center justify-center rounded-md text-slate-400 transition hover:bg-slate-100 hover:text-slate-700"
      >
        <X className="h-4 w-4" aria-hidden="true" />
      </button>

      {summary.dayLabel && (
        <div className="mb-2 border-b border-slate-100 pb-2">
          <p className="text-sm font-semibold text-slate-950">{summary.dayLabel}</p>
        </div>
      )}

      <div className="space-y-1.5">
        {summary.components.map((component) => (
          <div key={component.key} className="flex items-center justify-between gap-3">
            <div className="flex min-w-0 items-center gap-2 font-medium text-slate-600">
              <span className="h-2.5 w-2.5 shrink-0 rounded-sm" style={{ backgroundColor: component.color }} />
              <span>{component.label}</span>
            </div>
            <span className="shrink-0 font-semibold text-slate-950">
              {formatCapacitySummaryValue(component.value, summary.totalCapacity)}
            </span>
          </div>
        ))}
      </div>

      {summary.runtimeDetails.length > 0 && (
        <div className="mt-2 border-t border-slate-100 pt-2">
          <h4 className="text-xs font-semibold uppercase tracking-normal text-slate-500">
            Runtime details
          </h4>
          <div className="mt-1.5 space-y-1.5">
            {summary.runtimeDetails.map((detail) => (
              <div key={detail.key} className="rounded-md bg-slate-50 px-2 py-1.5">
                <div className="flex items-center justify-between gap-3">
                  <div className="flex min-w-0 items-center gap-1.5">
                    <span className="h-2.5 w-2.5 shrink-0 rounded-sm" style={{ backgroundColor: detail.color }} />
                    <span className="truncate font-semibold text-slate-700">
                      {detail.folderAlias}
                    </span>
                  </div>
                  <span className="shrink-0 font-mono text-xs font-semibold text-slate-500">
                    {formatMinutes(detail.minutes)}
                  </span>
                </div>
                <div className="mt-0.5 font-mono text-xs font-bold text-slate-950">
                  {detail.detailText}
                </div>
              </div>
            ))}
          </div>
        </div>
      )}

      <div className="mt-2 space-y-1.5 border-t border-slate-100 pt-2">
        <div className="flex items-center justify-between gap-3">
          <span className="font-medium text-slate-600">{summary.capacityUnitLabel || "Active folders"}</span>
          <span className="font-semibold text-slate-950">
            {formatNumber(summary.activeUnits ?? summary.activeFolders)}/{formatNumber(summary.totalUnits ?? summary.totalFolders)}
          </span>
        </div>
        <div className="flex items-center justify-between gap-3">
          <span className="font-medium text-slate-600">Utilized time</span>
          <span className="font-semibold text-slate-950">
            {formatCapacitySummaryValue(summary.utilization, summary.totalCapacity)}
          </span>
        </div>
      </div>

      {onZoomIn && (
        <button
          type="button"
          onClick={onZoomIn}
          className="mt-3 inline-flex min-h-9 w-full items-center justify-center gap-2 rounded-lg border border-blue-200 bg-blue-50 px-3 text-sm font-semibold text-blue-700 transition hover:bg-blue-100"
        >
          <ZoomIn className="h-4 w-4" aria-hidden="true" />
          Zoom in
        </button>
      )}
    </section>
  );
}

function BreakdownComponentSelector({ options, selectedKeys, onToggle }) {
  return (
    <section className="rounded-lg border border-slate-200 bg-white p-3 shadow-soft">
      <div className="flex flex-col gap-3 xl:flex-row xl:items-center xl:justify-between">
        <div>
          <h3 className="text-sm font-semibold text-slate-950">Drilldown includes</h3>
          <p className="mt-1 text-xs text-slate-500">Select main capacity components shown in the drilldown bars.</p>
        </div>
        <div className="grid gap-2 sm:grid-cols-2 lg:grid-cols-3 xl:flex xl:flex-wrap xl:justify-end">
          {options.map((option) => {
            const selected = selectedKeys.includes(option.key);

            return (
              <label
                key={option.key}
                className={`inline-flex min-h-9 cursor-pointer items-center gap-2 rounded-lg border px-3 text-sm font-semibold transition ${
                  selected
                    ? "border-blue-300 bg-blue-50 text-slate-950"
                    : "border-slate-200 bg-white text-slate-500 hover:bg-slate-50 hover:text-slate-900"
                }`}
              >
                <input
                  type="checkbox"
                  checked={selected}
                  onChange={() => onToggle(option.key)}
                  className="h-4 w-4 shrink-0 rounded border-slate-300 text-blue-600 focus:ring-blue-500"
                />
                <span
                  className="h-3 w-3 rounded-sm border border-slate-300"
                  style={{
                    backgroundColor: option.color,
                    backgroundImage: option.key === "idle_time"
                      ? `repeating-linear-gradient(135deg, ${CAPACITY_SPLIT_COLORS.idle_stripe} 0 1px, transparent 1px 5px)`
                      : "none",
                  }}
                />
                {option.label}
              </label>
            );
          })}
        </div>
      </div>
    </section>
  );
}

const MACHINE_COLORS = ["#0057B8", "#007A3D", "#6D28D9", "#008C95", "#C2410C", "#B0006D", "#334155"];

function UtilizationBreakdownChart({
  title,
  subtitle,
  data,
  nameKey,
  selectedStacks,
  barSize,
  rowHeight,
  emptyMessage,
  showPlannedNights,
  patternedUnplanned = false
}) {
  const isTowerChart = nameKey === "tower";
  const effectiveRowHeight = isTowerChart ? Math.max(rowHeight, 44) : rowHeight;
  const chartHeight = Math.max(
    isTowerChart ? 300 : 320,
    data.length * effectiveRowHeight + (isTowerChart ? 52 : 72)
  );
  const yAxisLabelOffset = -6;
  const chartMargin = { top: 8, right: 16, left: 4, bottom: 8 };
  const idlePatternId = `${nameKey}-utilization-idle-pattern`;

  const machineColors = useMemo(() => {
    const machines = [...new Set(
      data.map(row => String(row[nameKey] || "").split("\n")[0]).filter(Boolean)
    )].sort(compareResourceNames);
    return Object.fromEntries(machines.map((m, i) => [m, MACHINE_COLORS[i % MACHINE_COLORS.length]]));
  }, [data, nameKey]);
  const uvTowerNames = useMemo(
    () => new Set(isTowerChart ? data.filter((row) => row.uv_tower).map((row) => row[nameKey]) : []),
    [data, isTowerChart, nameKey]
  );
  const yAxisWidth = useMemo(() => {
    const maxChars = data.reduce((max, row) => {
      const parts = String(row[nameKey] || "").split("\n");
      return Math.max(max, ...parts.map(p => p.length));
    }, 0);
    return Math.max(80, maxChars * 7 + 24);
  }, [data, nameKey]);

  const CustomYAxisTick = (props) => {
    const { x, y, payload } = props;
    const parts = payload.value.split("\n");
    const isUvTower = isTowerChart && uvTowerNames.has(payload.value);

    if (parts.length === 2) {
      const machineColor = machineColors[parts[0]] || "#2563eb";
      return (
        <g transform={`translate(${x},${y})`}>
          <text x={yAxisLabelOffset} y={-7} dy={4} textAnchor="end" fill={machineColor} fontSize={10} fontWeight="800">
            {parts[0]}
          </text>
          <text x={yAxisLabelOffset} y={9} dy={4} textAnchor="end" fontSize={11} fontWeight="500">
            {isUvTower && <tspan fill="#B12C00" fontWeight="800">(UV) </tspan>}
            <tspan fill="#B12C00">{parts[1]}</tspan>
          </text>
        </g>
      );
    }

    return (
      <text
        x={x}
        y={y}
        dy={4}
        textAnchor="end"
        fill="#334155"
        fontSize={11}
      >
        {payload.value}
      </text>
    );
  };

  return (
    <section className="rounded-lg border border-slate-200 bg-white p-3 shadow-soft">
      <div className="mb-3">
        <h2 className="text-base font-semibold text-slate-950">{title}</h2>
        <p className="mt-1 text-sm text-slate-500">{subtitle}</p>
      </div>

      {data.length === 0 ? (
        <div className="flex h-72 items-center justify-center rounded-lg border border-dashed border-slate-200 text-sm text-slate-500">
          {emptyMessage}
        </div>
      ) : (
        <div className={`rounded-lg border border-slate-100 bg-slate-50 ${isTowerChart ? "p-0" : "p-1.5"}`}>
          <div className="relative w-full" style={{ height: chartHeight }}>
            <ResponsiveContainer>
              <BarChart data={data} layout="vertical" margin={chartMargin}>
                <defs>
                  <pattern id={idlePatternId} patternUnits="userSpaceOnUse" width="5" height="5" patternTransform="rotate(35)">
                    <rect width="5" height="5" fill={CAPACITY_SPLIT_COLORS.idle_time} />
                    <line x1="0" y1="0" x2="0" y2="5" stroke={CAPACITY_SPLIT_COLORS.idle_stripe} strokeWidth="1" />
                  </pattern>
                </defs>
                <CartesianGrid strokeDasharray="3 3" stroke="#94a3b8" horizontal={false} />
                <XAxis
                  type="number"
                  domain={[0, 100]}
                  ticks={[0, 25, 50, 75, 100]}
                  allowDecimals={false}
                  tickFormatter={(value) => `${value}%`}
                  tick={{ fill: "#475569", fontSize: 12 }}
                  tickLine={false}
                  axisLine={{ stroke: "#cbd5e1" }}
                />
                <YAxis
                  type="category"
                  dataKey={nameKey}
                  width={yAxisWidth}
                  interval={0}
                  minTickGap={0}
                  tickMargin={6}
                  tick={<CustomYAxisTick />}
                  tickLine={false}
                  axisLine={{ stroke: "#cbd5e1" }}
                />
                <Tooltip content={<UtilizationTooltip nameKey={nameKey} selectedStacks={selectedStacks} showPlannedNights={showPlannedNights} />} cursor={{ fill: "rgba(15, 23, 42, 0.06)" }} />
                {selectedStacks.map((option, index) => (
                  <Bar
                    key={option.key}
                    dataKey={`${option.key}_percentage`}
                    name={option.label}
                    stackId="engaged"
                    fill={option.color}
                    stroke="#f8fafc"
                    strokeWidth={1.25}
                    barSize={barSize}
                    radius={index === selectedStacks.length - 1 ? [0, 4, 4, 0] : [0, 0, 0, 0]}
                    shape={
                      patternedUnplanned && option.key === "idle_time"
                        ? <PatternedUtilizationBar patternId={idlePatternId} />
                        : undefined
                    }
                  />
                ))}
              </BarChart>
            </ResponsiveContainer>
          </div>
        </div>
      )}
    </section>
  );
}

function PatternedUtilizationBar(props) {
  const { x, y, width, height, patternId } = props;
  const barX = Number(x || 0);
  const barY = Number(y || 0);
  const barWidth = Number(width || 0);
  const barHeight = Number(height || 0);

  if (barWidth <= 0 || barHeight <= 0) return null;

  const radius = Math.min(4, barWidth / 2, barHeight / 2);
  const right = barX + barWidth;
  const bottom = barY + barHeight;
  const path = [
    `M ${barX} ${barY}`,
    `H ${right - radius}`,
    `Q ${right} ${barY} ${right} ${barY + radius}`,
    `V ${bottom - radius}`,
    `Q ${right} ${bottom} ${right - radius} ${bottom}`,
    `H ${barX}`,
    "Z"
  ].join(" ");

  return (
    <path
      d={path}
      fill={`url(#${patternId})`}
      stroke="#cbd5e1"
      strokeWidth="1.25"
      vectorEffect="non-scaling-stroke"
    />
  );
}

function ComplexPrintStackIcon({ x, y, color }) {
  const halfWidth = 3.5;
  const topOffset = 3.5;
  const bottomOffset = 3.5;

  return (
    <polygon
      points={`${x},${y - topOffset} ${x + halfWidth},${y + bottomOffset} ${x - halfWidth},${y + bottomOffset}`}
      fill={color}
      opacity="0.95"
      pointerEvents="none"
    />
  );
}

function ComplexPrintMarker({ x, y, side, label, color }) {
  const anchor = side === "left" ? "end" : "start";
  const badgeWidth = 48;
  const triangleCenterX = side === "left" ? x - 8 : x + 8;
  const labelOffset = side === "left" ? -17 : 17;
  const trianglePoints = `${triangleCenterX},${y - 4.2} ${triangleCenterX + 4.2},${y + 3.5} ${triangleCenterX - 4.2},${y + 3.5}`;

  return (
    <g pointerEvents="none">
      <rect
        x={side === "left" ? x - badgeWidth : x}
        y={y - 9}
        width={badgeWidth}
        height="18"
        rx="4"
        fill="#ffffff"
        fillOpacity="0.9"
        stroke="#cbd5e1"
        strokeWidth="0.6"
      />
      <polygon points={trianglePoints} fill={color} opacity="0.95" />
      <text
        x={x + labelOffset}
        y={y + 3}
        textAnchor={anchor}
        fontSize="8.5"
        fontWeight="500"
        fill="#0f172a"
      >
        {label}
      </text>
    </g>
  );
}

function UtilizationTooltip({ active, payload, nameKey, selectedStacks, showPlannedNights }) {
  if (!active || !payload?.length) return null;
  const row = payload[0].payload;
  const nightsLabel = nameKey === "folder" ? "Folder nights:" : "Planned nights:";
  const showIdleTime = selectedStacks.some((stack) => stack.key === "idle_time") && Number(row.idle_time || 0) > 0;
  const lossSubcomponents = [
    ["Reflong time", row.reflong_related_downtime],
    ["Changeover time", row.change_over_time],
    ["LPR to print start", row.late_start_time]
  ];

  return (
    <div className="min-w-[270px] rounded-lg border border-slate-200 bg-white p-3 text-sm shadow-soft">
      <p className="font-semibold text-slate-950">{row[nameKey]}</p>
      <div className="mt-2 space-y-1 text-slate-600">
        <TooltipRow
          label="Wait time"
          value={`${formatMinutes(row.waiting_time)} (${formatPercent(row.waiting_time_percentage)})`}
          color={CAPACITY_SPLIT_COLORS.waiting_time}
        />
        <TooltipRow
          label="Loss time"
          value={`${formatMinutes(row.loss_time)} (${formatPercent(row.loss_time_percentage)})`}
          color={CAPACITY_SPLIT_COLORS.loss_time}
        />
        <div className="space-y-0.5 pl-5 text-xs text-slate-500">
          {lossSubcomponents.map(([label, value]) => (
            <div key={label} className="flex items-center justify-between gap-4">
              <span>{label}</span>
              <span className="font-bold text-slate-700">
                {formatMinutes(value)} ({formatPercent(calculatePercentage(value, row.available_capacity))})
              </span>
            </div>
          ))}
        </div>
        <TooltipRow
          label="Downtime"
          value={`${formatMinutes(row.downtime)} (${formatPercent(row.downtime_percentage)})`}
          color={CAPACITY_SPLIT_COLORS.downtime}
        />
        <TooltipRow
          label="Run Time: SNP"
          value={`${formatMinutes(row.runtime_snp)} (${formatPercent(row.runtime_snp_percentage)})`}
          color={RUNTIME_SEGMENT_STYLES.snp.color}
        />
        {row.runtime_gnp > 0 && (
          <TooltipRow
            label="Run Time: GNP"
            value={`${formatMinutes(row.runtime_gnp)} (${formatPercent(row.runtime_gnp_percentage)})`}
            color={RUNTIME_SEGMENT_STYLES.gnp.color}
          />
        )}
        <TooltipRow
          label="Spare time"
          value={`${formatMinutes(row.spare_time)} (${formatPercent(row.spare_time_percentage)})`}
          color={CAPACITY_SPLIT_COLORS.spare_time}
        />
        {showIdleTime && (
          <TooltipRow
            label="Unplanned time"
            value={`${formatMinutes(row.idle_time)} (${formatPercent(row.idle_time_percentage)})`}
            color={CAPACITY_SPLIT_COLORS.idle_time}
          />
        )}
        {showPlannedNights && (
          <div className="flex justify-between border-t border-slate-200 pt-2 text-slate-700">
            <span>{nightsLabel}</span>
            <span className="font-semibold text-slate-950">{formatNumber(row.planned_nights)}/{formatNumber(row.total_nights)}</span>
          </div>
        )}
      </div>
    </div>
  );
}


function splitResourceLabel(value) {
  const parts = String(value || "")
    .split("\n")
    .map((part) => part.trim())
    .filter(Boolean);

  return [parts[0] || "", parts[1] || parts[0] || ""];
}

function TooltipRow({ label, value, color }) {
  return (
    <div className="flex items-center justify-between gap-4">
      <span className="flex items-center gap-1.5">
        <span
          className="h-2.5 w-2.5 flex-shrink-0 rounded-full"
          style={{ backgroundColor: color }}
        />
        <span>{label}</span>
      </span>
      <span className="font-bold text-slate-950">{value}</span>
    </div>
  );
}

function buildCapacitySplitModel(dailyRows, detailRows, towerDetailRows = []) {
  const days = [...(dailyRows || [])]
    .map((day) => day.run_date)
    .filter(Boolean)
    .sort();
  const folderKeys = Array.from(
    new Set((detailRows || []).map((row) => row.folder).filter(Boolean))
  ).sort(compareResourceNames);
  const folders = folderKeys.map((folderKey, index) => ({
    key: folderKey,
    alias: `F${index + 1}`,
    shortName: getFolderShortName(folderKey),
    color: FOLDER_ALIAS_COLORS[index % FOLDER_ALIAS_COLORS.length]
  }));
  const detailsByDayFolder = new Map(
    (detailRows || []).map((row) => [`${row.run_date}||${row.folder}`, row])
  );
  const rows = [];

  for (const runDate of days) {
    folders.forEach((folder, folderIndex) => {
      const detail = detailsByDayFolder.get(`${runDate}||${folder.key}`);

      if (!detail) {
        const idleValues = {
          waiting_time: 0,
          loss_time: 0,
          downtime: 0,
          runtime: 0,
          spare_time: 0,
          idle_time: CAPACITY_WINDOW_MINUTES
        };

        rows.push({
          run_date: runDate,
          folderKey: folder.key,
          folderIndex,
          isIdle: true,
          twin_folder_mode: false,
          twin_folder_group: "",
          ...idleValues,
          segments: buildCapacitySegments(idleValues)
        });
        return;
      }

      const values = normalizeCapacityValues(detail);

      rows.push({
        run_date: runDate,
        folderKey: folder.key,
        folderIndex,
        isIdle: false,
        twin_folder_mode: Boolean(detail.twin_folder_mode),
        twin_folder_group: detail.twin_folder_group || "",
        ...values,
        segments: buildCapacitySegments(values)
      });
    });
  }

  const folderPlantRows = buildPlantCapacityRows(days, rows, folders.length);
  const towerPlantRows = buildTowerPlantCapacityRows(days, dailyRows, towerDetailRows);

  return {
    days,
    folders,
    rows,
    plantRows: towerPlantRows.length > 0 ? towerPlantRows : folderPlantRows,
    totalTowerCount: calculateCapacityTowerCount(dailyRows, towerDetailRows, folders.length)
  };
}

function buildCapacityPeriodRows(plantRows, { timeframeMode, timeframeRange, zoomedMonth } = {}) {
  const sourceRows = [...(plantRows || [])].sort((a, b) => String(a.run_date || "").localeCompare(String(b.run_date || "")));

  if (sourceRows.length === 0) {
    return { grain: "day", rows: [] };
  }

  if (zoomedMonth) {
    return {
      grain: "day",
      rows: sourceRows
        .filter((row) => getMonthKey(row.run_date) === zoomedMonth)
        .map(asDailyCapacityPeriodRow)
    };
  }

  if (shouldUseDailyCapacityGrain(sourceRows, timeframeMode, timeframeRange)) {
    return { grain: "day", rows: sourceRows.map(asDailyCapacityPeriodRow) };
  }

  return { grain: "month", rows: buildMonthlyCapacityPeriodRows(sourceRows) };
}

function shouldUseDailyCapacityGrain(rows, timeframeMode, timeframeRange) {
  const mode = String(timeframeMode || "").toLowerCase();

  if (mode === "month") return true;
  if (mode !== "custom") return false;

  const start = timeframeRange?.start || rows[0]?.run_date || "";
  const end = timeframeRange?.end || rows[rows.length - 1]?.run_date || "";
  const daySpan = countDaysInclusive(start, end);
  return daySpan > 0 && daySpan <= PLANT_CAPACITY_PAGE_SIZE;
}

function asDailyCapacityPeriodRow(row) {
  return {
    ...row,
    period_key: row.run_date,
    period_type: "day",
    month_key: getMonthKey(row.run_date),
    start_date: row.run_date,
    end_date: row.run_date,
    canZoom: false,
  };
}

function buildMonthlyCapacityPeriodRows(rows) {
  const rowsByMonth = new Map();

  for (const row of rows) {
    const monthKey = getMonthKey(row.run_date);
    if (!monthKey) continue;
    const monthRows = rowsByMonth.get(monthKey) || [];
    monthRows.push(row);
    rowsByMonth.set(monthKey, monthRows);
  }

  return Array.from(rowsByMonth.entries())
    .sort(([first], [second]) => first.localeCompare(second))
    .map(([monthKey, monthRows]) => buildMonthlyCapacityPeriodRow(monthKey, monthRows));
}

function buildMonthlyCapacityPeriodRow(monthKey, monthRows) {
  const sortedRows = [...monthRows].sort((a, b) => String(a.run_date || "").localeCompare(String(b.run_date || "")));
  const totalCapacity = sumCapacityRows(sortedRows, "total_capacity");
  const values = normalizePlantCapacityValues({
    waiting_time: sumCapacityRows(sortedRows, "waiting_time"),
    loss_time: sumCapacityRows(sortedRows, "loss_time"),
    downtime: sumCapacityRows(sortedRows, "downtime"),
    runtime: sumCapacityRows(sortedRows, "runtime"),
    spare_time: sumCapacityRows(sortedRows, "spare_time"),
    idle_time: sumCapacityRows(sortedRows, "idle_time")
  }, totalCapacity);
  const runtimeSegments = aggregatePlantRuntimeSegments(sortedRows, values.runtime, values.downtime);

  return {
    run_date: monthKey,
    period_key: monthKey,
    period_type: "month",
    month_key: monthKey,
    start_date: sortedRows[0]?.run_date || "",
    end_date: sortedRows[sortedRows.length - 1]?.run_date || "",
    folderKey: "Plant",
    folderIndex: 0,
    isIdle: false,
    isPlant: true,
    twin_folder_mode: false,
    twin_folder_group: "",
    activeTowers: sumCapacityRows(sortedRows, "activeTowers"),
    totalTowers: sumCapacityRows(sortedRows, "totalTowers"),
    activeFolders: sumCapacityRows(sortedRows, "activeFolders"),
    totalFolders: sumCapacityRows(sortedRows, "totalFolders"),
    total_capacity: totalCapacity,
    ...values,
    runtime_segments: runtimeSegments,
    segments: buildCapacitySegments({ ...values, runtime_segments: runtimeSegments }, totalCapacity),
    canZoom: true,
  };
}

function buildTowerPlantCapacityRows(days, dailyRows, towerDetailRows = []) {
  const dailyByDate = new Map((dailyRows || []).map((row) => [row.run_date, row]));
  const towerRowsByDate = new Map();

  for (const row of towerDetailRows || []) {
    if (!row.run_date) continue;
    const rows = towerRowsByDate.get(row.run_date) || [];
    rows.push(row);
    towerRowsByDate.set(row.run_date, rows);
  }

  return days
    .map((runDate) => {
      const daily = dailyByDate.get(runDate);
      if (!daily) return null;

      const totalCapacity = Math.max(Number(daily.available_capacity || 0), 0);
      if (totalCapacity <= 0) return null;

      const values = normalizePlantCapacityValues({
        waiting_time: daily.waiting_time,
        loss_time: daily.lost_time,
        downtime: daily.downtime,
        runtime: daily.runtime,
        spare_time: daily.buffer_time,
        idle_time: daily.idle_time
      }, totalCapacity);
      const runtimeSegments = Array.isArray(daily.runtime_segments) && daily.runtime_segments.length > 0
        ? normalizeRuntimeSegments(daily.runtime_segments, values.runtime, totalCapacity, values.downtime)
        : aggregateRuntimeSegmentsFromSourceRows(towerRowsByDate.get(runDate) || [], values.runtime, totalCapacity, values.downtime);
      const totalTowers = Math.max(
        Number(daily.capacity_towers_count || 0),
        Math.ceil(totalCapacity / CAPACITY_WINDOW_MINUTES),
        1
      );

      return {
        run_date: runDate,
        folderKey: "Plant",
        folderIndex: 0,
        isIdle: false,
        isPlant: true,
        twin_folder_mode: false,
        twin_folder_group: "",
        activeTowers: Number(daily.active_towers_count || 0),
        totalTowers,
        activeFolders: Number(daily.active_folders_count || 0),
        totalFolders: totalTowers,
        total_capacity: totalCapacity,
        ...values,
        runtime_segments: runtimeSegments,
        segments: buildCapacitySegments({ ...values, runtime_segments: runtimeSegments }, totalCapacity)
      };
    })
    .filter(Boolean);
}

function buildPlantCapacityRows(days, rows, folderCount) {
  const rowsByDay = new Map();

  for (const row of rows) {
    const dayRows = rowsByDay.get(row.run_date) || [];
    dayRows.push(row);
    rowsByDay.set(row.run_date, dayRows);
  }

  return days.map((runDate) => {
    const dayRows = rowsByDay.get(runDate) || [];
    const totalFolders = Math.max(folderCount, dayRows.length, 1);
    const totalCapacity = totalFolders * CAPACITY_WINDOW_MINUTES;
    const values = normalizePlantCapacityValues({
      waiting_time: sumCapacityRows(dayRows, "waiting_time"),
      loss_time: sumCapacityRows(dayRows, "loss_time"),
      downtime: sumCapacityRows(dayRows, "downtime"),
      runtime: sumCapacityRows(dayRows, "runtime"),
      spare_time: sumCapacityRows(dayRows, "spare_time"),
      idle_time: sumCapacityRows(dayRows, "idle_time")
    }, totalCapacity);
    const runtimeSegments = aggregatePlantRuntimeSegments(dayRows, values.runtime, values.downtime);

    return {
      run_date: runDate,
      folderKey: "Plant",
      folderIndex: 0,
      isIdle: false,
      isPlant: true,
      twin_folder_mode: false,
      twin_folder_group: "",
      activeFolders: dayRows.filter((row) => !row.isIdle).length,
      totalFolders,
      total_capacity: totalCapacity,
      ...values,
      runtime_segments: runtimeSegments,
      segments: buildCapacitySegments({ ...values, runtime_segments: runtimeSegments }, totalCapacity)
    };
  });
}

function normalizePlantCapacityValues(values, totalCapacity) {
  const capacity = Math.max(Number(totalCapacity || 0), 0);
  const normalized = {
    waiting_time: cleanNumber(Math.max(Number(values.waiting_time || 0), 0)),
    loss_time: cleanNumber(Math.max(Number(values.loss_time || 0), 0)),
    downtime: cleanNumber(Math.max(Number(values.downtime || 0), 0)),
    runtime: cleanNumber(Math.max(Number(values.runtime || 0), 0)),
    spare_time: cleanNumber(Math.max(Number(values.spare_time || 0), 0)),
    idle_time: cleanNumber(Math.max(Number(values.idle_time || 0), 0))
  };

  if (capacity <= 0) return normalized;

  let total = cleanNumber(Object.values(normalized).reduce((sum, value) => sum + value, 0));

  if (total < capacity) {
    normalized.spare_time = cleanNumber(normalized.spare_time + capacity - total);
    return normalized;
  }

  if (total <= capacity) return normalized;

  let overage = cleanNumber(total - capacity);

  for (const key of ["idle_time", "spare_time", "runtime", "downtime", "loss_time", "waiting_time"]) {
    if (overage <= 0) break;

    const reduction = Math.min(normalized[key], overage);
    normalized[key] = cleanNumber(normalized[key] - reduction);
    overage = cleanNumber(overage - reduction);
  }

  return normalized;
}

function aggregatePlantRuntimeSegments(dayRows, targetRuntime, downtime = 0) {
  const buckets = new Map();

  for (const row of dayRows) {
    for (const segment of row.segments || []) {
      if (!segment.runtimeSegment || Number(segment.value || 0) <= 0) continue;

      const key = getRuntimeBucketKey(segment);
      const bucket = buckets.get(key) || {
        key,
        label: RUNTIME_SEGMENT_STYLES[key]?.label || "Run Time",
        minutes: 0,
        is_complex: Boolean(segment.isComplex),
        print_order: 0,
        source_print_order: 0,
        committed_speed_weighted_total: 0,
        committed_speed_weight_minutes: 0,
        complexity_code: segment.complexity_code || ""
      };

      const minutes = Number(segment.value || 0);
      bucket.minutes = cleanNumber(bucket.minutes + minutes);
      bucket.is_complex = bucket.is_complex || Boolean(segment.isComplex);
      bucket.print_order = cleanNumber(Number(bucket.print_order || 0) + Number(segment.print_order || 0));
      bucket.source_print_order = cleanNumber(Number(bucket.source_print_order || 0) + Number(segment.source_print_order || 0));
      if (Number(segment.committed_speed || 0) > 0 && minutes > 0) {
        bucket.committed_speed_weighted_total += Number(segment.committed_speed || 0) * minutes;
        bucket.committed_speed_weight_minutes += minutes;
      }
      if (!bucket.complexity_code && segment.complexity_code) bucket.complexity_code = segment.complexity_code;
      buckets.set(key, bucket);
    }
  }

  const orderedKeys = ["snp", "snp_complex", "gnp", "gnp_complex", "unknown"];
  const segments = orderedKeys
    .filter((key) => buckets.has(key))
    .map((key) => buckets.get(key));
  const totalMinutes = cleanNumber(segments.reduce((sum, segment) => sum + Number(segment.minutes || 0), 0));

  if (totalMinutes <= 0 || Number(targetRuntime || 0) <= 0) return [];

  const scale = Number(targetRuntime || 0) / totalMinutes;
  return finalizeRuntimeSegmentMetrics(segments.map((segment) => ({
    ...segment,
    minutes: cleanNumber(segment.minutes * scale),
    print_order: cleanNumber(Number(segment.print_order || 0) * scale),
    source_print_order: cleanNumber(Number(segment.source_print_order || 0) * scale),
    committed_speed: calculateWeightedSpeed(segment.committed_speed_weighted_total, segment.committed_speed_weight_minutes)
  })), downtime);
}

function aggregateRuntimeSegmentsFromSourceRows(rows, targetRuntime, capacityLimit = CAPACITY_WINDOW_MINUTES, downtime = 0) {
  const runtime = Math.min(
    Math.max(Number(targetRuntime || 0), 0),
    Math.max(Number(capacityLimit || CAPACITY_WINDOW_MINUTES), 0)
  );
  if (runtime <= 0 || !Array.isArray(rows) || rows.length === 0) return [];

  const buckets = new Map();

  for (const row of rows) {
    for (const segment of row.runtime_segments || []) {
      const minutes = Math.max(Number(segment.minutes || 0), 0);
      if (minutes <= 0) continue;

      const key = rawRuntimeSegmentBucketKey(segment);
      const bucket = buckets.get(key) || {
        key,
        label: segment.label || RUNTIME_SEGMENT_STYLES[key]?.label || "Run Time",
        minutes: 0,
        is_complex: Boolean(segment.is_complex),
        effective_speed: 0,
        actual_speed: 0,
        committed_speed: 0,
        speed_efficiency: 0,
        print_order: 0,
        source_print_order: 0,
        committed_speed_weighted_total: 0,
        committed_speed_weight_minutes: 0,
        complexity_code: segment.complexity_code || "",
      };

      bucket.minutes = cleanNumber(bucket.minutes + minutes);
      bucket.is_complex = bucket.is_complex || Boolean(segment.is_complex);
      bucket.print_order = cleanNumber(Number(bucket.print_order || 0) + Number(segment.print_order || 0));
      bucket.source_print_order = cleanNumber(Number(bucket.source_print_order || 0) + Number(segment.source_print_order || 0));
      if (Number(segment.committed_speed || 0) > 0) {
        bucket.committed_speed_weighted_total += Number(segment.committed_speed || 0) * minutes;
        bucket.committed_speed_weight_minutes += minutes;
      }
      if (!bucket.complexity_code && segment.complexity_code) bucket.complexity_code = segment.complexity_code;
      buckets.set(key, bucket);
    }
  }

  const orderedKeys = ["snp", "snp_complex", "gnp", "gnp_complex", "unknown"];
  const segments = orderedKeys
    .filter((key) => buckets.has(key))
    .map((key) => buckets.get(key));
  const totalMinutes = cleanNumber(segments.reduce((sum, segment) => sum + Number(segment.minutes || 0), 0));

  if (totalMinutes <= 0) return [];

  const scale = runtime / totalMinutes;
  return finalizeRuntimeSegmentMetrics(segments.map((segment) => ({
    ...segment,
    minutes: cleanNumber(segment.minutes * scale),
    print_order: cleanNumber(Number(segment.print_order || 0) * scale),
    source_print_order: cleanNumber(Number(segment.source_print_order || 0) * scale),
    committed_speed: calculateWeightedSpeed(segment.committed_speed_weighted_total, segment.committed_speed_weight_minutes)
  })), downtime);
}

function rawRuntimeSegmentBucketKey(segment) {
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

function getRuntimeBucketKey(segment) {
  const text = `${segment.key || ""} ${segment.label || ""}`.toLowerCase();
  const runtimeType = text.includes("snp")
    ? "snp"
    : text.includes("gnp")
      ? "gnp"
      : "unknown";

  if (runtimeType === "unknown") return runtimeType;
  return segment.isComplex ? `${runtimeType}_complex` : runtimeType;
}

function buildTwinFolderMarkers(rows, visibleDays) {
  if (!rows.length || !visibleDays.length) return [];

  const visibleDaySet = new Set(visibleDays);
  const grouped = new Map();

  for (const row of rows) {
    if (
      row.isIdle
      || !visibleDaySet.has(row.run_date)
      || !row.twin_folder_mode
      || !row.twin_folder_group
    ) {
      continue;
    }

    const key = `${row.run_date}||${row.twin_folder_group}`;
    const marker = grouped.get(key) || {
      run_date: row.run_date,
      twinGroup: row.twin_folder_group,
      folderIndexes: new Set(),
    };

    marker.folderIndexes.add(row.folderIndex);
    grouped.set(key, marker);
  }

  return Array.from(grouped.values())
    .map((marker) => {
      const folderIndexes = Array.from(marker.folderIndexes).sort((a, b) => a - b);

      return {
        ...marker,
        startFolderIndex: folderIndexes[0],
        endFolderIndex: folderIndexes[folderIndexes.length - 1],
        folderCount: folderIndexes.length,
      };
    })
    .filter((marker) => marker.folderCount >= 2 && marker.startFolderIndex !== marker.endFolderIndex)
    .sort((a, b) => {
      const dayDifference = visibleDays.indexOf(a.run_date) - visibleDays.indexOf(b.run_date);
      if (dayDifference !== 0) return dayDifference;
      return a.startFolderIndex - b.startFolderIndex;
    });
}

function buildCapacityDaySummary(selectedDay, rows, totalFolders) {
  if (!selectedDay) return null;

  const dayRows = rows.filter((row) => row.run_date === selectedDay);
  if (!dayRows.length) return null;

  const activeRows = dayRows.filter((row) => !row.isIdle);
  const folderCount = Math.max(totalFolders, dayRows.length);
  const totalCapacity = folderCount * CAPACITY_WINDOW_MINUTES;
  const runtime = sumCapacityRows(activeRows, "runtime");
  const waitingTime = sumCapacityRows(activeRows, "waiting_time");
  const lossTime = sumCapacityRows(activeRows, "loss_time");
  const downtime = sumCapacityRows(activeRows, "downtime");
  const spareTime = sumCapacityRows(activeRows, "spare_time");
  const idleTime = sumCapacityRows(dayRows, "idle_time");

  return {
    run_date: selectedDay,
    dayLabel: formatDayLabel(selectedDay),
    capacityUnitLabel: "Active folders",
    activeUnits: activeRows.length,
    totalUnits: folderCount,
    activeFolders: activeRows.length,
    totalFolders: folderCount,
    totalCapacity,
    utilization: cleanNumber(runtime + lossTime + downtime),
    runtimeDetails: buildRuntimeTooltipDetails(activeRows),
    components: [
      {
        key: "waiting_time",
        label: "Wait Time",
        value: waitingTime,
        color: CAPACITY_SPLIT_COLORS.waiting_time
      },
      {
        key: "loss_time",
        label: "Loss Time",
        value: lossTime,
        color: CAPACITY_SPLIT_COLORS.loss_time
      },
      {
        key: "downtime",
        label: "Downtime",
        value: downtime,
        color: CAPACITY_SPLIT_COLORS.downtime
      },
      {
        key: "runtime",
        label: "Run Time",
        value: runtime,
        color: CAPACITY_SPLIT_COLORS.runtime
      },
      {
        key: "spare_time",
        label: "Spare time",
        value: spareTime,
        color: CAPACITY_SPLIT_COLORS.spare_time
      },
      {
        key: "idle_time",
        label: "Unplanned time",
        value: idleTime,
        color: CAPACITY_SPLIT_COLORS.idle_time
      }
    ]
  };
}

function buildPlantCapacityPeriodSummary(selectedPeriod, periodRows) {
  if (!selectedPeriod) return null;

  const row = (periodRows || []).find((candidate) => candidate.period_key === selectedPeriod || candidate.run_date === selectedPeriod);
  if (!row) return null;

  const isMonth = row.period_type === "month";
  const totalCapacity = Math.max(Number(row.total_capacity || 0), 0);
  const runtime = Number(row.runtime || 0);
  const waitingTime = Number(row.waiting_time || 0);
  const lossTime = Number(row.loss_time || 0);
  const downtime = Number(row.downtime || 0);
  const spareTime = Number(row.spare_time || 0);
  const idleTime = Number(row.idle_time || 0);

  return {
    run_date: row.run_date,
    dayLabel: isMonth ? formatMonthYearLabel(row.month_key) : formatDayLabel(row.run_date),
    capacityUnitLabel: isMonth ? "Active tower-days" : "Active towers",
    activeUnits: Number(row.activeTowers || 0),
    totalUnits: Number(row.totalTowers || 0),
    activeFolders: Number(row.activeTowers || 0),
    totalFolders: Number(row.totalTowers || 0),
    totalCapacity,
    canZoom: Boolean(row.canZoom),
    zoomMonthKey: row.month_key || "",
    utilization: cleanNumber(runtime + lossTime + downtime),
    runtimeDetails: buildPlantRuntimeTooltipDetails(row),
    components: [
      {
        key: "waiting_time",
        label: "Wait Time",
        value: waitingTime,
        color: CAPACITY_SPLIT_COLORS.waiting_time
      },
      {
        key: "loss_time",
        label: "Loss Time",
        value: lossTime,
        color: CAPACITY_SPLIT_COLORS.loss_time
      },
      {
        key: "downtime",
        label: "Downtime",
        value: downtime,
        color: CAPACITY_SPLIT_COLORS.downtime
      },
      {
        key: "runtime",
        label: "Run Time",
        value: runtime,
        color: CAPACITY_SPLIT_COLORS.runtime
      },
      {
        key: "spare_time",
        label: "Spare time",
        value: spareTime,
        color: CAPACITY_SPLIT_COLORS.spare_time
      },
      {
        key: "idle_time",
        label: "Unplanned time",
        value: idleTime,
        color: CAPACITY_SPLIT_COLORS.idle_time
      }
    ]
  };
}

function buildPlantRuntimeTooltipDetails(row) {
  return (row.segments || [])
    .filter((segment) => segment.runtimeSegment && Number(segment.value || 0) > 0)
    .map((segment) => ({
      key: `${row.run_date}||Plant||${segment.key}`,
      folderAlias: formatCapacitySegmentLabel(segment),
      minutes: segment.value,
      color: segment.color || CAPACITY_SPLIT_COLORS.runtime,
      detailText: formatRuntimeSegmentDetail(segment) || formatPercent(calculatePercentage(segment.value, row.total_capacity))
    }));
}

function buildRuntimeTooltipDetails(rows) {
  const details = [];

  for (const row of rows) {
    for (const segment of row.segments || []) {
      if (!segment.runtimeSegment || Number(segment.value || 0) <= 0) continue;

      const detailText = formatRuntimeSegmentDetail(segment, row.twin_folder_mode);
      if (!detailText) continue;

      const folderAlias = `F${Number(row.folderIndex || 0) + 1}`;

      details.push({
        key: `${row.run_date}||${row.folderKey}||${segment.key}`,
        folderAlias: `${folderAlias}: ${formatCapacitySegmentLabel(segment)}`,
        minutes: segment.value,
        color: segment.color || CAPACITY_SPLIT_COLORS.runtime,
        detailText,
      });
    }
  }

  return details;
}

function sumCapacityRows(rows, key) {
  return cleanNumber(rows.reduce((total, row) => total + Number(row[key] || 0), 0));
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

function getSegmentFill(segment) {
  if (segment.fill) return segment.fill;
  if (segment.key === "idle_time") return "url(#idlePattern)";
  return segment.color;
}

function layoutExternalComplexMarkers(layouts, { x, barWidth, plotTop, plotBottom, chartRight }) {
  if (!layouts.length) return [];

  const markerGap = 16;
  const useLeftSide = x + barWidth + 48 > chartRight;
  let lastY = plotTop - markerGap;
  const positioned = layouts
    .map((layout, index) => ({
      index,
      segment: layout.segment,
      targetY: layout.y + layout.segmentHeight / 2,
    }))
    .sort((a, b) => a.targetY - b.targetY)
    .map((marker) => {
      const y = Math.min(
        Math.max(marker.targetY, lastY + markerGap, plotTop + 8),
        plotBottom - 8
      );
      lastY = y;
      return {
        ...marker,
        y,
        x: useLeftSide ? x - 6 : x + barWidth + 6,
        side: useLeftSide ? "left" : "right",
      };
    });
  const overflow = positioned.length > 0 ? positioned[positioned.length - 1].y - (plotBottom - 8) : 0;

  if (overflow > 0) {
    return positioned.map((marker) => ({
      ...marker,
      y: Math.max(plotTop + 8, marker.y - overflow),
    }));
  }

  return positioned;
}

function formatCapacitySegmentTitle({ segment, rowCapacity, folderAlias, isPlantView }) {
  const label = formatCapacitySegmentLabel(segment);

  if (isPlantView) {
    return `${label} ${formatPercent(calculatePercentage(segment.value, rowCapacity))} (${formatMinutes(segment.value)})`;
  }

  return `${folderAlias}: ${label} ${formatMinutes(segment.value)}`;
}

function formatCapacitySegmentLabel(segment) {
  if (!segment?.runtimeSegment) return segment?.label || "";
  return segment.label || runtimeSegmentLabel(getRuntimeBucketKey(segment)) || "Run Time";
}

function buildCapacitySegments(values, capacityLimit = CAPACITY_WINDOW_MINUTES) {
  return [
    {
      key: "waiting_time",
      label: "Wait Time",
      value: values.waiting_time,
      color: CAPACITY_SPLIT_COLORS.waiting_time
    },
    {
      key: "loss_time",
      label: "Loss Time",
      value: values.loss_time,
      color: CAPACITY_SPLIT_COLORS.loss_time
    },
    {
      key: "downtime",
      label: "Downtime",
      value: values.downtime,
      color: CAPACITY_SPLIT_COLORS.downtime
    },
    ...buildRuntimeCapacitySegments(values.runtime_segments, values.runtime, capacityLimit, values.downtime),
    {
      key: "spare_time",
      label: "Spare Time",
      value: values.spare_time,
      color: CAPACITY_SPLIT_COLORS.spare_time
    },
    {
      key: "idle_time",
      label: "Unplanned Time",
      value: values.idle_time,
      color: CAPACITY_SPLIT_COLORS.idle_time
    }
  ];
}

function buildRuntimeCapacitySegments(runtimeSegments, fallbackRuntime, capacityLimit = CAPACITY_WINDOW_MINUTES, downtime = 0) {
  const normalizedSegments = normalizeRuntimeSegments(runtimeSegments, fallbackRuntime, capacityLimit, downtime);

  if (normalizedSegments.length === 0) {
    return [
      {
        key: "runtime",
        label: "Run Time",
        value: fallbackRuntime,
        color: CAPACITY_SPLIT_COLORS.runtime,
        runtimeSegment: true,
        textColor: "#14532d",
        effective_speed: 0,
        actual_speed: 0,
        committed_speed: 0,
        speed_efficiency: 0
      }
    ];
  }

  return normalizedSegments.map((segment, index) => {
    const style = RUNTIME_SEGMENT_STYLES[segment.key] || RUNTIME_SEGMENT_STYLES.unknown;

    return {
      key: `runtime_${segment.key}_${index}`,
      label: segment.label || style.label,
      value: segment.minutes,
      color: style.color,
      fill: style.color,
      runtimeSegment: true,
      isComplex: Boolean(style.isComplex || segment.is_complex),
      textColor: style.textColor,
      effective_speed: segment.effective_speed,
      actual_speed: segment.actual_speed,
      committed_speed: segment.committed_speed,
      speed_efficiency: segment.speed_efficiency,
      print_order: segment.print_order,
      source_print_order: segment.source_print_order,
      complexity_code: segment.complexity_code
    };
  });
}

function normalizeRuntimeSegments(runtimeSegments, targetRuntime, capacityLimit = CAPACITY_WINDOW_MINUTES, downtime = 0) {
  const runtime = cleanNumber(Math.min(
    Math.max(Number(targetRuntime || 0), 0),
    Math.max(Number(capacityLimit || CAPACITY_WINDOW_MINUTES), 0)
  ));
  if (runtime <= 0 || !Array.isArray(runtimeSegments) || runtimeSegments.length === 0) {
    return [];
  }

  const positiveSegments = runtimeSegments
    .map((segment) => ({
      ...segment,
      minutes: cleanNumber(Math.max(Number(segment.minutes || 0), 0))
    }))
    .filter((segment) => segment.minutes > 0);
  const totalMinutes = cleanNumber(positiveSegments.reduce((total, segment) => total + segment.minutes, 0));

  if (totalMinutes <= 0) return [];

  const scale = runtime / totalMinutes;
  let remaining = runtime;

  const normalized = positiveSegments.map((segment, index) => {
    const nextSegment = { ...segment };

    if (index === positiveSegments.length - 1) {
      nextSegment.minutes = cleanNumber(Math.max(remaining, 0));
      nextSegment.print_order = cleanNumber(Number(segment.print_order || 0) * scale);
      nextSegment.source_print_order = cleanNumber(Number(segment.source_print_order || 0) * scale);
      return nextSegment;
    }

    nextSegment.minutes = cleanNumber(Math.min(segment.minutes * scale, remaining));
    nextSegment.print_order = cleanNumber(Number(segment.print_order || 0) * scale);
    nextSegment.source_print_order = cleanNumber(Number(segment.source_print_order || 0) * scale);
    remaining = cleanNumber(remaining - nextSegment.minutes);
    return nextSegment;
  }).filter((segment) => segment.minutes > 0);

  return finalizeRuntimeSegmentMetrics(normalized, downtime);
}

function normalizeCapacityValues(detail) {
  const waitingTime = clampMinutes(detail.waiting_time);
  const lossTime = clampMinutes(detail.lost_time);
  const downtime = clampMinutes(detail.downtime);
  const runtime = clampMinutes(detail.runtime);
  const nonSpareValues = {
    waiting_time: waitingTime,
    loss_time: lossTime,
    downtime,
    runtime
  };
  const nonSpareTotal = Object.values(nonSpareValues).reduce((total, value) => total + value, 0);
  const hasProvidedSpare = Number.isFinite(Number(detail.buffer_time));
  const hasProvidedIdle = Number.isFinite(Number(detail.idle_time));
  const idleTime = hasProvidedIdle ? clampMinutes(detail.idle_time) : 0;
  const spareTime = hasProvidedSpare
    ? clampMinutes(detail.buffer_time)
    : cleanNumber(Math.max(CAPACITY_WINDOW_MINUTES - nonSpareTotal - idleTime, 0));
  const values = {
    ...nonSpareValues,
    spare_time: spareTime,
    idle_time: idleTime,
    runtime_segments: normalizeRuntimeSegments(detail.runtime_segments, runtime, CAPACITY_WINDOW_MINUTES, downtime)
  };
  const total = cleanNumber(nonSpareTotal + spareTime + idleTime);

  if (total <= CAPACITY_WINDOW_MINUTES) {
    return values;
  }

  let overage = cleanNumber(total - CAPACITY_WINDOW_MINUTES);
  const normalized = { ...values };

  for (const key of ["spare_time", "idle_time", "runtime", "downtime", "loss_time", "waiting_time"]) {
    if (overage <= 0) break;

    const reduction = Math.min(normalized[key], overage);
    normalized[key] = cleanNumber(normalized[key] - reduction);
    overage = cleanNumber(overage - reduction);
  }

  normalized.runtime_segments = normalizeRuntimeSegments(normalized.runtime_segments, normalized.runtime, CAPACITY_WINDOW_MINUTES, normalized.downtime);

  return normalized;
}

function clampMinutes(value) {
  return Math.min(Math.max(Number(value || 0), 0), CAPACITY_WINDOW_MINUTES);
}

function getFolderShortName(folderKey) {
  const parts = String(folderKey || "")
    .split("\n")
    .map((part) => part.trim())
    .filter(Boolean);

  return parts[1] || parts[0] || "";
}

function formatHourTick(minutes) {
  const hours = Math.floor(Number(minutes || 0) / 60);
  const minutePart = String(Math.round(Number(minutes || 0) % 60)).padStart(2, "0");
  return `${String(hours).padStart(2, "0")}:${minutePart}`;
}

function buildPlantCapacityTicks(maxCapacity) {
  const capacity = Math.max(Number(maxCapacity || 0), CAPACITY_WINDOW_MINUTES);
  const folderUnits = Math.max(1, Math.ceil(capacity / CAPACITY_WINDOW_MINUTES));
  const folderStep = Math.max(1, Math.ceil(folderUnits / 5));
  const step = folderStep * CAPACITY_WINDOW_MINUTES;
  const ticks = [];

  for (let tick = 0; tick <= capacity; tick += step) {
    ticks.push(tick);
  }

  if (ticks[ticks.length - 1] !== capacity) {
    ticks.push(capacity);
  }

  return ticks;
}

function formatPlantCapacityTick(minutes) {
  return formatMinutes(minutes);
}

function formatDayLabel(dateStr) {
  const date = new Date(dateStr);
  return date.toLocaleDateString("en-US", { day: "2-digit", month: "short" });
}

function formatDayAxisLabel(dateStr) {
  const match = /^(\d{4})-(\d{2})-(\d{2})$/.exec(String(dateStr || ""));
  if (!match) return { day: String(dateStr || ""), weekday: "", month: "", isWeekend: false };

  const year = Number(match[1]);
  const month = Number(match[2]);
  const date = new Date(year, month - 1, Number(match[3]));
  const weekday = date.toLocaleDateString("en-US", { weekday: "short" });
  return {
    day: match[3],
    weekday,
    month: new Date(year, month - 1, 1).toLocaleDateString("en-US", { month: "short" }),
    isWeekend: weekday === "Sat" || weekday === "Sun"
  };
}

function formatCapacityAxisLabel(value, grain) {
  if (grain === "month") {
    const monthParts = parseMonthKey(value);
    if (!monthParts) return { day: String(value || ""), month: "" };

    return {
      day: new Date(monthParts.year, monthParts.month - 1, 1).toLocaleDateString("en-US", { month: "short" }),
      weekday: "",
      month: String(monthParts.year),
      isWeekend: false
    };
  }

  return formatDayAxisLabel(value);
}

function getMonthKey(dateStr) {
  const match = /^(\d{4})-(\d{2})/.exec(String(dateStr || ""));
  return match ? `${match[1]}-${match[2]}` : "";
}

function parseMonthKey(value) {
  const match = /^(\d{4})-(\d{2})$/.exec(String(value || ""));
  if (!match) return null;

  const year = Number(match[1]);
  const month = Number(match[2]);
  if (!Number.isFinite(year) || !Number.isFinite(month) || month < 1 || month > 12) return null;

  return { year, month };
}

function parseDateParts(value) {
  const match = /^(\d{4})-(\d{2})-(\d{2})$/.exec(String(value || ""));
  if (!match) return null;

  const year = Number(match[1]);
  const month = Number(match[2]);
  const day = Number(match[3]);
  if (!Number.isFinite(year) || !Number.isFinite(month) || !Number.isFinite(day)) return null;

  return { year, month, day };
}

function countDaysInclusive(start, end) {
  const startParts = parseDateParts(start);
  const endParts = parseDateParts(end);
  if (!startParts || !endParts) return 0;

  const startDate = Date.UTC(startParts.year, startParts.month - 1, startParts.day);
  const endDate = Date.UTC(endParts.year, endParts.month - 1, endParts.day);
  const first = Math.min(startDate, endDate);
  const last = Math.max(startDate, endDate);

  return Math.floor((last - first) / 86400000) + 1;
}

function formatMonthYearLabel(monthKey) {
  const monthParts = parseMonthKey(monthKey);
  if (!monthParts) return String(monthKey || "");

  return new Date(monthParts.year, monthParts.month - 1, 1).toLocaleDateString("en-US", {
    month: "long",
    year: "numeric"
  });
}

function formatCapacityReturnViewLabel(timeframeMode, timeframeRange) {
  const fullLabel = String(timeframeRange?.label || "").trim();
  if (fullLabel && fullLabel.length <= 28) return fullLabel;

  const mode = String(timeframeMode || "").toLowerCase();
  if (mode === "annual") return "annual view";
  if (mode === "half") return "half-year view";
  if (mode === "quarter") return "quarter view";
  if (mode === "custom") return "custom view";
  if (mode === "month") return "month view";
  return "selected view";
}

function formatCapacityReturnViewTitle(timeframeMode, timeframeRange) {
  const fullLabel = String(timeframeRange?.label || "").trim();
  if (fullLabel) return fullLabel;
  return formatCapacityReturnViewLabel(timeframeMode, timeframeRange);
}

function normalizeResourceBreakdownValues(row) {
  const fallbackLostTime = (
    Number(row.change_over_time || 0)
    + Number(row.reflong_related_downtime || 0)
    + Number(row.late_start_time || 0)
  );
  const normalized = normalizeCapacityValues({
    ...row,
    lost_time: Number(row.lost_time || 0) > 0 ? row.lost_time : fallbackLostTime
  });
  const runtimeBuckets = calculateRuntimeTypeBuckets({
    ...row,
    runtime: normalized.runtime,
    runtime_segments: normalized.runtime_segments
  });
  const nonWaitingLoss = scaleLossSubcomponents(
    {
      change_over_time: row.change_over_time,
      reflong_related_downtime: row.reflong_related_downtime,
      late_start_time: row.late_start_time
    },
    normalized.loss_time
  );

  return {
    waiting_time: normalized.waiting_time,
    loss_time: normalized.loss_time,
    downtime: normalized.downtime,
    spare_time: normalized.spare_time,
    idle_time: normalized.idle_time,
    runtime_snp: runtimeBuckets.runtime_snp,
    runtime_gnp: runtimeBuckets.runtime_gnp,
    change_over_time: nonWaitingLoss.change_over_time,
    reflong_related_downtime: nonWaitingLoss.reflong_related_downtime,
    late_start_time: nonWaitingLoss.late_start_time
  };
}

function buildTowerBreakdownSourceRows(towerRows, focusedDay) {
  const rows = Array.isArray(towerRows) ? towerRows : [];
  if (!focusedDay) return rows;

  const towers = new Map();
  for (const row of rows) {
    if (!row.tower) continue;
    const current = towers.get(row.tower) || {
      tower: row.tower,
      uv_tower: false,
    };
    current.uv_tower = current.uv_tower || Boolean(row.uv_tower);
    towers.set(row.tower, current);
  }

  const rowsForDay = rows.filter((row) => row.run_date === focusedDay);
  const towersSeenOnDay = new Set(rowsForDay.map((row) => row.tower).filter(Boolean));
  const idleTowerRows = Array.from(towers.values())
    .filter((tower) => !towersSeenOnDay.has(tower.tower))
    .map((tower) => ({
      run_date: focusedDay,
      tower: tower.tower,
      uv_tower: tower.uv_tower,
      waiting_time: 0,
      lost_time: 0,
      downtime: 0,
      runtime: 0,
      buffer_time: 0,
      idle_time: CAPACITY_WINDOW_MINUTES,
      available_capacity: CAPACITY_WINDOW_MINUTES,
      runtime_segments: [],
    }));

  return [...rowsForDay, ...idleTowerRows];
}

function aggregateResourceCapacitySplit(rows, nameKey, selectedProductionDays) {
  if (!selectedProductionDays || rows.length === 0) return [];

  const grouped = new Map();

  for (const row of rows) {
    const name = row[nameKey];
    if (!name) continue;
    const rowValues = normalizeResourceBreakdownValues(row);

    const current = grouped.get(name) || {
      [nameKey]: name,
      runtime_snp: 0,
      runtime_gnp: 0,
      downtime: 0,
      waiting_time: 0,
      loss_time: 0,
      idle_time: 0,
      change_over_time: 0,
      reflong_related_downtime: 0,
      late_start_time: 0,
      uv_tower: false,
      observedDates: new Set(),
      plannedDates: new Set()
    };

    current.runtime_snp += rowValues.runtime_snp;
    current.runtime_gnp += rowValues.runtime_gnp;
    current.downtime += rowValues.downtime;
    current.waiting_time += rowValues.waiting_time;
    current.loss_time += rowValues.loss_time;
    current.idle_time += rowValues.idle_time;
    current.change_over_time += rowValues.change_over_time;
    current.reflong_related_downtime += rowValues.reflong_related_downtime;
    current.late_start_time += rowValues.late_start_time;
    current.uv_tower = current.uv_tower || Boolean(row.uv_tower);

    if (row.run_date) {
      current.observedDates.add(row.run_date);
    }

    if (row.run_date && isActiveCapacityDetailRow(row)) {
      current.plannedDates.add(row.run_date);
    }

    grouped.set(name, current);
  }

  return Array.from(grouped.values())
    .map((row) => {
      const selectedCapacity = selectedProductionDays * CAPACITY_WINDOW_MINUTES;
      const plannedCapacity = row.plannedDates.size * CAPACITY_WINDOW_MINUTES;
      const observedCapacity = row.observedDates.size * CAPACITY_WINDOW_MINUTES;
      const missingUnplannedTime = Math.max(selectedCapacity - observedCapacity, 0);
      const capacityBasis = selectedCapacity;
      const breakdownStacks = FOLDER_BREAKDOWN_STACKS;
      const capacityValues = normalizeBreakdownCapacityValues({
        waiting_time: row.waiting_time,
        loss_time: row.loss_time,
        downtime: row.downtime,
        runtime_snp: row.runtime_snp,
        runtime_gnp: row.runtime_gnp,
        idle_time: row.idle_time + missingUnplannedTime
      }, capacityBasis);
      const finalNonWaitLoss = scaleLossSubcomponents(
        {
          change_over_time: row.change_over_time,
          reflong_related_downtime: row.reflong_related_downtime,
          late_start_time: row.late_start_time
        },
        capacityValues.loss_time
      );
      const percentages = calculateBreakdownPercentages(capacityValues, selectedCapacity, breakdownStacks);

      return {
        ...row,
        runtime_snp: Math.round(capacityValues.runtime_snp),
        runtime_gnp: Math.round(capacityValues.runtime_gnp),
        runtime: Math.round(capacityValues.runtime_snp + capacityValues.runtime_gnp),
        downtime: cleanNumber(capacityValues.downtime),
        waiting_time: cleanNumber(capacityValues.waiting_time),
        loss_time: cleanNumber(capacityValues.loss_time),
        idle_time: cleanNumber(capacityValues.idle_time),
        spare_time: cleanNumber(capacityValues.spare_time),
        change_over_time: cleanNumber(finalNonWaitLoss.change_over_time),
        reflong_related_downtime: cleanNumber(finalNonWaitLoss.reflong_related_downtime),
        late_start_time: cleanNumber(finalNonWaitLoss.late_start_time),
        available_capacity: cleanNumber(selectedCapacity),
        planned_capacity: cleanNumber(plannedCapacity),
        planned_nights: row.plannedDates.size,
        total_nights: selectedProductionDays,
        waiting_time_percentage: percentages.waiting_time,
        loss_time_percentage: percentages.loss_time,
        downtime_percentage: percentages.downtime,
        runtime_snp_percentage: percentages.runtime_snp,
        runtime_gnp_percentage: percentages.runtime_gnp,
        runtime_percentage: cleanNumber((percentages.runtime_snp || 0) + (percentages.runtime_gnp || 0)),
        spare_time_percentage: percentages.spare_time,
        idle_time_percentage: percentages.idle_time || 0
      };
    })
    .sort((a, b) => compareResourceNames(a[nameKey], b[nameKey]));
}

function calculateTotalActiveFolderCapacity(dailyRows) {
  if (!dailyRows?.length) return 0;

  const totalCapacityFolders = dailyRows.reduce(
    (total, row) => total + Number(row.capacity_folders_count || 0),
    0
  );

  if (totalCapacityFolders > 0) {
    return cleanNumber(totalCapacityFolders);
  }

  const maxActiveFolders = Math.max(
    ...dailyRows.map((row) => Number(row.active_folders_count || 0))
  );

  return cleanNumber(maxActiveFolders * dailyRows.length);
}

function calculateTotalTowerCapacity(dailyRows) {
  if (!dailyRows?.length) return 0;

  const totalCapacityTowers = dailyRows.reduce(
    (total, row) => total + Number(row.capacity_towers_count || 0),
    0
  );

  if (totalCapacityTowers > 0) {
    return cleanNumber(totalCapacityTowers);
  }

  return cleanNumber(
    dailyRows.reduce((total, row) => {
      const availableCapacity = Number(row.available_capacity || 0);
      return total + Math.ceil(availableCapacity / CAPACITY_WINDOW_MINUTES);
    }, 0)
  );
}

function calculateCapacityTowerCount(dailyRows, towerDetailRows, fallbackCount = 0) {
  const dailyTowerCount = Math.max(
    0,
    ...(dailyRows || []).map((row) => Number(row.capacity_towers_count || 0))
  );

  if (dailyTowerCount > 0) return cleanNumber(dailyTowerCount);

  const detailTowerCount = new Set(
    (towerDetailRows || []).map((row) => row.tower).filter(Boolean)
  ).size;

  if (detailTowerCount > 0) return cleanNumber(detailTowerCount);

  return cleanNumber(fallbackCount);
}

function calculateRuntimeTypeBuckets(row) {
  const runtime = Math.max(Number(row.runtime || 0), 0);
  const segments = Array.isArray(row.runtime_segments) ? row.runtime_segments : [];
  const buckets = {
    runtime_snp: 0,
    runtime_gnp: 0,
    source_runtime_snp: 0,
    source_runtime_gnp: 0
  };

  for (const segment of segments) {
    const minutes = Math.max(Number(segment.minutes || 0), 0);
    if (minutes <= 0) continue;

    const sourceMinutes = Math.max(Number(segment.source_runtime_minutes || segment.minutes || 0), 0);
    const typeText = `${segment.type || ""} ${segment.key || ""} ${segment.label || ""}`.toLowerCase();
    if (typeText.includes("snp")) {
      buckets.runtime_snp += minutes;
      buckets.source_runtime_snp += sourceMinutes;
    } else {
      buckets.runtime_gnp += minutes;
      buckets.source_runtime_gnp += sourceMinutes;
    }
  }

  const segmentTotal = buckets.runtime_snp + buckets.runtime_gnp;
  if (runtime <= 0) {
    return buckets;
  }

  if (segmentTotal <= 0) {
    return {
      runtime_snp: 0,
      runtime_gnp: runtime,
      source_runtime_snp: 0,
      source_runtime_gnp: runtime
    };
  }

  const scale = runtime / segmentTotal;
  return {
    runtime_snp: buckets.runtime_snp * scale,
    runtime_gnp: buckets.runtime_gnp * scale,
    source_runtime_snp: buckets.source_runtime_snp,
    source_runtime_gnp: buckets.source_runtime_gnp
  };
}

function scaleLossSubcomponents(lossParts, lossTotal) {
  const subcomponentTotal = Object.values(lossParts).reduce((total, value) => total + Math.max(Number(value || 0), 0), 0);
  const targetTotal = Math.max(Number(lossTotal || 0), 0);

  if (subcomponentTotal <= 0 || targetTotal <= 0) {
    return Object.fromEntries(
      Object.keys(lossParts).map((key) => [key, 0])
    );
  }

  if (subcomponentTotal <= targetTotal) {
    return Object.fromEntries(
      Object.entries(lossParts).map(([key, value]) => [key, Math.max(Number(value || 0), 0)])
    );
  }

  const scale = targetTotal / subcomponentTotal;
  return Object.fromEntries(
    Object.entries(lossParts).map(([key, value]) => [key, Math.max(Number(value || 0), 0) * scale])
  );
}

function normalizeBreakdownCapacityValues(values, availableCapacity) {
  const capacity = Math.max(Number(availableCapacity || 0), 0);
  const normalized = {
    waiting_time: Math.max(Number(values.waiting_time || 0), 0),
    loss_time: Math.max(Number(values.loss_time || 0), 0),
    downtime: Math.max(Number(values.downtime || 0), 0),
    runtime_snp: Math.max(Number(values.runtime_snp || 0), 0),
    runtime_gnp: Math.max(Number(values.runtime_gnp || 0), 0),
    idle_time: Math.max(Number(values.idle_time || 0), 0),
    spare_time: 0
  };
  const componentKeys = ["waiting_time", "loss_time", "downtime", "runtime_snp", "runtime_gnp", "idle_time"];
  const used = componentKeys.reduce((total, key) => total + normalized[key], 0);

  if (capacity <= 0) {
    return normalized;
  }

  if (used > capacity) {
    let overage = cleanNumber(used - capacity);

    for (const key of [...componentKeys].reverse()) {
      if (overage <= 0) break;

      const reduction = Math.min(normalized[key], overage);
      normalized[key] = cleanNumber(normalized[key] - reduction);
      overage = cleanNumber(overage - reduction);
    }
  }

  const adjustedUsed = componentKeys.reduce((total, key) => total + normalized[key], 0);
  normalized.spare_time = Math.max(capacity - adjustedUsed, 0);

  return normalized;
}

function calculateBreakdownPercentages(values, availableCapacity, stacks = BREAKDOWN_STACKS) {
  const capacity = Number(availableCapacity || 0);

  if (capacity <= 0) {
    return Object.fromEntries(stacks.map((stack) => [stack.key, 0]));
  }

  const rawPercentages = stacks.map((stack) => {
    const rawPercentage = (Math.max(Number(values[stack.key] || 0), 0) / capacity) * 100;
    return {
      key: stack.key,
      percentage: Math.min(Math.max(rawPercentage, 0), 100)
    };
  });
  const rawTotal = rawPercentages.reduce((total, row) => total + row.percentage, 0);
  const scale = rawTotal > 100 ? 100 / rawTotal : 1;
  const percentages = {};
  let roundedTotal = 0;

  rawPercentages.forEach((row) => {
    const rounded = cleanNumber(row.percentage * scale);
    percentages[row.key] = rounded;
    roundedTotal = cleanNumber(roundedTotal + rounded);
  });

  if (roundedTotal > 100) {
    let overage = cleanNumber(roundedTotal - 100);

    for (const stack of [...stacks].reverse()) {
      if (overage <= 0) break;

      const reduction = Math.min(percentages[stack.key] || 0, overage);
      percentages[stack.key] = cleanNumber((percentages[stack.key] || 0) - reduction);
      overage = cleanNumber(overage - reduction);
    }
  }

  return percentages;
}

function calculatePercentage(numerator, denominator) {
  const capacity = Number(denominator || 0);
  if (capacity <= 0) return 0;

  const percentage = (Number(numerator || 0) / capacity) * 100;
  return cleanNumber(Math.min(Math.max(percentage, 0), 100));
}

function calculateRawPercentage(numerator, denominator) {
  const capacity = Number(denominator || 0);
  if (capacity <= 0) return 0;

  const percentage = (Number(numerator || 0) / capacity) * 100;
  return Math.min(Math.max(percentage, 0), 100);
}

function calculateWeightedSpeed(weightedTotal, weightMinutes) {
  const minutes = Number(weightMinutes || 0);
  if (minutes <= 0) return 0;
  return cleanNumber(Number(weightedTotal || 0) / minutes);
}

function calculateActualSpeedFromPo(printOrder, runtimeMinutes, downtimeMinutes = 0) {
  const elapsedHours = (Number(runtimeMinutes || 0) + Number(downtimeMinutes || 0)) / 60;
  if (elapsedHours <= 0) return 0;
  return cleanNumber(Number(printOrder || 0) / elapsedHours);
}

function calculateSpeedEfficiency(actualSpeed, committedSpeed) {
  const committed = Number(committedSpeed || 0);
  if (committed <= 0) return 0;
  return cleanNumber((Number(actualSpeed || 0) / committed) * 100);
}

function finalizeRuntimeSegmentMetrics(segments, downtime = 0) {
  const totalRuntime = segments.reduce((total, segment) => total + Number(segment.minutes || 0), 0);
  const totalDowntime = Math.max(Number(downtime || 0), 0);

  return segments.map((segment) => {
    const minutes = Number(segment.minutes || 0);
    const downtimeShare = totalRuntime > 0 ? totalDowntime * (minutes / totalRuntime) : 0;
    const actualSpeed = calculateActualSpeedFromPo(segment.print_order, minutes, downtimeShare);
    const committedSpeed = Number(segment.committed_speed || 0);

    return {
      ...segment,
      actual_speed: actualSpeed,
      effective_speed: actualSpeed,
      speed_efficiency: calculateSpeedEfficiency(actualSpeed, committedSpeed)
    };
  });
}

function compareResourceNames(first, second) {
  const firstParts = splitResourceName(first);
  const secondParts = splitResourceName(second);

  if (firstParts.prefix !== secondParts.prefix) {
    return firstParts.prefix.localeCompare(secondParts.prefix);
  }

  if (firstParts.number !== secondParts.number) {
    return firstParts.number - secondParts.number;
  }

  return String(first || "").localeCompare(String(second || ""));
}

function splitResourceName(value) {
  const text = String(value || "").trim();
  const match = /^(.*?)(\d+)(.*)$/.exec(text);

  if (!match) {
    return {
      prefix: text,
      number: Number.MAX_SAFE_INTEGER
    };
  }

  return {
    prefix: match[1].trim(),
    number: Number(match[2])
  };
}

function cleanNumber(value) {
  const numeric = Number.isFinite(Number(value)) ? Number(value) : 0;
  const rounded = Math.round(numeric * 100) / 100;
  return Number.isInteger(rounded) ? rounded : Number(rounded.toFixed(2));
}

function formatNumber(value) {
  return new Intl.NumberFormat("en-US", { maximumFractionDigits: 1 }).format(Number(value || 0));
}

function formatMinutes(value) {
  return `${formatNumber(value)} min`;
}

function formatKpiDuration(value) {
  const totalMinutes = Math.max(Math.round(Number(value || 0)), 0);
  const hours = Math.floor(totalMinutes / 60);
  const minutes = totalMinutes % 60;

  if (hours <= 0) return `${minutes} min`;
  if (minutes <= 0) return `${formatNumber(hours)} hr`;
  return `${formatNumber(hours)} hr ${minutes} min`;
}

function formatCapacityMinutes(value) {
  return `${formatNumber(value)} mins`;
}

function formatCapacitySummaryValue(minutes, totalCapacity) {
  const percentage = totalCapacity > 0 ? (Number(minutes || 0) / totalCapacity) * 100 : 0;
  return `${formatCapacityMinutes(minutes)} (${formatFixedPercent(percentage)})`;
}

function formatRuntimeSegmentLabel(segment) {
  if (!segment?.runtimeSegment) return "";
  if (!segment.isComplex) return "";
  return "▲";
}

function formatRuntimeSegmentShortType(segment) {
  const text = `${segment?.key || ""} ${segment?.label || ""}`.toLowerCase();
  if (text.includes("snp")) return "SNP";
  if (text.includes("gnp")) return "GNP";
  return "Run";
}

function formatRuntimeSegmentDetail(segment, isTwin = false) {
  const complexityText = formatRuntimeComplexityCode(segment);
  const speedText = formatEffectiveSpeed(segment?.effective_speed, isTwin);
  const printOrderText = formatPrintOrderVolume(segment?.print_order, isTwin);
  return [complexityText, printOrderText, speedText].filter(Boolean).join(" | ");
}

function formatRuntimeComplexityCode(segment) {
  const code = String(segment?.complexity_code || "").trim().toUpperCase();
  if (/^C(?:[1-9]|1[0-5])$/.test(code)) return code;

  const label = String(segment?.label || "").trim().toUpperCase();
  if (/^C(?:[1-9]|1[0-5])$/.test(label)) return label;

  return "";
}

function calculateRuntimeLabelFontSize(label, segmentHeight, barWidth) {
  if (!label || segmentHeight < 28 || barWidth < 10) return 0;

  const availableLength = Math.max(segmentHeight - 8, 0);
  const availableWidth = Math.max(barWidth - 4, 0);
  const lengthLimitedSize = availableLength / Math.max(label.length * 0.56, 1);
  const widthLimitedSize = availableWidth;
  const fontSize = Math.min(11, lengthLimitedSize, widthLimitedSize);

  return fontSize >= 8 ? Math.floor(fontSize * 10) / 10 : 0;
}

function formatEffectiveSpeed(value, isTwin = false) {
  const raw = Number(value || 0);
  if (raw <= 0) return "";
  const speed = isTwin ? raw / 2 : raw;
  return `Speed: ${formatCompactQuantity(speed)} CPH`;
}

function formatPrintOrderVolume(value, isTwin = false) {
  const raw = Number(value || 0);
  if (raw <= 0) return "";
  const volume = isTwin ? raw / 2 : raw;
  return `PO: ${formatCompactQuantity(volume)}`;
}

function formatCompactQuantity(value) {
  const numeric = Number(value || 0);
  const absValue = Math.abs(numeric);

  if (absValue >= 1000000) {
    const millions = numeric / 1000000;
    const rounded = absValue >= 100000000
      ? Math.round(millions)
      : Math.round(millions * 10) / 10;
    return `${formatNumber(rounded)}m`;
  }

  if (absValue >= 1000) {
    const thousands = numeric / 1000;
    const rounded = absValue >= 100000
      ? Math.round(thousands)
      : Math.round(thousands * 10) / 10;
    return `${formatNumber(rounded)}k`;
  }

  return formatNumber(Math.round(numeric));
}

function formatFixedPercent(value) {
  const numeric = Math.min(Math.max(Number(value || 0), 0), 100);
  return `${numeric.toLocaleString("en-US", {
    minimumFractionDigits: 2,
    maximumFractionDigits: 2
  })}%`;
}

function formatPercent(value) {
  return `${formatNumber(Math.min(Math.max(Number(value || 0), 0), 100))}%`;
}
