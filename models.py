"""SQLAlchemy data models for the train maintenance planner."""
from datetime import datetime, date, timedelta

from flask_sqlalchemy import SQLAlchemy

db = SQLAlchemy()


TRAIN_STATUSES = ("active", "in_workshop", "standby", "retired")
ORDER_STATUSES = ("open", "scheduled", "in_progress", "completed", "cancelled")
PRIORITIES = ("1-critical", "2-high", "3-normal", "4-low")
TASK_TYPES = ("inspection", "preventive", "corrective", "overhaul")


class Train(db.Model):
    __tablename__ = "train"

    id = db.Column(db.Integer, primary_key=True)
    equipment_number = db.Column(db.String(32), unique=True, nullable=False)  # SAP EQUNR
    name = db.Column(db.String(128), nullable=False)                          # SAP KTXT
    model = db.Column(db.String(64))                                          # SAP TYPBZ
    year_built = db.Column(db.Integer)                                        # SAP BAUJJ
    depot = db.Column(db.String(64))                                          # SAP SWERK
    status = db.Column(db.String(32), default="active")
    current_mileage_km = db.Column(db.Integer, default=0)
    acquired_on = db.Column(db.Date)
    last_sap_sync = db.Column(db.DateTime)

    plans = db.relationship("MaintenancePlan", backref="train", cascade="all, delete-orphan")
    orders = db.relationship("WorkOrder", backref="train", cascade="all, delete-orphan")

    def to_dict(self):
        return {
            "id": self.id,
            "equipment_number": self.equipment_number,
            "name": self.name,
            "model": self.model,
            "year_built": self.year_built,
            "depot": self.depot,
            "status": self.status,
            "current_mileage_km": self.current_mileage_km,
            "acquired_on": self.acquired_on.isoformat() if self.acquired_on else None,
            "last_sap_sync": self.last_sap_sync.isoformat() if self.last_sap_sync else None,
        }


class MaintenancePlan(db.Model):
    __tablename__ = "maintenance_plan"

    id = db.Column(db.Integer, primary_key=True)
    plan_number = db.Column(db.String(32), unique=True, nullable=False)   # SAP WARPL
    train_id = db.Column(db.Integer, db.ForeignKey("train.id"), nullable=False)
    task_type = db.Column(db.String(32), default="preventive")
    description = db.Column(db.String(255), nullable=False)               # SAP MPTXT
    interval_days = db.Column(db.Integer)                                 # time-based cycle
    interval_km = db.Column(db.Integer)                                   # mileage-based cycle
    duration_hours = db.Column(db.Float, default=4.0)                     # SAP ARBEI
    priority = db.Column(db.String(16), default="3-normal")
    last_performed_on = db.Column(db.Date)
    last_performed_km = db.Column(db.Integer, default=0)
    active = db.Column(db.Boolean, default=True)

    def next_due_date(self):
        if not self.interval_days or not self.last_performed_on:
            return None
        return self.last_performed_on + timedelta(days=self.interval_days)

    def next_due_km(self):
        if not self.interval_km:
            return None
        return (self.last_performed_km or 0) + self.interval_km

    def status(self, train_mileage_km, today=None):
        today = today or date.today()
        date_due = self.next_due_date()
        km_due = self.next_due_km()

        date_overdue = bool(date_due and date_due < today)
        km_overdue = bool(km_due and train_mileage_km >= km_due)
        if date_overdue or km_overdue:
            return "overdue"

        if date_due and (date_due - today).days <= 14:
            return "due_soon"
        if km_due and (km_due - train_mileage_km) <= 2000:
            return "due_soon"
        return "ok"

    def to_dict(self):
        return {
            "id": self.id,
            "plan_number": self.plan_number,
            "train_id": self.train_id,
            "task_type": self.task_type,
            "description": self.description,
            "interval_days": self.interval_days,
            "interval_km": self.interval_km,
            "duration_hours": self.duration_hours,
            "priority": self.priority,
            "last_performed_on": self.last_performed_on.isoformat() if self.last_performed_on else None,
            "last_performed_km": self.last_performed_km,
            "active": self.active,
            "next_due_date": self.next_due_date().isoformat() if self.next_due_date() else None,
            "next_due_km": self.next_due_km(),
        }


