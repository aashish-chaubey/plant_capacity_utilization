import { useEffect, useState } from "react";

const API_BASE_URL = String(import.meta.env.VITE_API_BASE_URL || "").replace(/\/+$/, "");

export default function EvalLogPage() {
  const [records, setRecords] = useState([]);
  const [stats, setStats] = useState({});
  const [loading, setLoading] = useState(true);
  const [error, setError] = useState("");
  const [modal, setModal] = useState(null);

  async function loadLogs() {
    setLoading(true);
    setError("");
    try {
      const response = await fetch(`${API_BASE_URL}/api/eval_n_log?limit=100`);
      if (!response.ok) throw new Error(`Failed with status ${response.status}`);
      const payload = await response.json();
      setRecords(payload.records || []);
      setStats(payload.stats || {});
    } catch (err) {
      setError(err.message || "Unable to load eval logs.");
    } finally {
      setLoading(false);
    }
  }

  useEffect(() => {
    loadLogs();
  }, []);

  return (
    <div className="min-h-screen bg-slate-50 text-slate-900">
      <header className="bg-slate-950 px-6 py-5 text-white">
        <h1 className="text-xl font-bold">Chat Eval & Log</h1>
        <p className="mt-1 text-sm text-slate-300">Separate read-only view over backend chat evaluation logs.</p>
      </header>

      <main className="space-y-5 p-6">
        <div className="flex items-center gap-3">
          <button
            type="button"
            onClick={loadLogs}
            className="rounded-lg bg-blue-600 px-4 py-2 text-sm font-bold text-white hover:bg-blue-700"
          >
            Refresh
          </button>
          {loading && <span className="text-sm text-slate-500">Loading...</span>}
          {error && <span className="text-sm font-semibold text-red-600">{error}</span>}
        </div>

        <section className="grid gap-3 sm:grid-cols-2 lg:grid-cols-5">
          <StatCard label="Records" value={stats.records ?? 0} />
          <StatCard label="Chat Logged" value={stats.chat_logged ?? 0} />
          <StatCard label="Eval Complete" value={stats.evaluation_completed ?? 0} />
          <StatCard label="Queued" value={stats.queued ?? 0} />
          <StatCard label="Size" value={`${Math.round((stats.size_bytes || 0) / 1024)} KB`} />
        </section>

        <section className="overflow-hidden rounded-xl border border-slate-200 bg-white shadow-sm">
          <div className="overflow-x-auto">
            <table className="w-full border-collapse text-left text-sm">
              <thead className="bg-slate-100 text-xs uppercase tracking-wide text-slate-500">
                <tr>
                  <th className="px-3 py-3">Time</th>
                  <th className="px-3 py-3">Event</th>
                  <th className="px-3 py-3">Query</th>
                  <th className="px-3 py-3">Response</th>
                  <th className="px-3 py-3">Eval</th>
                  <th className="px-3 py-3">Scores</th>
                  <th className="px-3 py-3">Prompt / Context</th>
                </tr>
              </thead>
              <tbody>
                {records.map((record, index) => (
                  <tr key={`${record.line_number}-${record.chat_id}`} className="border-t border-slate-200 align-top">
                    <td className="px-3 py-3 text-xs">
                      {record.created_at || "-"}
                      <div className="text-slate-400">#{record.line_number}</div>
                    </td>
                    <td className="px-3 py-3 text-xs">
                      {record.event || "-"}
                      <div className="max-w-[180px] truncate text-slate-400">{record.chat_id || ""}</div>
                    </td>
                    <td className="max-w-[300px] px-3 py-3 font-semibold">
                      {shortText(record.query)}
                      <SeeMore onClick={() => setModal(buildModal(record, "query"))} />
                    </td>
                    <td className="max-w-[380px] px-3 py-3 whitespace-pre-wrap">
                      {shortText(record.response, 20)}
                      <SeeMore onClick={() => setModal(buildModal(record, "response"))} />
                    </td>
                    <td className="px-3 py-3">
                      <span className={`rounded-full px-2 py-0.5 text-xs font-bold ${statusClass(record.evaluation_status)}`}>
                        {record.evaluation_status || "none"}
                      </span>
                      {record.evaluation_error && <div className="mt-1 max-w-[260px] text-xs text-red-600">{record.evaluation_error}</div>}
                    </td>
                    <td className="px-3 py-3 font-mono text-xs">
                      Faith: {score(record.faithfulness)}
                      <br />
                      Answer: {score(record.answer_relevance)}
                      <br />
                      Context: {score(record.context_relevance)}
                    </td>
                    <td className="px-3 py-3 font-mono text-xs">
                      Prompt: {record.system_prompt_chars || 0} chars{" "}
                      <InlineButton onClick={() => setModal(buildModal(record, "system_prompt"))}>open</InlineButton>
                      <br />
                      Context: {record.retrieval_context_chars || 0} chars{" "}
                      <InlineButton onClick={() => setModal(buildModal(record, "retrieval_context"))}>open</InlineButton>
                    </td>
                  </tr>
                ))}
                {!records.length && !loading && (
                  <tr>
                    <td colSpan="7" className="px-3 py-8 text-center text-slate-500">
                      No eval log records found.
                    </td>
                  </tr>
                )}
              </tbody>
            </table>
          </div>
        </section>
      </main>

      {modal && <ContentModal modal={modal} onClose={() => setModal(null)} />}
    </div>
  );
}

