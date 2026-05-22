import { AlertCircle } from "lucide-react";
import { useState } from "react";

import Dashboard from "./components/Dashboard.jsx";
import UploadPanel from "./components/UploadPanel.jsx";

const API_BASE_URL = import.meta.env.VITE_API_BASE_URL || "http://127.0.0.1:8000";

export default function App() {
  const [result, setResult] = useState(null);
  const [errors, setErrors] = useState([]);
  const [loading, setLoading] = useState(false);
  const [fileName, setFileName] = useState("");

  async function handleUpload(file) {
    if (!file) return;

    setLoading(true);
    setErrors([]);
    setFileName(file.name);

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
        <UploadPanel onUpload={handleUpload} loading={loading} compact={Boolean(result)} />

        {errors.length > 0 && (
          <section className="mt-5 rounded-lg border border-red-200 bg-red-50 p-4 text-red-900">
            <div className="flex items-start gap-3">
              <AlertCircle className="mt-0.5 h-5 w-5 shrink-0" aria-hidden="true" />
              <div>
                <h2 className="text-sm font-semibold">Validation errors</h2>
                <ul className="mt-2 space-y-1 text-sm">
                  {errors.map((error) => (
                    <li key={error}>{error}</li>
                  ))}
                </ul>
              </div>
            </div>
          </section>
        )}

        {result && <Dashboard data={result} />}
      </main>
    </div>
  );
}
