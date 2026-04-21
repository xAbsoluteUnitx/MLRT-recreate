"""Train maintenance planning tool.

A Flask web application that plans, schedules and tracks rolling-stock
maintenance for a train operator. Data can be imported from SAP (IH08 /
IP15 / IW39 flat-file exports) or pulled from the built-in mock SAP
connector for demos.

Run:
    pip install -r requirements.txt
    python app.py

Then open http://localhost:5000.
"""
import os
from datetime import datetime, timedelta, date

from flask import (
    Flask, render_template, request, redirect, url_for, jsonify, flash,
    send_from_directory, Response, abort,
)
from apscheduler.schedulers.background import BackgroundScheduler

from models import (
    db,
    Train, MaintenancePlan, WorkOrder, Technician, SapImportLog,
    TRAIN_STATUSES, ORDER_STATUSES, PRIORITIES, TASK_TYPES,
)
import sap_connector


BASE_DIR = os.path.dirname(__file__)
DB_PATH = os.path.join(BASE_DIR, "maintenance.db")


def create_app() -> Flask:
    app = Flask(__name__, template_folder="templates", static_folder="static")
    app.config["SQLALCHEMY_DATABASE_URI"] = f"sqlite:///{DB_PATH}"
    app.config["SQLALCHEMY_TRACK_MODIFICATIONS"] = False
    app.config["SECRET_KEY"] = os.environ.get("SECRET_KEY", "dev-train-planner")
    app.config["MAX_CONTENT_LENGTH"] = 16 * 1024 * 1024  # 16 MB upload cap

    db.init_app(app)

    with app.app_context():
        db.create_all()
        sap_connector.ensure_sample_files()
        if Train.query.count() == 0:
            # First start — load the sample dataset so the UI is immediately useful.
            sap_connector.run_mock_pull(num_trains=10)

    _register_routes(app)
    _register_template_filters(app)
    return app


# ---------------------------------------------------------------------------
# Maintenance scheduling logic
# ---------------------------------------------------------------------------
def plans_with_status(train: Train):
    """Return a list of (plan, status, next_due_date, next_due_km) tuples."""
    result = []
    for plan in train.plans:
        st = plan.status(train.current_mileage_km or 0)
        result.append({
            "plan": plan,
            "status": st,
            "next_due_date": plan.next_due_date(),
            "next_due_km": plan.next_due_km(),
        })
    return result


def upcoming_maintenance(days_ahead: int = 30):
    """Return plans that are overdue or due within the horizon, worst first."""
    today = date.today()
    horizon = today + timedelta(days=days_ahead)
    items = []
    for plan in MaintenancePlan.query.filter_by(active=True).all():
        train = plan.train
        if not train or train.status == "retired":
            continue
        status = plan.status(train.current_mileage_km or 0, today)
        if status == "ok":
            continue
        due_date = plan.next_due_date()
        due_km = plan.next_due_km()
        if status == "due_soon" and due_date and due_date > horizon:
            continue
        items.append({
            "plan": plan,
            "train": train,
            "status": status,
            "next_due_date": due_date,
            "next_due_km": due_km,
            "km_to_go": (due_km - (train.current_mileage_km or 0)) if due_km else None,
            "days_to_go": (due_date - today).days if due_date else None,
        })

    def sort_key(item):
        return (
            0 if item["status"] == "overdue" else 1,
            item["days_to_go"] if item["days_to_go"] is not None else 999,
            item["km_to_go"] if item["km_to_go"] is not None else 10_000_000,
        )

    items.sort(key=sort_key)
    return items


def generate_work_order_from_plan(plan: MaintenancePlan, scheduled_start: datetime,
                                  technician_id: int | None = None) -> WorkOrder:
    last_number = db.session.query(db.func.max(WorkOrder.id)).scalar() or 0
    order_number = f"WO-{1_100_000 + last_number + 1}"
    scheduled_end = scheduled_start + timedelta(hours=plan.duration_hours or 4)
    order = WorkOrder(
        order_number=order_number,
        train_id=plan.train_id,
        plan_id=plan.id,
        technician_id=technician_id,
        description=f"{plan.description} (from plan {plan.plan_number})",
        order_type="PM01" if plan.task_type != "corrective" else "PM02",
        status="scheduled",
        priority=plan.priority,
        scheduled_start=scheduled_start,
        scheduled_end=scheduled_end,
        estimated_hours=plan.duration_hours or 4,
    )
    db.session.add(order)
    db.session.commit()
    return order