function StatCard({ label, value }) {
  return (
    <div className="rounded-xl border border-slate-200 bg-white p-4 shadow-sm">
      <div className="text-sm text-slate-500">{label}</div>
      <div className="mt-1 text-xl font-bold">{value}</div>
    </div>
  );
}

function SeeMore({ onClick }) {
  return (
    <button type="button" onClick={onClick} className="mt-1 block text-xs font-bold text-blue-600 hover:underline">
      see more
    </button>
  );
}

function InlineButton({ children, onClick }) {
  return (
    <button type="button" onClick={onClick} className="font-sans text-xs font-bold text-blue-600 hover:underline">
      {children}
    </button>
  );
}

function ContentModal({ modal, onClose }) {
  useEffect(() => {
    function onKeyDown(event) {
      if (event.key === "Escape") onClose();
    }
    document.addEventListener("keydown", onKeyDown);
    return () => document.removeEventListener("keydown", onKeyDown);
  }, [onClose]);

  return (
    <div className="fixed inset-0 z-50 flex items-center justify-center bg-slate-950/60 p-6" onClick={onClose}>
      <div className="flex max-h-[86vh] w-full max-w-6xl flex-col overflow-hidden rounded-2xl bg-white shadow-2xl" onClick={(event) => event.stopPropagation()}>
        <div className="flex items-center justify-between border-b border-slate-200 bg-slate-50 px-4 py-3">
          <div>
            <h2 className="font-bold">{modal.title}</h2>
            <p className="text-xs text-slate-500">{modal.meta}</p>
          </div>
          <button type="button" onClick={onClose} className="rounded-lg bg-slate-900 px-3 py-2 text-sm font-bold text-white">
            Close
          </button>
        </div>
        <div className="overflow-auto p-4">{modal.content}</div>
      </div>
    </div>
  );
}

function buildModal(record, field) {
  const titles = {
    query: "Full Query",
    response: "Full Response",
    system_prompt: "Full System Prompt",
    retrieval_context: "Full Retrieval Context",
  };
  const values = {
    query: record.query || "",
    response: record.response || "",
    system_prompt: record.system_prompt || "",
    retrieval_context: contextText(record),
  };
  return {
    title: titles[field] || field,
    meta: `Chat ID: ${record.chat_id || ""} | Line: ${record.line_number || ""}`,
    content: field === "retrieval_context" ? renderContext(record) : <pre className="whitespace-pre-wrap break-words font-mono text-xs leading-6">{values[field]}</pre>,
  };
}

