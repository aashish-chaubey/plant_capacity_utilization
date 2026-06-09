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

Optional sheet:

- `General` is used for tower breakdowns. If it is missing, the dashboard still calculates folder and daily capacity, but tower breakdown data is empty.

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

Optional `Book Wise Details` columns used for twin folder mode:

- `Production Type`
- `Plant Name`

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

Required `General` columns for tower breakdowns:

- `IssueID`
- `Towers used`

Extra sheets and columns are ignored. Column names are matched after trimming whitespace. Sheet names are exact for this MVP.

## Calculation Assumptions

- `Issue Date` is the production date for the dashboard's 00:00-04:00 window.
- `Run Date` is still parsed and required for compatibility with the report format, but dashboard grouping is based on `Issue Date`.
- A capacity unit is one unique `Issue Date + Machine + Folder`.
- A folder is active on a day if it has at least one print interval overlapping `Issue Date` 00:00-04:00.
- Rows with `Production Type = >||>` are treated as twin folder runs when their plant, machine, and folder match `backend/twin_folder.json` or `backend/twin_folders.json`.
- In twin folder mode, the configured companion folder receives a copied row before folder capacity is calculated, so both folders share the same runtime, downtime, waiting, reflong, loss, and spare-time treatment.
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
- Spare time is returned as `buffer_time` in the API. Per active folder it is calculated as `240 - runtime - lost_time - downtime`, floored at `0`.
- Idle time is returned separately as `idle_time`. It is unused plant-capacity window time from folders that were not scheduled on that day, and is not added to spare time.
- Average utilization is weighted: `total_runtime / total_available_capacity * 100`.

## Calculation Workflow

### 1. Workbook parsing

1. The backend reads `Book Wise Details`, `Down Time`, and, when available, `General`.
2. Column names are trimmed before use.
3. `Issue Date`, `Run Date`, `Start Date`, `End Date`, `Start Time`, and `End Time` are parsed into date/time values.
4. `Start DateTime` and `End DateTime` are built from the parsed start/end date and time fields.
5. `Report Date` is set from `Issue Date`. All dashboard grouping uses `Issue Date`, not `Run Date`.
6. Machine, folder, issue id, reflong, downtime, and runtime fields are normalized for calculation.
7. Plant name and production type are normalized so twin folder mode can be matched against the local twin-folder mapping.

### 2. Print window filtering

1. The production window is fixed at `Issue Date 00:00` to `Issue Date 04:00`.
2. A `Book Wise Details` row is kept for capacity only if its print interval overlaps that window.
3. Rows outside the window are discarded before active folders, runtime, downtime, lost time, and spare time are calculated.
4. For kept rows, the effective print interval is clipped to the window:
   - effective start = later of actual start and `00:00`
   - effective end = earlier of actual end and `04:00`

### 3. Folder-day capacity units

1. One folder-day capacity unit is one unique `Issue Date + Machine + Folder`.
2. Each active folder-day has `240` available minutes.
3. Active folder-days are derived only after the print-window filter, so a folder is active only if it prints inside `00:00-04:00`.
4. For configured `>||>` twin folder rows, the source folder row is copied to each configured companion folder before active folder-days are derived.

### 4. Runtime calculation

1. For each folder-day, all effective print intervals are sorted.
2. Overlapping or touching intervals are merged so duplicate or concurrent edition rows do not double count runtime.
3. Gross runtime is the sum of merged interval durations.
4. Net runtime is `gross_runtime - total_downtime`, floored at `0`.
5. Runtime is capped at `240` minutes per folder-day or tower-day.

### 5. Waiting, LPR, and changeover

1. The first print interval is compared against the window start and the selected `Last Tiff` time.
2. If the first edition is ready before `00:00`, waiting is `0` and LPR to print start is the time from `00:00` to first print start.
3. If the first edition becomes ready inside the window, waiting is the time from `00:00` to ready time, and LPR to print start is the time from ready time to first print start.
4. For later print intervals, the gap between previous print end and next print start is split into waiting and changeover:
   - waiting is time until the next edition is ready
   - changeover is time from ready time to next print start
5. If the next edition was ready before the previous print ended, the full gap is treated as changeover.
6. Waiting, LPR, and changeover are clamped so they cannot consume more than the real available gap.

### 6. Downtime and reflong downtime

1. `Down Time` rows are matched to print-window rows by `IssueID + Machine + Folder`.
2. Downtime is included only when the matching issue/folder appears inside the `00:00-04:00` print window.
3. Reflong downtime is moved out of normal downtime and into lost time only when:
   - the downtime `IssueID` matches a book-wise `Issue Id`
   - the matching book-wise row has `Reflong = Yes`
   - the downtime `Related` field starts with `Reflong`
4. Normal downtime is `total_downtime - reflong_related_downtime`, floored at `0`.

### 7. Lost time and spare time

1. Lost time is the sum of:
   - waiting time
   - LPR to print start
   - changeover time
   - reflong-related downtime
2. Lost time is capped to the remaining folder capacity after runtime and normal downtime.
3. Spare time is:

```text
240 - runtime - downtime - lost_time
```

4. Spare time is floored at `0` and capped at `240`.
5. The per-folder guardrail is:

```text
runtime + downtime + lost_time + spare_time = 240
```

### 8. Daily plant capacity

1. Folder-day rows are aggregated by `Issue Date`.
2. Daily runtime, downtime, lost time, and spare time are summed from folder-day rows.
3. The plant's daily capacity uses the maximum active folder count seen in the report.
4. Example: if any day reaches 5 active folders, every day uses `5 * 240 = 1200` available minutes.
5. If a day uses fewer than the plant maximum folder count, each unused folder contributes `240` idle minutes to that day.
6. Daily utilization is:

```text
runtime / available_capacity * 100
```

7. Utilization is capped at `100%`.

### 9. Tower breakdown

1. The `General` sheet maps each `IssueID` to one or more towers from `Towers used`.
2. Book-wise print intervals are expanded from issue-level rows to tower-level rows.
3. Tower intervals are merged the same way folder intervals are merged.
4. Tower runtime, waiting time, LPR, changeover, downtime, reflong downtime, and spare time are calculated for each `Issue Date + Machine + Tower`.
5. Each tower-day has a maximum of `240` available minutes.
6. Tower spare time is the remaining capacity after the selected time components.

### 10. Timeframe filters and dashboard summaries

1. The backend returns full-report daily, folder, and tower rows.
2. The frontend filters rows by the selected annual, half-yearly, quarterly, monthly, or custom date range.
3. Summary KPIs are recalculated from the filtered daily rows.
4. For selections longer than 31 days, the daily capacity chart is grouped into weekly bars.
5. Tower and folder utilization charts aggregate selected components across the chosen timeframe.
6. Chart percentages are rounded and capped so no displayed value exceeds `100%`.

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
    "total_idle_time": 480,
    "average_utilization_percentage": 59.72,
    "spare_capacity_percentage": 23.61,
    "idle_capacity_percentage": 6.67,
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
      "buffer_time": 489,
      "idle_time": 480,
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
