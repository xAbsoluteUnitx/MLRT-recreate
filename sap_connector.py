"""SAP data connector.

Supports two import modes:

* **File import** — upload CSV files exported from SAP transactions
  (IH08 for equipment, IP15 for maintenance plans, IW39 for work orders,
  PA20 for technicians). Column names follow the standard SAP field codes
  (EQUNR, KTXT, WARPL, ...) so the same files produced by an SAP user
  running those transactions can be dropped directly into the tool.
* **Mock API pull** — simulates a live SAP connection by generating a
  realistic data set. Useful for demos and for running the tool when no
  SAP system is available.

Real SAP integrations usually go through either an OData service
(S/4HANA Cloud), a SOAP/BAPI call (SAP ERP), or an IDOC drop. The
CSV-based path used here is functionally equivalent to the "flat file"
drop pattern used by many operators that extract data from SAP with
scheduled background jobs.
"""
from __future__ import annotations

import csv
import io
import os
import random
from datetime import datetime, date, timedelta
from typing import Iterable

from models import (
    db,
    Train,
    MaintenancePlan,
    WorkOrder,
    Technician,
    SapImportLog,
)


SAMPLE_DIR = os.path.join(os.path.dirname(__file__), "sample_sap_data")


# ---------------------------------------------------------------------------
# CSV parsing
# ---------------------------------------------------------------------------
def _reader(file_storage_or_text) -> csv.DictReader:
    """Accept a Werkzeug FileStorage, a raw string, or an open file."""
    if hasattr(file_storage_or_text, "read"):
        raw = file_storage_or_text.read()
        if isinstance(raw, bytes):
            raw = raw.decode("utf-8-sig")
    else:
        raw = file_storage_or_text
    return csv.DictReader(io.StringIO(raw))


def _parse_int(value):
    if value in (None, ""):
        return None
    try:
        return int(float(value))
    except (TypeError, ValueError):
        return None


def _parse_float(value):
    if value in (None, ""):
        return None
    try:
        return float(value)
    except (TypeError, ValueError):
        return None


def _parse_date(value):
    if not value:
        return None
    for fmt in ("%Y-%m-%d", "%d.%m.%Y", "%Y%m%d", "%m/%d/%Y"):
        try:
            return datetime.strptime(value, fmt).date()
        except ValueError:
            continue
    return None


def _parse_datetime(value):
    if not value:
        return None
    for fmt in ("%Y-%m-%dT%H:%M:%S", "%Y-%m-%d %H:%M:%S", "%Y-%m-%d", "%d.%m.%Y %H:%M", "%d.%m.%Y"):
        try:
            return datetime.strptime(value, fmt)
        except ValueError:
            continue
    d = _parse_date(value)
    return datetime.combine(d, datetime.min.time()) if d else None


# ---------------------------------------------------------------------------
# Equipment (trains) — IH08
# ---------------------------------------------------------------------------
EQUIPMENT_COLUMNS = [
    "EQUNR",            # Equipment number
    "KTXT",             # Description
    "TYPBZ",            # Type / model
    "BAUJJ",            # Year of construction
    "SWERK",            # Maintenance plant / depot
    "STATUS",           # Status text
    "ZZLAUFLEISTUNG",   # Custom field: mileage in km
    "ANSDT",            # Acquisition date
]


def import_equipment(source) -> SapImportLog:
    created = updated = skipped = 0
    try:
        for row in _reader(source):
            equnr = (row.get("EQUNR") or "").strip()
            if not equnr:
                skipped += 1
                continue
            train = Train.query.filter_by(equipment_number=equnr).first()
            if train is None:
                train = Train(equipment_number=equnr)
                db.session.add(train)
                created += 1
            else:
                updated += 1
            train.name = (row.get("KTXT") or train.name or equnr).strip()
            train.model = (row.get("TYPBZ") or "").strip() or train.model
            train.year_built = _parse_int(row.get("BAUJJ")) or train.year_built
            train.depot = (row.get("SWERK") or "").strip() or train.depot
            status = (row.get("STATUS") or "").strip().lower()
            if status:
                train.status = status if status in ("active", "in_workshop", "standby", "retired") else "active"
            mileage = _parse_int(row.get("ZZLAUFLEISTUNG"))
            if mileage is not None:
                train.current_mileage_km = mileage
            acq = _parse_date(row.get("ANSDT"))
            if acq:
                train.acquired_on = acq
            train.last_sap_sync = datetime.utcnow()
        log = SapImportLog(
            source="file",
            object_type="equipment",
            records_created=created,
            records_updated=updated,
            records_skipped=skipped,
            status="success",
            message=f"Imported {created + updated} equipment records (created {created}, updated {updated}).",
        )
        db.session.add(log)
        db.session.commit()
        return log
    except Exception as e:
        db.session.rollback()
        log = SapImportLog(
            source="file",
            object_type="equipment",
            status="error",
            message=f"Import failed: {e}",
        )
        db.session.add(log)
        db.session.commit()
        return log