function renderContext(record) {
  const parsed = parseContext(record);
  if (!parsed) return <pre className="whitespace-pre-wrap break-words font-mono text-xs">{contextText(record)}</pre>;

  return (
    <div className="space-y-4">
      {parsed.summary && <ObjectSection title="Summary" value={parsed.summary} />}
      {Object.entries(parsed)
        .filter(([key]) => key !== "summary")
        .map(([key, value]) =>
          Array.isArray(value) ? <ArraySection key={key} title={key} rows={value} /> : <ObjectSection key={key} title={key} value={value} />
        )}
      <details className="rounded-xl border border-slate-200">
        <summary className="cursor-pointer bg-slate-100 px-3 py-2 font-bold">Raw JSON context</summary>
        <pre className="max-h-96 overflow-auto bg-slate-950 p-3 text-xs text-slate-100">{JSON.stringify(parsed, null, 2)}</pre>
      </details>
    </div>
  );
}

function ObjectSection({ title, value }) {
  const entries = value && typeof value === "object" && !Array.isArray(value) ? Object.entries(value) : [["value", value]];
  return (
    <section className="overflow-hidden rounded-xl border border-slate-200">
      <h3 className="bg-slate-100 px-3 py-2 font-bold">{title}</h3>
      <div className="grid grid-cols-[minmax(160px,260px)_1fr] gap-x-4 gap-y-2 p-3 text-sm">
        {entries.map(([key, entryValue]) => (
          <div key={key} className="contents">
            <div className="text-slate-500">{key}</div>
            <div className="break-words">{formatValue(entryValue)}</div>
          </div>
        ))}
      </div>
    </section>
  );
}

function ArraySection({ title, rows }) {
  const sample = rows.slice(0, 20);
  const columns = Array.from(
    new Set(sample.flatMap((row) => (row && typeof row === "object" && !Array.isArray(row) ? Object.keys(row) : ["value"])))
  ).slice(0, 10);

  return (
    <section className="overflow-hidden rounded-xl border border-slate-200">
      <h3 className="bg-slate-100 px-3 py-2 font-bold">
        {title} ({rows.length} rows, showing first {sample.length})
      </h3>
      <div className="overflow-x-auto p-3">
        <table className="w-full border-collapse text-xs">
          <thead>
            <tr>{columns.map((column) => <th key={column} className="border border-slate-200 bg-slate-50 px-2 py-1 text-left">{column}</th>)}</tr>
          </thead>
          <tbody>
            {sample.map((row, index) => (
              <tr key={index}>
                {columns.map((column) => (
                  <td key={column} className="max-w-[260px] break-words border border-slate-200 px-2 py-1">
                    {formatValue(row && typeof row === "object" ? row[column] : row)}
                  </td>
                ))}
              </tr>
            ))}
          </tbody>
        </table>
      </div>
    </section>
  );
}

function contextText(record) {
  const context = Array.isArray(record.retrieval_context) ? record.retrieval_context : [];
  return context.join("\n\n--- context part ---\n\n");
}

function parseContext(record) {
  try {
    return JSON.parse(contextText(record));
  } catch {
    return null;
  }
}

function shortText(value, words = 14) {
  const text = String(value || "").replace(/\s+/g, " ").trim();
  const parts = text.split(" ").filter(Boolean);
  if (!parts.length) return "-";
  return parts.length <= words ? text : `${parts.slice(0, words).join(" ")}...`;
}

function score(metric) {
  if (!metric || metric.score === null || metric.score === undefined) return "-";
  const pass = metric.passed === true ? "yes" : metric.passed === false ? "no" : "";
  return `${Number(metric.score).toFixed(2)} ${pass}`;
}

function statusClass(status) {
  if (status === "complete") return "bg-green-100 text-green-800";
  if (status === "queued") return "bg-amber-100 text-amber-800";
  return "bg-red-100 text-red-800";
}

function formatValue(value) {
  if (value === null || value === undefined) return "";
  if (Array.isArray(value)) return value.length > 8 ? `${value.slice(0, 8).join(", ")} ... (${value.length})` : value.join(", ");
  if (typeof value === "object") return JSON.stringify(value);
  return String(value);
}