class Technician(db.Model):
    __tablename__ = "technician"

    id = db.Column(db.Integer, primary_key=True)
    employee_id = db.Column(db.String(16), unique=True, nullable=False)   # SAP PERNR
    name = db.Column(db.String(128), nullable=False)
    qualification = db.Column(db.String(64))
    depot = db.Column(db.String(64))
    active = db.Column(db.Boolean, default=True)

    orders = db.relationship("WorkOrder", backref="technician")

    def to_dict(self):
        return {
            "id": self.id,
            "employee_id": self.employee_id,
            "name": self.name,
            "qualification": self.qualification,
            "depot": self.depot,
            "active": self.active,
        }


class WorkOrder(db.Model):
    __tablename__ = "work_order"

    id = db.Column(db.Integer, primary_key=True)
    order_number = db.Column(db.String(32), unique=True, nullable=False)  # SAP AUFNR
    train_id = db.Column(db.Integer, db.ForeignKey("train.id"), nullable=False)
    plan_id = db.Column(db.Integer, db.ForeignKey("maintenance_plan.id"))
    technician_id = db.Column(db.Integer, db.ForeignKey("technician.id"))

    description = db.Column(db.String(255), nullable=False)               # SAP KTEXT
    order_type = db.Column(db.String(16), default="PM01")                 # SAP AUART
    status = db.Column(db.String(16), default="open")
    priority = db.Column(db.String(16), default="3-normal")

    scheduled_start = db.Column(db.DateTime)                              # SAP GSTRP
    scheduled_end = db.Column(db.DateTime)                                # SAP GLTRP
    actual_start = db.Column(db.DateTime)
    actual_end = db.Column(db.DateTime)

    estimated_hours = db.Column(db.Float, default=0)                      # SAP ARBEI
    actual_hours = db.Column(db.Float, default=0)                         # SAP ISMNW

    created_at = db.Column(db.DateTime, default=datetime.utcnow)
    completion_notes = db.Column(db.Text)
    sap_synced_at = db.Column(db.DateTime)

    plan = db.relationship("MaintenancePlan", backref="orders")

    @property
    def is_overdue(self):
        if self.status in ("completed", "cancelled"):
            return False
        return bool(self.scheduled_end and self.scheduled_end < datetime.utcnow())

    def to_dict(self):
        return {
            "id": self.id,
            "order_number": self.order_number,
            "train_id": self.train_id,
            "train_name": self.train.name if self.train else None,
            "train_equipment_number": self.train.equipment_number if self.train else None,
            "plan_id": self.plan_id,
            "technician_id": self.technician_id,
            "technician_name": self.technician.name if self.technician else None,
            "description": self.description,
            "order_type": self.order_type,
            "status": self.status,
            "priority": self.priority,
            "scheduled_start": self.scheduled_start.isoformat() if self.scheduled_start else None,
            "scheduled_end": self.scheduled_end.isoformat() if self.scheduled_end else None,
            "actual_start": self.actual_start.isoformat() if self.actual_start else None,
            "actual_end": self.actual_end.isoformat() if self.actual_end else None,
            "estimated_hours": self.estimated_hours,
            "actual_hours": self.actual_hours,
            "created_at": self.created_at.isoformat() if self.created_at else None,
            "completion_notes": self.completion_notes,
            "is_overdue": self.is_overdue,
        }


class SapImportLog(db.Model):
    __tablename__ = "sap_import_log"

    id = db.Column(db.Integer, primary_key=True)
    imported_at = db.Column(db.DateTime, default=datetime.utcnow)
    source = db.Column(db.String(32))            # "file", "mock_api"
    object_type = db.Column(db.String(32))       # "equipment", "plans", "orders", "technicians"
    records_created = db.Column(db.Integer, default=0)
    records_updated = db.Column(db.Integer, default=0)
    records_skipped = db.Column(db.Integer, default=0)
    status = db.Column(db.String(16), default="success")
    message = db.Column(db.Text)

    def to_dict(self):
        return {
            "id": self.id,
            "imported_at": self.imported_at.isoformat() if self.imported_at else None,
            "source": self.source,
            "object_type": self.object_type,
            "records_created": self.records_created,
            "records_updated": self.records_updated,
            "records_skipped": self.records_skipped,
            "status": self.status,
            "message": self.message,
        }
