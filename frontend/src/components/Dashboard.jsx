import {
  Bar,
  BarChart,
  CartesianGrid,
  Legend,
  ResponsiveContainer,
  Tooltip,
  XAxis,
  YAxis
} from "recharts";
import { X } from "lucide-react";
import { useEffect, useMemo, useState } from "react";

import KpiCard from "./KpiCard.jsx";

const COLORS = {
  runtime: "#2563eb",
  lost_time: "#f59e0b",
  downtime: "#dc2626",
  buffer_time: "#cbd5e1"
};

export default function Dashboard({ data }) {
  const firstDay = data.daily?.[0]?.run_date || "";
  const [selectedDay, setSelectedDay] = useState(firstDay);
  const [isFolderPanelOpen, setIsFolderPanelOpen] = useState(false);

  useEffect(() => {
    setSelectedDay(firstDay);
    setIsFolderPanelOpen(false);
  }, [firstDay]);

  const selectedDaily = useMemo(
    () => data.daily.find((day) => day.run_date === selectedDay) || data.daily[0],
    [data.daily, selectedDay]
  );
  const selectedDetails = useMemo(
    () =>
      data.details
        .filter((row) => row.run_date === selectedDaily?.run_date)
        .map((row) => ({
          ...row,
          label: `${row.machine} / ${row.folder}`
        })),
    [data.details, selectedDaily]
  );

  const kpis = [
    ["Total Available Capacity", formatMinutes(data.summary.total_available_capacity), "blue"],
    ["Total Runtime", formatMinutes(data.summary.total_runtime), "green"],
    ["Total Lost Time", formatMinutes(data.summary.total_lost_time), "amber"],
    ["Total Downtime", formatMinutes(data.summary.total_downtime), "red"],
    ["Total Spare Time", formatMinutes(data.summary.total_buffer_time), "slate"],
    ["Average Utilization", formatPercent(data.summary.average_utilization_percentage), "blue"],
    ["Active Folder-Days", formatNumber(data.summary.active_folder_days), "slate"]
  ];

  return (
    <div className="mt-6 space-y-6">
      <section className="grid gap-4 sm:grid-cols-2 lg:grid-cols-4">
        {kpis.map(([label, value, tone]) => (
          <KpiCard key={label} label={label} value={value} tone={tone} />
        ))}
      </section>

      <section className="rounded-lg border border-slate-200 bg-white p-4 shadow-soft">
        <div className="mb-4 flex flex-col gap-2 sm:flex-row sm:items-end sm:justify-between">
          <div>
            <h2 className="text-base font-semibold text-slate-950">Daily capacity split</h2>
            <p className="mt-1 text-sm text-slate-500">Runtime, lost time, downtime, and spare time by Run Date</p>
          </div>
          {selectedDaily && (
            <div className="text-sm text-slate-500">
              Selected: <span className="font-semibold text-slate-800">{selectedDaily.run_date}</span>
            </div>
          )}
        </div>

        <div className="h-[360px] w-full">
          <ResponsiveContainer>
            <BarChart
              data={data.daily}
              margin={{ top: 12, right: 20, left: 8, bottom: 18 }}
              onClick={(event) => {
                if (event?.activeLabel) {
                  setSelectedDay(event.activeLabel);
                  setIsFolderPanelOpen(true);
                }
              }}
            >
              <CartesianGrid strokeDasharray="3 3" stroke="#e2e8f0" vertical={false} />
              <XAxis
                dataKey="run_date"
                tick={{ fill: "#475569", fontSize: 12 }}
                tickLine={false}
                axisLine={{ stroke: "#cbd5e1" }}
              />
              <YAxis
                tick={{ fill: "#475569", fontSize: 12 }}
                tickLine={false}
                axisLine={{ stroke: "#cbd5e1" }}
                width={72}
              />
              <Tooltip content={<CapacityTooltip />} cursor={{ fill: "rgba(37, 99, 235, 0.08)" }} />
              <Legend wrapperStyle={{ paddingTop: 10 }} />
              <Bar dataKey="runtime" name="Runtime" stackId="capacity" fill={COLORS.runtime} />
              <Bar dataKey="lost_time" name="Lost Time" stackId="capacity" fill={COLORS.lost_time} />
              <Bar dataKey="downtime" name="Downtime / Breakdown" stackId="capacity" fill={COLORS.downtime} />
              <Bar dataKey="buffer_time" name="Spare Time" stackId="capacity" fill={COLORS.buffer_time} />
            </BarChart>
          </ResponsiveContainer>
        </div>
      </section>

      <FolderMachinePanel
        day={selectedDaily}
        details={selectedDetails}
        open={isFolderPanelOpen}
        onClose={() => setIsFolderPanelOpen(false)}
      />

      <section className="grid gap-6 xl:grid-cols-[1.2fr_0.8fr]">
        <DailyTable daily={data.daily} selectedDay={selectedDaily?.run_date} onSelectDay={setSelectedDay} />
        <DetailsTable day={selectedDaily} details={selectedDetails} />
      </section>
    </div>
  );
}

