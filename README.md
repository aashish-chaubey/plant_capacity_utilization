# Plant Capacity Utilization Dashboard

Working MVP for uploading an Excel production report and generating capacity utilization across active machine-folder combinations with annual, half-yearly, quarterly, monthly, and custom timeframe filters.

## Stack

- Backend: FastAPI, pandas, openpyxl
- Frontend: React, Tailwind CSS, Recharts
- Database: skipped for this iteration; uploaded workbooks are processed statelessly

## Project Structure

```text
backend/
  app/
    main.py
    services/capacity.py
  requirements.txt
frontend/
  src/
    App.jsx
    components/
  package.json
data/
  sample production reports
```

## Backend Setup

```bash
cd backend
python3 -m venv .venv
source .venv/bin/activate
python3 -m pip install -r requirements.txt
python3 -m uvicorn app.main:app --reload --host 127.0.0.1 --port 8000
```

Health check:

```bash
curl http://127.0.0.1:8000/api/health
```

Upload endpoint:

```http
POST /api/upload
Content-Type: multipart/form-data
field: file
```

## Frontend Setup

```bash
cd frontend
npm install
npm run dev
```

Open the Vite URL shown in the terminal, normally:

```text
http://127.0.0.1:5173
```

If the backend is running on a different URL, set:

```bash
VITE_API_BASE_URL=http://127.0.0.1:8000 npm run dev
```

## Expected Report Structure

Uploads are sent directly to processing without a separate file validation checklist. Processing still expects the production report data to use the sheets and columns below.

Required sheets:

- `Book Wise Details`
- `Down Time`

Required `Book Wise Details` columns:

- `Issue Date`
- `Machine`
- `Folder`
- `Start Date`
- `Start Time`
- `End Date`
- `End Time`
- `Total Run Time (mnts)`
- `Total Downtime`
- `Reflong`
- `Issue Id`
- `Run Date`

Required `Down Time` columns:

- `IssueID`
- `Machine`
- `Folder`
- `Related`
- `Reason`
- `Start Time`
- `End Time`
- `Total Downtime`
- `Run Date`

Extra sheets and columns are ignored. Column names are matched after trimming whitespace. Sheet names are exact for this MVP.

## Calculation Assumptions

- `Issue Date` is the production date for the dashboard's 00:00-04:00 window.
- `Run Date` is still parsed and required for compatibility with the report format, but dashboard grouping is based on `Issue Date`.
- A capacity unit is one unique `Issue Date + Machine + Folder`.
- A folder is active on a day if it has at least one print interval overlapping `Issue Date` 00:00-04:00.
- Each active capacity unit has `240` available minutes for the fixed `00:00-04:00` window.
- Daily plant capacity uses the maximum active folder count observed for that plant/report. For example, if the plant reaches 5 active folders on any day, every daily bar uses `5 * 240 = 1200` available minutes.
- Runtime is calculated from merged print intervals inside the 00:00-04:00 window, so overlapping or duplicate edition rows are not double counted.
- Changeover time is calculated from the actual print sequence for each folder/day: the positive gap between the end of one edition print interval and the start of the next. The workbook's `Change Over Time (mins)` value is ignored.
- Downtime is summed from `Down Time`.`Total Downtime`.
- Reflong downtime is moved into Lost Time only when:
  - `Down Time`.`IssueID` matches `Book Wise Details`.`Issue Id`
  - the matched book-wise row has `Reflong = Yes`
  - `Down Time`.`Related` starts with `Reflong`, case-insensitive
- Late start is calculated per capacity unit from expected `00:00` to the earliest parsed print start time, capped at `240` minutes.
- Spare time is returned as `buffer_time` in the API. Per active folder it is calculated as `240 - runtime - lost_time - downtime`, floored at `0`; daily spare time also includes full 240-minute spare capacity for any plant-capacity folder that was not active that day.
- Average utilization is weighted: `total_runtime / total_available_capacity * 100`.

## Example Response

```json
{
  "valid": true,
  "summary": {
    "total_available_capacity": 7200,
    "total_runtime": 4300,
    "total_lost_time": 900,
    "total_downtime": 300,
    "total_buffer_time": 1700,
    "average_utilization_percentage": 59.72,
    "active_folder_days": 30
  },
  "daily": [
    {
      "run_date": "2026-03-31",
      "active_folders_count": 3,
      "capacity_folders_count": 5,
      "available_capacity": 1200,
      "runtime": 185,
      "lost_time": 40,
      "downtime": 6,
      "buffer_time": 969,
      "utilization_percentage": 15.42
    }
  ],
  "details": [
    {
      "run_date": "2026-03-31",
      "machine": "Hiline-2",
      "folder": "Folder 3",
      "available_capacity": 240,
      "runtime": 185,
      "lost_time": 40,
      "downtime": 6,
      "buffer_time": 9,
      "change_over_time": 20,
      "reflong_related_downtime": 5,
      "late_start_time": 15
    }
  ],
  "errors": []
}
```

Processing failure response:

```json
{
  "valid": false,
  "summary": null,
  "daily": [],
  "details": [],
  "errors": [
    "Processing failed: Worksheet named 'Book Wise Details' not found"
  ]
}
```