# ---------------------------------------------------------------------------
# Maintenance plans — IP15
# ---------------------------------------------------------------------------
PLAN_COLUMNS = [
    "WARPL",            # Maintenance plan number
    "EQUNR",            # Equipment number (to link)
    "MPTXT",            # Plan description
    "TASK_TYPE",        # inspection / preventive / corrective / overhaul
    "ZYKLUS_DAYS",      # Cycle length in days
    "ZYKLUS_KM",        # Cycle length in km
    "ARBEI",            # Planned work duration (hours)
    "PRIORITY",
    "LAST_DONE",        # Last performed date
    "LAST_KM",          # Mileage at last service
]


def import_plans(source) -> SapImportLog:
    created = updated = skipped = 0
    try:
        for row in _reader(source):
            warpl = (row.get("WARPL") or "").strip()
            equnr = (row.get("EQUNR") or "").strip()
            if not warpl or not equnr:
                skipped += 1
                continue
            train = Train.query.filter_by(equipment_number=equnr).first()
            if train is None:
                skipped += 1
                continue
            plan = MaintenancePlan.query.filter_by(plan_number=warpl).first()
            if plan is None:
                plan = MaintenancePlan(plan_number=warpl, train_id=train.id,
                                       description=(row.get("MPTXT") or warpl).strip())
                db.session.add(plan)
                created += 1
            else:
                updated += 1
                plan.train_id = train.id
                if row.get("MPTXT"):
                    plan.description = row["MPTXT"].strip()
            plan.task_type = (row.get("TASK_TYPE") or plan.task_type or "preventive").strip().lower()
            plan.interval_days = _parse_int(row.get("ZYKLUS_DAYS")) or plan.interval_days
            plan.interval_km = _parse_int(row.get("ZYKLUS_KM")) or plan.interval_km
            plan.duration_hours = _parse_float(row.get("ARBEI")) or plan.duration_hours or 4.0
            if row.get("PRIORITY"):
                plan.priority = row["PRIORITY"].strip()
            last_done = _parse_date(row.get("LAST_DONE"))
            if last_done:
                plan.last_performed_on = last_done
            last_km = _parse_int(row.get("LAST_KM"))
            if last_km is not None:
                plan.last_performed_km = last_km
            plan.active = True
        log = SapImportLog(
            source="file",
            object_type="plans",
            records_created=created,
            records_updated=updated,
            records_skipped=skipped,
            status="success",
            message=f"Imported {created + updated} maintenance plans.",
        )
        db.session.add(log)
        db.session.commit()
        return log
    except Exception as e:
        db.session.rollback()
        log = SapImportLog(source="file", object_type="plans",
                           status="error", message=f"Import failed: {e}")
        db.session.add(log)
        db.session.commit()
        return log


# ---------------------------------------------------------------------------
# Work orders — IW39
# ---------------------------------------------------------------------------
ORDER_COLUMNS = [
    "AUFNR",            # Order number
    "EQUNR",            # Equipment number
    "WARPL",            # Linked maintenance plan (optional)
    "KTEXT",            # Short text
    "AUART",            # Order type
    "STATUS",           # open / scheduled / in_progress / completed
    "PRIORITY",
    "GSTRP",            # Scheduled start
    "GLTRP",            # Scheduled end
    "ARBEI",            # Planned hours
    "PERNR",            # Responsible employee number
]


