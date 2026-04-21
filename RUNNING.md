# How to run the Train Maintenance Planner

A step-by-step guide from a fresh checkout to a running app with imported
SAP data.

---

## 1. Prerequisites

You need:

- **Python 3.10 or newer** (`python3 --version` to check)
- **pip** (`pip --version`)
- A free TCP port — the app uses **5000** by default
- A modern browser (Chrome, Firefox, Safari, Edge)

No database server is needed — SQLite is used and the file is created
automatically.

---

## 2. Get the code

```bash
git clone https://github.com/xAbsoluteUnitx/MLRT-recreate.git
cd MLRT-recreate
git checkout claude/train-maintenance-planner-foXGN
```

---

## 3. Create a virtual environment (recommended)

```bash
python3 -m venv .venv
source .venv/bin/activate        # Windows: .venv\Scripts\activate
```

You should see `(.venv)` at the start of your prompt afterwards.

---

## 4. Install the dependencies

```bash
pip install -r requirements.txt
```

This installs three packages:

| Package           | Purpose                                |
|-------------------|----------------------------------------|
| `flask`           | Web framework                          |
| `flask-sqlalchemy`| ORM for the SQLite database            |
| `apscheduler`     | Hourly auto-schedule background job    |

---

## 5. Start the app

```bash
python app.py
```

You should see output similar to:

```
 * Serving Flask app 'app'
 * Debug mode: on
 * Running on http://127.0.0.1:5000
 * Running on http://192.0.2.2:5000
Press CTRL+C to quit
```

On the **first** startup the app will:

1. Create the SQLite database `maintenance.db` in the project folder.
2. Create `sample_sap_data/` with four CSVs matching the SAP PM export format.
3. Run the **mock SAP connector** once to seed 10 trains, their
   maintenance plans, technicians, and work orders so every page has
   real-looking data immediately.

---

## 6. Open the dashboard

Open <http://localhost:5000> in your browser. You should see the
Operations Dashboard with KPIs (fleet size, open/overdue work orders,
completed this month) and two tables — "Overdue maintenance" and
"Due in the next 30 days".

---

## 7. Walk through the UI

Navigate via the top bar:

| Tab            | What you can do                                                 |
|----------------|-----------------------------------------------------------------|
| **Dashboard**  | KPIs, overdue list, due-soon list, recent work orders           |
| **Fleet**      | Filter/search trains; click any to open its detail page         |
| **Schedule**   | 6-week calendar of scheduled work orders (colored by priority)  |
| **Work orders**| List, filter, create, export CSV                                |
| **SAP import** | Upload CSVs from SAP, run mock pull, view import history        |

Try these actions to see the app end-to-end:

1. **Fleet → click a train.** You'll see its equipment master, plans
   with "overdue / due soon / ok" status, and work-order history.
2. **Update its mileage** — if a plan has a km-based cycle, its status
   will flip to "due_soon" or "overdue" as soon as the threshold is hit.
3. **Schedule a plan** using the inline form on the train detail page
   (pick a date and a technician, then click "Schedule"). A new work
   order is created.
4. **Work orders → pick one → change Status to "in_progress" → Save.**
   The actual start time is captured automatically.
5. **Change it to "completed"**, type actual hours and notes, Save.
   The linked maintenance plan's "last performed" fields are updated —
   go back to the train's plan table to see the status flip back to OK.
6. **Dashboard → "Auto-schedule due plans (14 d)"** — one click creates
   scheduled work orders for every overdue/due-soon plan that doesn't
   already have one open.

---

## 8. Import SAP data

There are two ways.

### Option A — Upload a CSV exported from SAP

1. In SAP, run one of the standard PM transactions and export the
   result to a CSV file:
   - **IH08** for equipment master → `equipment.csv`
   - **IP15** for maintenance plans → `plans.csv`
   - **IW39** for work orders → `orders.csv`
   - **PA20** for technicians → `technicians.csv`
2. In the planner, open **SAP import**, pick the object type, choose
   your file, click **Upload**.
3. Check the "Import history" table at the bottom — it shows how many
   records were created, updated, or skipped, and any error messages.

The CSV column names must match SAP field codes (`EQUNR`, `WARPL`,
`AUFNR`, etc.). Download the sample files on the SAP import page to
see the exact format. SAP work-order status codes are mapped
automatically (`CRTD` → open, `REL` → scheduled, `STRT` → in_progress,
`TECO`/`CLSD` → completed).

### Option B — Mock SAP pull (for demos / no SAP access)

1. Open **SAP import**.
2. Under "Live SAP pull (mock)" pick the number of trains (1–50).
3. Click **Run mock pull now**.

A realistic dataset is generated and imported exactly like a real SAP
pull. The "Import history" table records each run.

---

## 9. Use the JSON API / CSV export

For external tools or quick inspection:

```bash
curl http://localhost:5000/api/trains
curl http://localhost:5000/api/work-orders
curl http://localhost:5000/api/plans
curl 'http://localhost:5000/api/upcoming?days=30'
curl -O http://localhost:5000/export/work-orders.csv
```

---

## 10. Stop the app

Press **Ctrl+C** in the terminal where `python app.py` is running.

The database is persistent — next time you run it, all your trains,
plans, and work orders are still there.

---

## 11. Reset the data (optional)

To start fresh:

```bash
rm maintenance.db
python app.py
```

The app re-seeds itself with a new mock dataset.

---

## 12. Change the port / secret key (optional)

Environment variables are read at startup:

```bash
PORT=8080 SECRET_KEY="something-random" python app.py
```

---

## Troubleshooting

| Symptom                                   | Fix                                                                  |
|-------------------------------------------|----------------------------------------------------------------------|
| `ModuleNotFoundError: flask`              | You didn't activate the venv or skipped `pip install`. Re-do step 3 & 4. |
| Port 5000 is already in use               | `PORT=5050 python app.py`                                            |
| Upload says "Skipped" for every row       | CSV column names don't match the SAP field codes. Check the sample file. |
| Dashboard is empty                        | Run the mock pull from **SAP import**, or upload your own CSVs.      |
| Want to run behind nginx / gunicorn       | `pip install gunicorn` then `gunicorn -w 2 -b 0.0.0.0:5000 app:app`  |