def auto_schedule_due_work(horizon_days: int = 14) -> list[WorkOrder]:
    """Create scheduled work orders for every overdue/due-soon plan that
    doesn't already have an open work order."""
    created = []
    for item in upcoming_maintenance(days_ahead=horizon_days):
        plan = item["plan"]
        existing = WorkOrder.query.filter(
            WorkOrder.plan_id == plan.id,
            WorkOrder.status.in_(("open", "scheduled", "in_progress")),
        ).first()
        if existing:
            continue
        # Schedule it on the earlier of (due date) or (today + 2 days).
        start_date = item["next_due_date"] or (date.today() + timedelta(days=2))
        if start_date < date.today():
            start_date = date.today() + timedelta(days=1)
        scheduled_start = datetime.combine(start_date, datetime.min.time().replace(hour=8))
        created.append(generate_work_order_from_plan(plan, scheduled_start))
    return created


# ---------------------------------------------------------------------------
# Flask routes
# ---------------------------------------------------------------------------
def _register_routes(app: Flask):

    # --------- Dashboard ----------------------------------------------------
    @app.route("/")
    def dashboard():
        today = date.today()
        fleet_total = Train.query.count()
        active_count = Train.query.filter_by(status="active").count()
        in_workshop = Train.query.filter_by(status="in_workshop").count()
        standby = Train.query.filter_by(status="standby").count()

        open_orders = WorkOrder.query.filter(
            WorkOrder.status.in_(("open", "scheduled", "in_progress"))
        ).count()
        overdue_orders = [o for o in WorkOrder.query.filter(
            WorkOrder.status.in_(("open", "scheduled", "in_progress"))
        ).all() if o.is_overdue]

        month_start = datetime(today.year, today.month, 1)
        completed_this_month = WorkOrder.query.filter(
            WorkOrder.status == "completed",
            WorkOrder.actual_end >= month_start,
        ).count()

        upcoming = upcoming_maintenance(days_ahead=30)
        upcoming_overdue = [u for u in upcoming if u["status"] == "overdue"]
        upcoming_due_soon = [u for u in upcoming if u["status"] == "due_soon"]

        recent_orders = WorkOrder.query.order_by(WorkOrder.created_at.desc()).limit(8).all()

        return render_template(
            "dashboard.html",
            fleet_total=fleet_total,
            active_count=active_count,
            in_workshop=in_workshop,
            standby=standby,
            open_orders=open_orders,
            overdue_count=len(overdue_orders),
            completed_this_month=completed_this_month,
            upcoming_overdue=upcoming_overdue[:10],
            upcoming_due_soon=upcoming_due_soon[:10],
            recent_orders=recent_orders,
        )

    # --------- Fleet -------------------------------------------------------
    @app.route("/trains")
    def trains_list():
        q = request.args.get("q", "").strip()
        status = request.args.get("status", "").strip()
        depot = request.args.get("depot", "").strip()

        query = Train.query
        if q:
            like = f"%{q}%"
            query = query.filter(db.or_(
                Train.equipment_number.ilike(like),
                Train.name.ilike(like),
                Train.model.ilike(like),
            ))
        if status:
            query = query.filter_by(status=status)
        if depot:
            query = query.filter_by(depot=depot)

        trains = query.order_by(Train.equipment_number).all()
        depots = sorted({t.depot for t in Train.query.all() if t.depot})
        # Overlay the worst plan status per train so the list shows it.
        worst = {}
        for t in trains:
            statuses = [p.status(t.current_mileage_km or 0) for p in t.plans]
            if "overdue" in statuses:
                worst[t.id] = "overdue"
            elif "due_soon" in statuses:
                worst[t.id] = "due_soon"
            else:
                worst[t.id] = "ok"

        return render_template(
            "trains.html",
            trains=trains,
            worst=worst,
            depots=depots,
            q=q, status=status, depot=depot,
            statuses=TRAIN_STATUSES,
        )

    @app.route("/trains/<int:train_id>")
    def train_detail(train_id):
        train = Train.query.get_or_404(train_id)
        plan_statuses = plans_with_status(train)
        orders = WorkOrder.query.filter_by(train_id=train.id)\
            .order_by(WorkOrder.scheduled_start.desc().nullslast()).all()
        return render_template(
            "train_detail.html",
            train=train,
            plan_statuses=plan_statuses,
            orders=orders,
            technicians=Technician.query.filter_by(active=True).order_by(Technician.name).all(),
            now=datetime.utcnow(),
        )

    @app.route("/trains/<int:train_id>/mileage", methods=["POST"])
    def update_mileage(train_id):
        train = Train.query.get_or_404(train_id)
        try:
            new_km = int(request.form["mileage"])
            if new_km < 0 or new_km < (train.current_mileage_km or 0):
                raise ValueError("Mileage can't decrease.")
        except (KeyError, ValueError) as e:
            flash(f"Invalid mileage: {e}", "error")
            return redirect(url_for("train_detail", train_id=train.id))
        train.current_mileage_km = new_km
        db.session.commit()
        flash(f"Mileage updated to {new_km:,} km.", "success")
        return redirect(url_for("train_detail", train_id=train.id))

    @app.route("/trains/<int:train_id>/status", methods=["POST"])
    def update_train_status(train_id):
        train = Train.query.get_or_404(train_id)
        status = request.form.get("status", "").strip()
        if status in TRAIN_STATUSES:
            train.status = status
            db.session.commit()
            flash(f"Status updated to {status}.", "success")
        return redirect(url_for("train_detail", train_id=train.id))

    # --------- Plans / auto-schedule ---------------------------------------
    @app.route("/plans/<int:plan_id>/schedule", methods=["POST"])
    def schedule_plan(plan_id):
        plan = MaintenancePlan.query.get_or_404(plan_id)
        when_str = request.form.get("when")
        try:
            scheduled_start = datetime.fromisoformat(when_str) if when_str else (
                datetime.combine(date.today() + timedelta(days=2),
                                 datetime.min.time().replace(hour=8)))
        except ValueError:
            flash("Invalid date.", "error")
            return redirect(url_for("train_detail", train_id=plan.train_id))
        tech_id = request.form.get("technician_id") or None
        tech_id = int(tech_id) if tech_id else None
        order = generate_work_order_from_plan(plan, scheduled_start, tech_id)
        flash(f"Work order {order.order_number} scheduled.", "success")
        return redirect(url_for("work_order_detail", order_id=order.id))

    @app.route("/plans/auto-schedule", methods=["POST"])
    def auto_schedule():
        created = auto_schedule_due_work(
            horizon_days=int(request.form.get("horizon", "14")))
        flash(f"Created {len(created)} work order(s) from due/overdue plans.", "success")
        return redirect(url_for("work_orders_list"))

    # --------- Work orders --------------------------------------------------
    @app.route("/work-orders")
    def work_orders_list():
        status = request.args.get("status", "").strip()
        priority = request.args.get("priority", "").strip()
        train_id = request.args.get("train_id", "").strip()

        query = WorkOrder.query
        if status:
            query = query.filter_by(status=status)
        if priority:
            query = query.filter_by(priority=priority)
        if train_id:
            query = query.filter_by(train_id=int(train_id))
        orders = query.order_by(WorkOrder.scheduled_start.asc().nullslast()).all()
        trains = Train.query.order_by(Train.equipment_number).all()
        return render_template(
            "work_orders.html",
            orders=orders,
            trains=trains,
            statuses=ORDER_STATUSES,
            priorities=PRIORITIES,
            status=status, priority=priority, train_id=train_id,
            now=datetime.utcnow(),
        )

    @app.route("/work-orders/new", methods=["GET", "POST"])
    def work_order_new():
        if request.method == "POST":
            form = request.form
            train_id = int(form["train_id"])
            plan_id = int(form["plan_id"]) if form.get("plan_id") else None
            tech_id = int(form["technician_id"]) if form.get("technician_id") else None
            try:
                start = datetime.fromisoformat(form["scheduled_start"])
                end = datetime.fromisoformat(form["scheduled_end"]) if form.get("scheduled_end") else start + timedelta(hours=4)
            except ValueError:
                flash("Invalid start/end date-time.", "error")
                return redirect(url_for("work_order_new"))

            last_id = db.session.query(db.func.max(WorkOrder.id)).scalar() or 0
            order = WorkOrder(
                order_number=form.get("order_number") or f"WO-{1_100_000 + last_id + 1}",
                train_id=train_id,
                plan_id=plan_id,
                technician_id=tech_id,
                description=form["description"],
                order_type=form.get("order_type") or "PM01",
                status=form.get("status") or "scheduled",
                priority=form.get("priority") or "3-normal",
                scheduled_start=start,
                scheduled_end=end,
                estimated_hours=float(form.get("estimated_hours") or 4),
            )
            db.session.add(order)
            db.session.commit()
            flash(f"Work order {order.order_number} created.", "success")
            return redirect(url_for("work_order_detail", order_id=order.id))

        return render_template(
            "work_order_form.html",
            trains=Train.query.order_by(Train.equipment_number).all(),
            plans=MaintenancePlan.query.all(),
            technicians=Technician.query.filter_by(active=True).order_by(Technician.name).all(),
            statuses=ORDER_STATUSES,
            priorities=PRIORITIES,
        )

    @app.route("/work-orders/<int:order_id>")
    def work_order_detail(order_id):
        order = WorkOrder.query.get_or_404(order_id)
        return render_template(
            "work_order_detail.html",
            order=order,
            technicians=Technician.query.filter_by(active=True).order_by(Technician.name).all(),
            statuses=ORDER_STATUSES,
            priorities=PRIORITIES,
            now=datetime.utcnow(),
        )

    @app.route("/work-orders/<int:order_id>/update", methods=["POST"])
    def work_order_update(order_id):
        order = WorkOrder.query.get_or_404(order_id)
        form = request.form
        new_status = form.get("status")
        if new_status and new_status in ORDER_STATUSES:
            order.status = new_status
            if new_status == "in_progress" and not order.actual_start:
                order.actual_start = datetime.utcnow()
            if new_status == "completed":
                order.actual_end = datetime.utcnow()
                try:
                    order.actual_hours = float(form.get("actual_hours") or order.estimated_hours)
                except ValueError:
                    pass
                # Mark the plan as performed and bump the train's mileage bookkeeping.
                if order.plan:
                    order.plan.last_performed_on = date.today()
                    if order.train:
                        order.plan.last_performed_km = order.train.current_mileage_km or 0
        if form.get("technician_id"):
            order.technician_id = int(form["technician_id"])
        if form.get("priority") in PRIORITIES:
            order.priority = form["priority"]
        if form.get("completion_notes") is not None:
            order.completion_notes = form["completion_notes"]
        db.session.commit()
        flash(f"Work order {order.order_number} updated.", "success")
        return redirect(url_for("work_order_detail", order_id=order.id))

    @app.route("/work-orders/<int:order_id>/delete", methods=["POST"])
    def work_order_delete(order_id):
        order = WorkOrder.query.get_or_404(order_id)
        db.session.delete(order)
        db.session.commit()
        flash(f"Work order {order.order_number} deleted.", "success")
        return redirect(url_for("work_orders_list"))

    # --------- Schedule view -----------------------------------------------
    @app.route("/schedule")
    def schedule():
        # Show a 4-week window starting from 1 week before today
        start = date.today() - timedelta(days=date.today().weekday() + 7)
        end = start + timedelta(days=42)
        orders = WorkOrder.query.filter(
            WorkOrder.scheduled_start != None,  # noqa: E711
            WorkOrder.scheduled_start >= datetime.combine(start, datetime.min.time()),
            WorkOrder.scheduled_start < datetime.combine(end, datetime.min.time()),
        ).order_by(WorkOrder.scheduled_start).all()

        # Build calendar structure: list of weeks, each with 7 days.
        weeks = []
        d = start
        while d < end:
            week = []
            for _ in range(7):
                day_orders = [o for o in orders
                              if o.scheduled_start and o.scheduled_start.date() == d]
                week.append({"date": d, "orders": day_orders})
                d += timedelta(days=1)
            weeks.append(week)

        return render_template(
            "schedule.html",
            weeks=weeks,
            range_start=start,
            range_end=end - timedelta(days=1),
            today=date.today(),
        )

    # --------- SAP import --------------------------------------------------
    @app.route("/sap")
    def sap_import_page():
        logs = SapImportLog.query.order_by(SapImportLog.imported_at.desc()).limit(30).all()
        return render_template(
            "sap_import.html",
            logs=logs,
            object_types=list(sap_connector.IMPORTERS.keys()),
        )

    @app.route("/sap/upload", methods=["POST"])
    def sap_upload():
        object_type = request.form.get("object_type")
        if object_type not in sap_connector.IMPORTERS:
            flash("Unknown SAP object type.", "error")
            return redirect(url_for("sap_import_page"))
        file = request.files.get("file")
        if not file or not file.filename:
            flash("Please pick a CSV file.", "error")
            return redirect(url_for("sap_import_page"))
        log = sap_connector.IMPORTERS[object_type](file)
        level = "success" if log.status == "success" else "error"
        flash(f"{object_type}: {log.message}", level)
        return redirect(url_for("sap_import_page"))

    @app.route("/sap/mock-pull", methods=["POST"])
    def sap_mock_pull():
        try:
            n = int(request.form.get("num_trains", "10"))
        except ValueError:
            n = 10
        n = max(1, min(n, 50))
        logs = sap_connector.run_mock_pull(num_trains=n)
        created = sum(l.records_created for l in logs)
        updated = sum(l.records_updated for l in logs)
        flash(f"Mock SAP pull complete — {created} new, {updated} updated records.",
              "success")
        return redirect(url_for("sap_import_page"))

    @app.route("/sap/sample/<object_type>")
    def sap_sample(object_type):
        if object_type not in sap_connector.IMPORTERS:
            abort(404)
        paths = sap_connector.ensure_sample_files()
        directory, filename = os.path.split(paths[object_type])
        return send_from_directory(directory, filename, as_attachment=True)

    # --------- JSON APIs ----------------------------------------------------
    @app.route("/api/trains")
    def api_trains():
        return jsonify([t.to_dict() for t in Train.query.all()])

    @app.route("/api/work-orders")
    def api_work_orders():
        return jsonify([o.to_dict() for o in WorkOrder.query.all()])

    @app.route("/api/plans")
    def api_plans():
        return jsonify([p.to_dict() for p in MaintenancePlan.query.all()])

    @app.route("/api/upcoming")
    def api_upcoming():
        items = upcoming_maintenance(
            days_ahead=int(request.args.get("days", "30")))
        return jsonify([
            {
                "plan_number": i["plan"].plan_number,
                "description": i["plan"].description,
                "train": i["train"].equipment_number,
                "status": i["status"],
                "next_due_date": i["next_due_date"].isoformat() if i["next_due_date"] else None,
                "next_due_km": i["next_due_km"],
                "days_to_go": i["days_to_go"],
                "km_to_go": i["km_to_go"],
            }
            for i in items
        ])

    # --------- CSV export --------------------------------------------------
    @app.route("/export/work-orders.csv")
    def export_work_orders():
        import csv, io
        buf = io.StringIO()
        writer = csv.writer(buf)
        writer.writerow([
            "Order", "Train", "Description", "Type", "Status", "Priority",
            "Scheduled start", "Scheduled end", "Technician",
            "Estimated h", "Actual h",
        ])
        for o in WorkOrder.query.order_by(WorkOrder.scheduled_start.asc().nullslast()).all():
            writer.writerow([
                o.order_number,
                o.train.equipment_number if o.train else "",
                o.description,
                o.order_type,
                o.status,
                o.priority,
                o.scheduled_start.isoformat() if o.scheduled_start else "",
                o.scheduled_end.isoformat() if o.scheduled_end else "",
                o.technician.name if o.technician else "",
                o.estimated_hours or 0,
                o.actual_hours or 0,
            ])
        return Response(buf.getvalue(), mimetype="text/csv",
                        headers={"Content-Disposition":
                                 "attachment; filename=work_orders.csv"})


