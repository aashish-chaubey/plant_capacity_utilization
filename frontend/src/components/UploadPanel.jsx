import { FileSpreadsheet, Loader2, UploadCloud } from "lucide-react";
import { useRef, useState } from "react";

export default function UploadPanel({ onUpload, loading, compact = false }) {
  const inputRef = useRef(null);
  const [dragging, setDragging] = useState(false);

  function submitFile(file) {
    if (file) onUpload(file);
  }

  function handleDrop(event) {
    event.preventDefault();
    setDragging(false);
    submitFile(event.dataTransfer.files?.[0]);
  }

  return (
    <section
      className={`rounded-lg border bg-white shadow-soft transition ${
        dragging ? "border-blue-400 ring-2 ring-blue-100" : "border-slate-200"
      } ${compact ? "p-4" : "p-6"}`}
      onDragOver={(event) => {
        event.preventDefault();
        setDragging(true);
      }}
      onDragLeave={() => setDragging(false)}
      onDrop={handleDrop}
    >
      <div className="flex flex-col gap-4 md:flex-row md:items-center md:justify-between">
        <div className="flex items-start gap-4">
          <div className="flex h-11 w-11 shrink-0 items-center justify-center rounded-lg bg-blue-50 text-blue-700">
            <FileSpreadsheet className="h-6 w-6" aria-hidden="true" />
          </div>
          <div>
            <h2 className="text-base font-semibold text-slate-950">Upload production report</h2>
            <p className="mt-1 max-w-2xl text-sm leading-6 text-slate-500">
              Accepts `.xlsx` files with `Book Wise Details` and `Down Time` sheets.
            </p>
          </div>
        </div>

        <div className="flex flex-col gap-2 sm:flex-row">
          <input
            ref={inputRef}
            type="file"
            accept=".xlsx"
            className="hidden"
            onChange={(event) => submitFile(event.target.files?.[0])}
          />
          <button
            type="button"
            onClick={() => inputRef.current?.click()}
            disabled={loading}
            className="inline-flex min-h-10 items-center justify-center gap-2 rounded-lg bg-blue-700 px-4 text-sm font-semibold text-white shadow-sm transition hover:bg-blue-800 disabled:cursor-not-allowed disabled:bg-slate-400"
          >
            {loading ? (
              <Loader2 className="h-4 w-4 animate-spin" aria-hidden="true" />
            ) : (
              <UploadCloud className="h-4 w-4" aria-hidden="true" />
            )}
            {loading ? "Processing" : "Choose file"}
          </button>
        </div>
      </div>
    </section>
  );
}