function FolderMachinePanel({ day, details, open, onClose }) {
  if (!open || !day || details.length === 0) return null;

  const chartHeight = Math.max(280, details.length * 58 + 92);

  return (
    <div className="fixed inset-0 z-50">
      <button
        type="button"
        aria-label="Close machine-folder panel"
        className="absolute inset-0 h-full w-full bg-slate-950/30"
        onClick={onClose}
      />

      <section className="absolute bottom-4 left-4 right-4 top-4 flex flex-col rounded-lg border border-slate-200 bg-white shadow-2xl md:left-auto md:w-[760px]">
        <div className="flex items-start justify-between gap-4 border-b border-slate-200 px-5 py-4">
          <div>
            <h2 className="text-base font-semibold text-slate-950">Machine-folder time consumption</h2>
            <p className="mt-1 text-sm text-slate-500">
              {day.run_date} · {formatNumber(details.length)} active folder{details.length === 1 ? "" : "s"}
            </p>
          </div>
          <button
            type="button"
            aria-label="Close panel"
            onClick={onClose}
            className="inline-flex h-9 w-9 shrink-0 items-center justify-center rounded-lg border border-slate-200 text-slate-500 transition hover:bg-slate-50 hover:text-slate-800"
          >
            <X className="h-4 w-4" aria-hidden="true" />
          </button>
        </div>

        <div className="min-h-0 flex-1 overflow-y-auto p-5">
          <div className="w-full" style={{ height: chartHeight }}>
            <ResponsiveContainer>
              <BarChart
                data={details}
                layout="vertical"
                margin={{ top: 8, right: 24, left: 12, bottom: 8 }}
              >
                <CartesianGrid strokeDasharray="3 3" stroke="#e2e8f0" horizontal={false} />
                <XAxis
                  type="number"
                  tick={{ fill: "#475569", fontSize: 12 }}
                  tickLine={false}
                  axisLine={{ stroke: "#cbd5e1" }}
                />
                <YAxis
                  type="category"
                  dataKey="label"
                  width={170}
                  tick={{ fill: "#334155", fontSize: 12 }}
                  tickLine={false}
                  axisLine={{ stroke: "#cbd5e1" }}
                />
                <Tooltip content={<FolderTooltip />} cursor={{ fill: "rgba(37, 99, 235, 0.08)" }} />
                <Legend wrapperStyle={{ paddingTop: 10 }} />
                <Bar dataKey="runtime" name="Runtime" stackId="folder" fill={COLORS.runtime} />
                <Bar dataKey="lost_time" name="Lost Time" stackId="folder" fill={COLORS.lost_time} />
                <Bar dataKey="downtime" name="Downtime / Breakdown" stackId="folder" fill={COLORS.downtime} />
                <Bar dataKey="buffer_time" name="Spare Time" stackId="folder" fill={COLORS.buffer_time} />
              </BarChart>
            </ResponsiveContainer>
          </div>
        </div>
      </section>
    </div>
  );
}

function FolderTooltip({ active, payload }) {
  if (!active || !payload?.length) return null;
  const row = payload[0].payload;

  return (
    <div className="rounded-lg border border-slate-200 bg-white p-3 text-sm shadow-soft">
      <p className="font-semibold text-slate-950">{row.machine}</p>
      <p className="text-slate-500">{row.folder}</p>
      <div className="mt-2 space-y-1 text-slate-600">
        <TooltipRow label="Runtime" value={formatMinutes(row.runtime)} color={COLORS.runtime} />
        <TooltipRow label="Lost Time" value={formatMinutes(row.lost_time)} color={COLORS.lost_time} />
        <TooltipRow label="Downtime" value={formatMinutes(row.downtime)} color={COLORS.downtime} />
        <TooltipRow label="Spare Time" value={formatMinutes(row.buffer_time)} color={COLORS.buffer_time} />
        <div className="border-t border-slate-200 pt-2">
          <p>Available Capacity: {formatMinutes(row.available_capacity)}</p>
          <p>Changeover: {formatMinutes(row.change_over_time)}</p>
          <p>Late Start: {formatMinutes(row.late_start_time)}</p>
        </div>
      </div>
    </div>
  );
}

function CapacityTooltip({ active, payload, label }) {
  if (!active || !payload?.length) return null;
  const day = payload[0].payload;

  return (
    <div className="rounded-lg border border-slate-200 bg-white p-3 text-sm shadow-soft">
      <p className="font-semibold text-slate-950">{label}</p>
      <div className="mt-2 space-y-1 text-slate-600">
        <TooltipRow label="Runtime" value={formatMinutes(day.runtime)} color={COLORS.runtime} />
        <TooltipRow label="Lost Time" value={formatMinutes(day.lost_time)} color={COLORS.lost_time} />
        <TooltipRow label="Downtime" value={formatMinutes(day.downtime)} color={COLORS.downtime} />
        <TooltipRow label="Spare Time" value={formatMinutes(day.buffer_time)} color={COLORS.buffer_time} />
        <div className="border-t border-slate-200 pt-2">
          <p>Available Capacity: {formatMinutes(day.available_capacity)}</p>
          <p>Active Folders: {formatNumber(day.active_folders_count)}</p>
          <p>Utilization: {formatPercent(day.utilization_percentage)}</p>
        </div>
      </div>
    </div>
  );
}

