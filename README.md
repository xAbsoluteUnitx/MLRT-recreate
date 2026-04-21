# Rolling-Stock Maintenance Planner

A web application that plans, schedules, and tracks rolling-stock maintenance
for a train operator. Master data, maintenance plans, and work orders are
imported from SAP PM.

## Features

- **Fleet dashboard** — KPIs for fleet availability, open work orders,
  overdue tasks, and completed jobs in the current month.
- **Train / equipment master** — equipment number, model, depot, mileage,
  status, acquisition date, and last SAP sync timestamp.
- **Maintenance plans** — time-based and mileage-based cycles with
  automatic "overdue / due soon / ok" calculation per train.
- **Work orders** — create, schedule, assign to a technician, track
  actual vs. estimated hours, and capture completion notes. Linked plans
  are automatically marked "performed" when a work order is completed.
- **Calendar schedule** — 6-week Gantt-style calendar colored by priority.
- **Auto-scheduling** — one click generates scheduled work orders for
  every overdue or upcoming plan that doesn't already have one.
- **SAP import** — upload CSV flat-file exports from SAP PM transactions,
  or run the built-in mock connector to generate a realistic dataset.
- **JSON API** — `/api/trains`, `/api/work-orders`, `/api/plans`,
  `/api/upcoming` for external integrations.
- **CSV export** — `/export/work-orders.csv`.

## Quick start

```bash
pip install -r requirements.txt
python app.py
```

Open <http://localhost:5000>. On first start the app seeds itself with a
realistic mock dataset (10 trains, their plans, technicians and work
orders) so every page is immediately populated.

## SAP data model

The importer supports four SAP object types. Column names follow standard
SAP PM field codes — any CSV produced by a user running the matching
SAP transaction can be dropped into the uploader.

| Object       | SAP tx | File name        | Required columns |
|--------------|--------|------------------|------------------|
| Equipment    | IH08   | `equipment.csv`  | `EQUNR, KTXT, TYPBZ, BAUJJ, SWERK, STATUS, ZZLAUFLEISTUNG, ANSDT` |
| Plans        | IP15   | `plans.csv`      | `WARPL, EQUNR, MPTXT, TASK_TYPE, ZYKLUS_DAYS, ZYKLUS_KM, ARBEI, PRIORITY, LAST_DONE, LAST_KM` |
| Work orders  | IW39   | `orders.csv`     | `AUFNR, EQUNR, WARPL, KTEXT, AUART, STATUS, PRIORITY, GSTRP, GLTRP, ARBEI, PERNR` |
| Technicians  | PA20   | `technicians.csv`| `PERNR, NAME, QUALIFICATION, SWERK, ACTIVE` |

SAP work-order status codes are mapped automatically:
`CRTD → open`, `REL → scheduled`, `STRT → in_progress`, `TECO/CLSD → completed`.

Sample CSVs are downloadable from the **SAP import** page in the UI.

## Architecture

```
app.py             Flask routes, scheduling logic, app factory
models.py          SQLAlchemy models
sap_connector.py   CSV parsers + mock SAP data generator
templates/         Jinja2 templates
static/css/        Dark industrial theme
sample_sap_data/   Auto-generated CSV samples
maintenance.db     SQLite database (created on first run)
```

A background job runs once an hour to auto-schedule overdue / due-soon
plans that don't already have an open work order.

## Replacing the mock connector with real SAP

To point at a real SAP system, implement an OData client in
`sap_connector.py` that yields the same dict rows as the CSV parsers and
feed them into the existing `import_*` functions. All downstream logic
(scheduling, KPIs, UI) will work unchanged.

## License

MIT