def _map_sap_status(status: str) -> str:
    if not status:
        return "open"
    s = status.strip().upper()
    mapping = {
        "CRTD": "open", "OPEN": "open",
        "REL": "scheduled", "SCHED": "scheduled", "SCHEDULED": "scheduled",
        "IN_PROGRESS": "in_progress", "INPROG": "in_progress", "STRT": "in_progress",
        "TECO": "completed", "CLSD": "completed", "COMPLETED": "completed", "DONE": "completed",
        "CANC": "cancelled", "CANCELLED": "cancelled",
    }
    return mapping.get(s, s.lower() if s.lower() in
                      ("open", "scheduled", "in_progress", "completed", "cancelled") else "open")


def import_orders(source) -> SapImportLog:
    created = updated = skipped = 0
    try:
        for row in _reader(source):
            aufnr = (row.get("AUFNR") or "").strip()
            equnr = (row.get("EQUNR") or "").strip()
            if not aufnr or not equnr:
                skipped += 1
                continue
            train = Train.query.filter_by(equipment_number=equnr).first()
            if train is None:
                skipped += 1
                continue
            order = WorkOrder.query.filter_by(order_number=aufnr).first()
            if order is None:
                order = WorkOrder(order_number=aufnr, train_id=train.id,
                                  description=(row.get("KTEXT") or aufnr).strip())
                db.session.add(order)
                created += 1
            else:
                updated += 1
                order.train_id = train.id
                if row.get("KTEXT"):
                    order.description = row["KTEXT"].strip()

            warpl = (row.get("WARPL") or "").strip()
            if warpl:
                plan = MaintenancePlan.query.filter_by(plan_number=warpl).first()
                order.plan_id = plan.id if plan else None

            order.order_type = (row.get("AUART") or order.order_type or "PM01").strip()
            order.status = _map_sap_status(row.get("STATUS") or order.status)
            order.priority = (row.get("PRIORITY") or order.priority or "3-normal").strip()
            order.scheduled_start = _parse_datetime(row.get("GSTRP")) or order.scheduled_start
            order.scheduled_end = _parse_datetime(row.get("GLTRP")) or order.scheduled_end
            order.estimated_hours = _parse_float(row.get("ARBEI")) or order.estimated_hours

            pernr = (row.get("PERNR") or "").strip()
            if pernr:
                tech = Technician.query.filter_by(employee_id=pernr).first()
                order.technician_id = tech.id if tech else None

            order.sap_synced_at = datetime.utcnow()
        log = SapImportLog(
            source="file",
            object_type="orders",
            records_created=created,
            records_updated=updated,
            records_skipped=skipped,
            status="success",
            message=f"Imported {created + updated} work orders.",
        )
        db.session.add(log)
        db.session.commit()
        return log
    except Exception as e:
        db.session.rollback()
        log = SapImportLog(source="file", object_type="orders",
                           status="error", message=f"Import failed: {e}")
        db.session.add(log)
        db.session.commit()
        return log


# ---------------------------------------------------------------------------
# Technicians — PA20 / HR mini-master
# ---------------------------------------------------------------------------
TECHNICIAN_COLUMNS = ["PERNR", "NAME", "QUALIFICATION", "SWERK", "ACTIVE"]


def import_technicians(source) -> SapImportLog:
    created = updated = skipped = 0
    try:
        for row in _reader(source):
            pernr = (row.get("PERNR") or "").strip()
            if not pernr:
                skipped += 1
                continue
            tech = Technician.query.filter_by(employee_id=pernr).first()
            if tech is None:
                tech = Technician(employee_id=pernr, name=(row.get("NAME") or pernr).strip())
                db.session.add(tech)
                created += 1
            else:
                updated += 1
                if row.get("NAME"):
                    tech.name = row["NAME"].strip()
            tech.qualification = (row.get("QUALIFICATION") or tech.qualification or "").strip()
            tech.depot = (row.get("SWERK") or tech.depot or "").strip()
            active = (row.get("ACTIVE") or "").strip().lower()
            if active in ("false", "0", "no", "n", "inactive"):
                tech.active = False
            else:
                tech.active = True
        log = SapImportLog(
            source="file",
            object_type="technicians",
            records_created=created,
            records_updated=updated,
            records_skipped=skipped,
            status="success",
            message=f"Imported {created + updated} technicians.",
        )
        db.session.add(log)
        db.session.commit()
        return log
    except Exception as e:
        db.session.rollback()
        log = SapImportLog(source="file", object_type="technicians",
                           status="error", message=f"Import failed: {e}")
        db.session.add(log)
        db.session.commit()
        return log