# ---------------------------------------------------------------------------
# Template filters
# ---------------------------------------------------------------------------
def _register_template_filters(app: Flask):
    @app.template_filter("dt")
    def fmt_dt(value):
        if not value:
            return "—"
        if isinstance(value, datetime):
            return value.strftime("%Y-%m-%d %H:%M")
        return value.strftime("%Y-%m-%d")

    @app.template_filter("d")
    def fmt_d(value):
        if not value:
            return "—"
        if isinstance(value, datetime):
            value = value.date()
        return value.strftime("%Y-%m-%d")

    @app.template_filter("km")
    def fmt_km(value):
        if value is None:
            return "—"
        return f"{int(value):,} km"

    @app.template_filter("hours")
    def fmt_hours(value):
        if value is None:
            return "—"
        return f"{float(value):.1f} h"

    @app.template_filter("status_badge")
    def status_badge(value):
        return {
            "active": "badge-active",
            "in_workshop": "badge-warning",
            "standby": "badge-standby",
            "retired": "badge-muted",
            "open": "badge-muted",
            "scheduled": "badge-standby",
            "in_progress": "badge-warning",
            "completed": "badge-active",
            "cancelled": "badge-muted",
            "overdue": "badge-danger",
            "due_soon": "badge-warning",
            "ok": "badge-active",
        }.get(value, "badge-muted")


# ---------------------------------------------------------------------------
# Background scheduler — keeps derived state up to date
# ---------------------------------------------------------------------------
def start_scheduler(app: Flask):
    scheduler = BackgroundScheduler(daemon=True)

    def hourly_auto_schedule():
        with app.app_context():
            auto_schedule_due_work(horizon_days=14)

    scheduler.add_job(hourly_auto_schedule, "interval", hours=1, id="auto_schedule")
    scheduler.start()
    return scheduler


app = create_app()

if __name__ == "__main__":
    start_scheduler(app)
    port = int(os.environ.get("PORT", "5000"))
    app.run(host="0.0.0.0", port=port, debug=True, use_reloader=False)