function TooltipRow({ label, value, color }) {
  return (
    <div className="flex items-center gap-2">
      <span className="h-2.5 w-2.5 rounded-full" style={{ backgroundColor: color }} />
      <span>
        {label}: {value}
      </span>
    </div>
  );
}

function DailyTable({ daily, selectedDay, onSelectDay }) {
  return (
    <section className="rounded-lg border border-slate-200 bg-white shadow-soft">
      <div className="border-b border-slate-200 px-4 py-3">
        <h2 className="text-base font-semibold text-slate-950">Daily summary</h2>
      </div>
      <div className="overflow-x-auto">
        <table className="min-w-full divide-y divide-slate-200 text-sm">
          <thead className="bg-slate-50 text-left text-xs font-semibold uppercase tracking-normal text-slate-500">
            <tr>
              <th className="px-4 py-3">Run Date</th>
              <th className="px-4 py-3 text-right">Capacity</th>
              <th className="px-4 py-3 text-right">Runtime</th>
              <th className="px-4 py-3 text-right">Lost</th>
              <th className="px-4 py-3 text-right">Down</th>
              <th className="px-4 py-3 text-right">Spare</th>
              <th className="px-4 py-3 text-right">Util.</th>
            </tr>
          </thead>
          <tbody className="divide-y divide-slate-100">
            {daily.map((day) => (
              <tr
                key={day.run_date}
                onClick={() => onSelectDay(day.run_date)}
                className={`cursor-pointer transition hover:bg-blue-50 ${
                  selectedDay === day.run_date ? "bg-blue-50" : "bg-white"
                }`}
              >
                <td className="whitespace-nowrap px-4 py-3 font-medium text-slate-900">{day.run_date}</td>
                <td className="px-4 py-3 text-right text-slate-700">{formatMinutes(day.available_capacity)}</td>
                <td className="px-4 py-3 text-right text-slate-700">{formatMinutes(day.runtime)}</td>
                <td className="px-4 py-3 text-right text-slate-700">{formatMinutes(day.lost_time)}</td>
                <td className="px-4 py-3 text-right text-slate-700">{formatMinutes(day.downtime)}</td>
                <td className="px-4 py-3 text-right text-slate-700">{formatMinutes(day.buffer_time)}</td>
                <td className="px-4 py-3 text-right text-slate-700">{formatPercent(day.utilization_percentage)}</td>
              </tr>
            ))}
          </tbody>
        </table>
      </div>
    </section>
  );
}

function DetailsTable({ day, details }) {
  return (
    <section className="rounded-lg border border-slate-200 bg-white shadow-soft">
      <div className="flex items-center justify-between border-b border-slate-200 px-4 py-3">
        <h2 className="text-base font-semibold text-slate-950">Folder breakdown</h2>
        {day && <span className="text-sm text-slate-500">{day.run_date}</span>}
      </div>
      <div className="overflow-x-auto">
        <table className="min-w-full divide-y divide-slate-200 text-sm">
          <thead className="bg-slate-50 text-left text-xs font-semibold uppercase tracking-normal text-slate-500">
            <tr>
              <th className="px-4 py-3">Machine</th>
              <th className="px-4 py-3">Folder</th>
              <th className="px-4 py-3 text-right">Runtime</th>
              <th className="px-4 py-3 text-right">Lost</th>
              <th className="px-4 py-3 text-right">Down</th>
              <th className="px-4 py-3 text-right">Spare</th>
            </tr>
          </thead>
          <tbody className="divide-y divide-slate-100">
            {details.map((row) => (
              <tr key={`${row.run_date}-${row.machine}-${row.folder}`}>
                <td className="whitespace-nowrap px-4 py-3 font-medium text-slate-900">{row.machine}</td>
                <td className="whitespace-nowrap px-4 py-3 text-slate-700">{row.folder}</td>
                <td className="px-4 py-3 text-right text-slate-700">{formatMinutes(row.runtime)}</td>
                <td className="px-4 py-3 text-right text-slate-700">{formatMinutes(row.lost_time)}</td>
                <td className="px-4 py-3 text-right text-slate-700">{formatMinutes(row.downtime)}</td>
                <td className="px-4 py-3 text-right text-slate-700">{formatMinutes(row.buffer_time)}</td>
              </tr>
            ))}
          </tbody>
        </table>
      </div>
    </section>
  );
}

function formatNumber(value) {
  return new Intl.NumberFormat("en-US", { maximumFractionDigits: 1 }).format(Number(value || 0));
}

function formatMinutes(value) {
  return `${formatNumber(value)} min`;
}

function formatPercent(value) {
  return `${formatNumber(value)}%`;
}