# ---------------------------------------------------------------------------
# Mock SAP data generator
# ---------------------------------------------------------------------------
MODELS = [
    ("ICE 4", "Frankfurt HBF", 2018),
    ("ICE 3", "Munich HBF", 2014),
    ("IC 2 (Twindexx)", "Hamburg HBF", 2019),
    ("Desiro HC", "Stuttgart HBF", 2020),
    ("Vectron Loco", "Nuremberg HBF", 2021),
    ("Coradia Stream", "Berlin HBF", 2022),
]

PLAN_TEMPLATES = [
    # (description, task_type, interval_days, interval_km, hours, priority)
    ("Daily visual inspection",            "inspection",  1,     None,    0.5, "3-normal"),
    ("Weekly safety inspection",           "inspection",  7,     None,    2,   "2-high"),
    ("Monthly interior & HVAC check",      "preventive",  30,    None,    4,   "3-normal"),
    ("Brake system overhaul",              "overhaul",    None,  150_000, 12,  "1-critical"),
    ("Bogie inspection",                   "preventive",  180,   100_000, 8,   "2-high"),
    ("Wheel profile measurement",          "preventive",  None,  50_000,  3,   "2-high"),
    ("Pantograph & catenary contact test", "preventive",  90,    None,    2,   "3-normal"),
    ("Traction motor overhaul",            "overhaul",    None,  500_000, 24,  "1-critical"),
    ("Fire-safety certification",          "inspection",  365,   None,    6,   "1-critical"),
]

TECH_NAMES = [
    ("10001001", "Klaus Müller",      "brakes & bogies"),
    ("10001002", "Ingrid Bauer",      "traction electrics"),
    ("10001003", "Jan Novak",         "HVAC"),
    ("10001004", "Sofia Rossi",       "doors & pneumatics"),
    ("10001005", "Peter Johansson",   "wheelsets"),
    ("10001006", "Anne Dupont",       "diagnostics"),
    ("10001007", "Marek Kowalski",    "general mechanic"),
    ("10001008", "Helena Schmidt",    "electrician"),
]


def _random_today_minus(days_min, days_max):
    return date.today() - timedelta(days=random.randint(days_min, days_max))


def build_mock_dataset(num_trains=10, rng: random.Random | None = None) -> dict[str, str]:
    """Generate in-memory CSV strings for all four object types."""
    rng = rng or random.Random(42)

    # --- trains --------------------------------------------------------------
    trains_rows = []
    for i in range(num_trains):
        model, depot, base_year = rng.choice(MODELS)
        eq = f"T-{model.split()[0][:3].upper()}-{4000 + i:04d}"
        trains_rows.append({
            "EQUNR": eq,
            "KTXT": f"{model} unit {4000 + i}",
            "TYPBZ": model,
            "BAUJJ": base_year - rng.randint(0, 6),
            "SWERK": depot,
            "STATUS": rng.choices(
                ["active", "active", "active", "standby", "in_workshop"],
                k=1,
            )[0],
            "ZZLAUFLEISTUNG": rng.randint(80_000, 1_900_000),
            "ANSDT": (date.today() - timedelta(days=rng.randint(200, 3000))).isoformat(),
        })

    # --- technicians ---------------------------------------------------------
    tech_rows = [
        {
            "PERNR": pernr,
            "NAME": name,
            "QUALIFICATION": qual,
            "SWERK": rng.choice(MODELS)[1],
            "ACTIVE": "true",
        }
        for pernr, name, qual in TECH_NAMES
    ]

    # --- plans ---------------------------------------------------------------
    plan_rows = []
    plan_counter = 1
    for t in trains_rows:
        # Each train gets 4–6 plans from the template list
        picked = rng.sample(PLAN_TEMPLATES, k=rng.randint(4, 6))
        for desc, ttype, days, km, hours, prio in picked:
            last_done = _random_today_minus(5, 200)
            last_km = max(0, t["ZZLAUFLEISTUNG"] - rng.randint(2_000, 90_000))
            plan_rows.append({
                "WARPL": f"MP-{plan_counter:05d}",
                "EQUNR": t["EQUNR"],
                "MPTXT": desc,
                "TASK_TYPE": ttype,
                "ZYKLUS_DAYS": days or "",
                "ZYKLUS_KM": km or "",
                "ARBEI": hours,
                "PRIORITY": prio,
                "LAST_DONE": last_done.isoformat(),
                "LAST_KM": last_km,
            })
            plan_counter += 1

    # --- work orders ---------------------------------------------------------
    order_rows = []
    order_counter = 1
    statuses = ["open", "scheduled", "scheduled", "in_progress", "completed", "completed"]
    for t in trains_rows:
        # 2–4 orders per train
        for _ in range(rng.randint(2, 4)):
            plan = rng.choice([p for p in plan_rows if p["EQUNR"] == t["EQUNR"]])
            status = rng.choice(statuses)
            start = datetime.utcnow() + timedelta(days=rng.randint(-30, 30), hours=rng.randint(0, 23))
            end = start + timedelta(hours=plan["ARBEI"])
            tech = rng.choice(tech_rows)
            order_rows.append({
                "AUFNR": f"WO-{1_000_000 + order_counter}",
                "EQUNR": t["EQUNR"],
                "WARPL": plan["WARPL"],
                "KTEXT": plan["MPTXT"],
                "AUART": "PM01" if plan["TASK_TYPE"] != "corrective" else "PM02",
                "STATUS": status,
                "PRIORITY": plan["PRIORITY"],
                "GSTRP": start.strftime("%Y-%m-%dT%H:%M:%S"),
                "GLTRP": end.strftime("%Y-%m-%dT%H:%M:%S"),
                "ARBEI": plan["ARBEI"],
                "PERNR": tech["PERNR"],
            })
            order_counter += 1

    return {
        "equipment": _rows_to_csv(EQUIPMENT_COLUMNS, trains_rows),
        "technicians": _rows_to_csv(TECHNICIAN_COLUMNS, tech_rows),
        "plans": _rows_to_csv(PLAN_COLUMNS, plan_rows),
        "orders": _rows_to_csv(ORDER_COLUMNS, order_rows),
    }


def _rows_to_csv(columns: Iterable[str], rows: Iterable[dict]) -> str:
    out = io.StringIO()
    writer = csv.DictWriter(out, fieldnames=list(columns), extrasaction="ignore")
    writer.writeheader()
    for r in rows:
        writer.writerow(r)
    return out.getvalue()


def write_sample_files(num_trains=10) -> dict[str, str]:
    os.makedirs(SAMPLE_DIR, exist_ok=True)
    data = build_mock_dataset(num_trains=num_trains)
    paths = {}
    for key, csv_text in data.items():
        path = os.path.join(SAMPLE_DIR, f"{key}.csv")
        with open(path, "w", encoding="utf-8") as fh:
            fh.write(csv_text)
        paths[key] = path
    return paths


def ensure_sample_files(num_trains=10) -> dict[str, str]:
    """Generate sample files if they do not exist already."""
    paths = {k: os.path.join(SAMPLE_DIR, f"{k}.csv")
             for k in ("equipment", "technicians", "plans", "orders")}
    if not all(os.path.exists(p) for p in paths.values()):
        write_sample_files(num_trains=num_trains)
    return paths


def run_mock_pull(num_trains=10) -> list[SapImportLog]:
    """Import a freshly generated mock dataset as if pulled from SAP."""
    data = build_mock_dataset(num_trains=num_trains)
    logs = []
    for object_type, parser in (
        ("equipment", import_equipment),
        ("technicians", import_technicians),
        ("plans", import_plans),
        ("orders", import_orders),
    ):
        log = parser(io.StringIO(data[object_type]))
        log.source = "mock_api"
        db.session.commit()
        logs.append(log)
    return logs


# ---------------------------------------------------------------------------
# Dispatcher used by the upload endpoint
# ---------------------------------------------------------------------------
IMPORTERS = {
    "equipment": import_equipment,
    "plans": import_plans,
    "orders": import_orders,
    "technicians": import_technicians,
}
