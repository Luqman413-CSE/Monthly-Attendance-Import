"""Permission checks, interpretation, current reads and atomic posting."""
import hashlib
import json
from decimal import Decimal

import frappe
from frappe.utils import getdate, now_datetime, nowdate

from monthly_attendance_import.parser import parse, extract_project_name
from monthly_attendance_import.rules import decide, normal_hours, validate_policy, RuleError

BATCH = "Monthly Attendance Import"
POLICY = "Monthly Attendance Import Policy"
OVERTIME = "Overtime Slip"
INPUTS = ("company", "month", "year", "attachment", "worksheet")


class LeaveValidationError(RuleError):
    def __init__(self, message, **preview_details):
        super().__init__(message)
        self.preview_details = preview_details


def current_rows(doctype, conditions):
    """Current (locking) read, not a stale repeatable-read snapshot."""
    table = frappe.qb.DocType(doctype)
    query = frappe.qb.from_(table).select(table.star)
    for field, operator, value in conditions:
        column = table[field]
        expression = {"=": lambda: column == value, "<": lambda: column < value,
                      "<=": lambda: column <= value, ">=": lambda: column >= value}[operator]()
        query = query.where(expression)
    return query.for_update().run(as_dict=True)


def lock_employee_document(doc, method=None):
    employees = {doc.get("employee")}
    if not doc.is_new():
        employees.add(frappe.db.get_value(doc.doctype, doc.name, "employee"))
    for employee in sorted(e for e in employees if e):
        table = frappe.qb.DocType("Employee")
        frappe.qb.from_(table).select(table.name).where(table.name == employee).for_update().run()


def import_source_file_urls(batch_name):
    """Return current and historical source URLs for one import batch."""
    urls = set()
    current = frappe.db.get_value(BATCH, batch_name, "attachment")
    if current:
        urls.add(current)
    urls.update(frappe.get_all("Monthly Attendance Import History",
        filters={"parent": batch_name}, pluck="attachment"))
    return {url for url in urls if url}


def protect_file(doc, method=None):
    previous = doc.get_doc_before_save() if method != "on_trash" else doc
    if not previous and not doc.is_new():
        previous = frappe.get_doc("File", doc.name)
    if getattr(doc.flags, "monthly_import_cleanup_detach", False):
        if (
            method != "validate"
            or not previous
            or previous.attached_to_doctype != BATCH
            or not previous.attached_to_name
            or doc.attached_to_doctype
            or doc.attached_to_name
            or any(previous.get(f) != doc.get(f) for f in
                   ("file_url", "is_private", "content_hash"))
        ):
            frappe.throw("Only an explicit Monthly Attendance Import source detachment is allowed")
        return
    for candidate in (previous, doc):
        protected_source = candidate and candidate.attached_to_doctype == BATCH and candidate.attached_to_name \
            and candidate.file_url in import_source_file_urls(candidate.attached_to_name)
        if protected_source:
            if method == "on_trash" or (previous and any(
                previous.get(f) != doc.get(f) for f in
                ("file_url", "is_private", "attached_to_doctype", "attached_to_name", "content_hash")
            )):
                frappe.throw("Import source files are retained for audit; upload a new attachment instead")
            if not doc.is_private:
                frappe.throw("Monthly attendance workbooks must be private")


def detach_source_files_for_cleanup(batch):
    """Detach one import's source Files without deleting their content."""
    if isinstance(batch, str):
        batch = frappe.get_doc(BATCH, batch)
    if not batch.attachment:
        return 0
    files = frappe.get_all(
        "File",
        filters={"attached_to_doctype": BATCH, "attached_to_name": batch.name},
        fields=["name", "file_url", "is_private"],
    )
    if not files:
        frappe.throw("Import source attachment is missing")
    for file in files:
        if file.file_url not in import_source_file_urls(batch.name) or not file.is_private or not file.file_url.startswith("/private/files/"):
            frappe.throw("Import source attachment failed private-file validation")
        doc = frappe.get_doc("File", file.name)
        doc.attached_to_doctype = None
        doc.attached_to_name = None
        doc.attached_to_field = None
        doc.flags.monthly_import_cleanup_detach = True
        doc.save()
    return len(files)


def import_delete_dependencies(batch):
    """Return linked transaction problems that must block controlled deletion."""
    result_rows = frappe.get_all(BATCH + " Result", filters={"parent": batch.name},
        fields=["attendance", "overtime_slip"])
    problems = []
    for field, doctype in (("attendance", "Attendance"), ("overtime_slip", OVERTIME)):
        names = sorted({row.get(field) for row in result_rows if row.get(field)})
        for name in names:
            doc = frappe.db.get_value(doctype, name,
                ["docstatus", "company"], as_dict=True)
            if not doc:
                problems.append(f"{doctype} {name} referenced by the import no longer exists")
            elif doc.company != batch.company:
                problems.append(f"{doctype} {name} belongs to another Company")
            elif doc.docstatus < 2:
                problems.append(f"{doctype} {name} is still active; cancel or delete it first")
    return problems, len(result_rows)


@frappe.whitelist(methods=["POST"])
def delete_import(name, reason):
    """Delete one import after dependency checks, preserving its source Files."""
    if "System Manager" not in frappe.get_roles():
        frappe.throw("Only System Managers can delete Monthly Attendance Imports", frappe.PermissionError)
    reason = (reason or "").strip()
    if not reason:
        frappe.throw("A deletion reason is required")
    batch = frappe.get_doc(BATCH, name, for_update=True)
    batch.check_permission("write")
    if batch.status == "Queued":
        frappe.throw("Queued imports cannot be deleted while processing")
    problems, result_count = import_delete_dependencies(batch)
    if problems:
        frappe.throw("Import cannot be deleted:\n" + "\n".join(problems))
    save_point = "monthly_import_delete"
    frappe.db.savepoint(save_point)
    try:
        log = frappe.get_doc({
            "doctype": "Monthly Attendance Import Deletion Log",
            "import_name": batch.name,
            "deleted_by": frappe.session.user,
            "deleted_at": now_datetime(),
            "reason": reason,
            "company": batch.company,
            "month": batch.month,
            "year": batch.year,
            "result_rows": result_count,
        })
        log.flags.from_monthly_import_delete = True
        log.insert(ignore_permissions=True)
        detached = detach_source_files_for_cleanup(batch)
        batch.flags.monthly_import_cleanup = True
        batch.delete()
        frappe.db.commit()
    except Exception:
        frappe.db.rollback(save_point=save_point)
        raise
    return {"name": name, "result_rows": result_count, "detached_files": detached}


def reverse_import_dependencies(batch):
    """Build and validate the exact transaction set owned by an imported batch."""
    start = f"{int(batch.year):04d}-{int(batch.month):02d}-01"
    from calendar import monthrange
    end = f"{int(batch.year):04d}-{int(batch.month):02d}-{monthrange(int(batch.year), int(batch.month))[1]:02d}"
    rows = frappe.get_all(BATCH + " Result", filters={"parent": batch.name},
        fields=["name", "employee", "attendance_date", "result", "attendance", "overtime_slip",
                "leave_application"])
    problems, warnings, transactions = [], [], []
    attendance_names = set()
    overtime_names = set()
    for row in rows:
        if row.result != "Created" and (row.attendance or row.overtime_slip):
            problems.append(f"Result {row.name} has transaction links but is not marked Created")
        if row.attendance:
            attendance_names.add(row.attendance)
        if row.overtime_slip:
            overtime_names.add(row.overtime_slip)

    for name in sorted(attendance_names):
        doc = frappe.get_doc("Attendance", name)
        if doc.company != batch.company or not (start <= str(doc.attendance_date) <= end):
            problems.append(f"Attendance {name} is outside the import company or period")
        if doc.attendance_request and not frappe.db.exists("Attendance Request", doc.attendance_request):
            problems.append(f"Attendance {name} references a missing Attendance Request")
        action = {0: "Delete draft", 1: "Cancel submitted", 2: "Already cancelled"}.get(doc.docstatus)
        if not action:
            problems.append(f"Attendance {name} has unsupported docstatus {doc.docstatus}")
            continue
        if doc.docstatus == 1 and doc.attendance_request:
            problems.append(f"Attendance {name} has a submitted Attendance Request dependency")
        if doc.docstatus == 1 and doc.leave_application:
            leave_status = frappe.db.get_value("Leave Application", doc.leave_application,
                ["docstatus", "status"], as_dict=True)
            if not leave_status:
                problems.append(f"Attendance {name} references a missing Leave Application")
            elif leave_status.docstatus != 1 or leave_status.status != "Approved":
                problems.append(f"Attendance {name} references a non-approved Leave Application")
            else:
                warnings.append(f"Attendance {name} references approved Leave Application {doc.leave_application}; it will not be changed")
        transactions.append(dict(kind="Attendance", name=name, doc=doc, action=action,
            employee=doc.employee, date=doc.attendance_date, leave_application=doc.leave_application))

    for name in sorted(overtime_names):
        doc = frappe.get_doc(OVERTIME, name)
        if doc.company != batch.company or str(doc.start_date) > end or str(doc.end_date) < start:
            problems.append(f"Overtime Slip {name} is outside the import company or period")
        payroll_reference = doc.salary_slip or doc.payroll_entry
        if payroll_reference or doc.submitted_via_payroll_entry:
            problems.append(f"Overtime Slip {name} is linked to payroll ({payroll_reference or 'Payroll Entry'})")
        action = {0: "Delete draft", 1: "Cancel submitted", 2: "Already cancelled"}.get(doc.docstatus)
        if not action:
            problems.append(f"Overtime Slip {name} has unsupported docstatus {doc.docstatus}")
            continue
        transactions.append(dict(kind="Overtime Slip", name=name, doc=doc, action=action,
            employee=doc.employee, date=doc.start_date, leave_application=None,
            payroll_reference=payroll_reference))

    employees = sorted({t["employee"] for t in transactions if t.get("employee")})
    employee_filter = employees or ["__none__"]
    submitted_salary_slips = frappe.get_all("Salary Slip", filters={
        "employee": ["in", employee_filter], "company": batch.company,
        "docstatus": 1, "start_date": ["<=", end], "end_date": [">=", start]},
        fields=["name", "employee", "start_date", "end_date"])
    for slip in submitted_salary_slips:
        problems.append(f"Submitted Salary Slip {slip.name} overlaps an imported employee")
    return dict(rows=rows, transactions=transactions, problems=problems, warnings=warnings,
        submitted_salary_slips=submitted_salary_slips)


@frappe.whitelist(methods=["POST"])
def reverse_import(name, reason):
    """Atomically reverse only transactions created by one imported batch."""
    if "System Manager" not in frappe.get_roles():
        frappe.throw("Only System Managers can reverse Monthly Attendance Imports", frappe.PermissionError)
    reason = (reason or "").strip()
    if not reason:
        frappe.throw("A reversal reason is required")
    batch = frappe.get_doc(BATCH, name, for_update=True)
    batch.check_permission("write")
    if batch.status == "Reversed":
        frappe.throw("This import has already been reversed")
    if batch.status != "Imported":
        frappe.throw("Only successfully imported batches can be reversed")
    plan = reverse_import_dependencies(batch)
    if plan["problems"]:
        frappe.throw("Import cannot be reversed:\n" + "\n".join(plan["problems"]))

    save_point = "monthly_import_reverse"
    frappe.db.savepoint(save_point)
    try:
        draft_attendance = {t["name"] for t in plan["transactions"]
            if t["kind"] == "Attendance" and t["doc"].docstatus == 0}
        draft_overtime = {t["name"] for t in plan["transactions"]
            if t["kind"] == "Overtime Slip" and t["doc"].docstatus == 0}

        for row in batch.results:
            if row.attendance in draft_attendance:
                row.reversed_attendance = row.attendance
                row.attendance = None
            if row.overtime_slip in draft_overtime:
                row.reversed_overtime_slip = row.overtime_slip
                row.overtime_slip = None
        batch.status = "Reversed"
        batch.flags.from_monthly_import_reverse = True
        batch.save()

        for transaction in plan["transactions"]:
            if transaction["action"] == "Delete draft":
                transaction["doc"].delete()
            elif transaction["action"] == "Cancel submitted":
                transaction["doc"].cancel()
        frappe.db.commit()
    except Exception:
        frappe.db.rollback(save_point=save_point)
        raise
    return {"status": "Reversed", "name": batch.name,
        "attendance_count": sum(t["kind"] == "Attendance" for t in plan["transactions"]),
        "overtime_count": sum(t["kind"] == "Overtime Slip" for t in plan["transactions"]),
        "warnings": plan["warnings"]}


def correction_dependencies(batch):
    """Return active links that must be clear before a reversed cycle is reused."""
    problems = []
    employees = set()
    for row in batch.results:
        if row.employee:
            employees.add(row.employee)
        for field, doctype in (("attendance", "Attendance"), ("overtime_slip", OVERTIME)):
            name = row.get(field)
            if not name:
                continue
            fields = ["docstatus", "company"]
            if doctype == OVERTIME:
                fields += ["salary_slip", "payroll_entry", "submitted_via_payroll_entry"]
            doc = frappe.db.get_value(doctype, name, fields, as_dict=True)
            if not doc:
                problems.append(f"{doctype} {name} referenced by the previous cycle no longer exists")
            elif doc.company != batch.company:
                problems.append(f"{doctype} {name} belongs to another Company")
            elif doc.docstatus < 2:
                problems.append(f"{doctype} {name} is still active; reverse it before correction")
            elif doctype == OVERTIME and (doc.salary_slip or doc.payroll_entry or doc.submitted_via_payroll_entry):
                problems.append(f"Overtime Slip {name} remains linked to payroll")

    if employees:
        start = f"{int(batch.year):04d}-{int(batch.month):02d}-01"
        from calendar import monthrange
        end = f"{int(batch.year):04d}-{int(batch.month):02d}-{monthrange(int(batch.year), int(batch.month))[1]:02d}"
        slips = frappe.get_all("Salary Slip", filters={
            "employee": ["in", sorted(employees)], "company": batch.company,
            "docstatus": 1, "start_date": ["<=", end], "end_date": [">=", start]},
            pluck="name")
        for name in slips:
            problems.append(f"Submitted Salary Slip {name} overlaps the previous import cycle")
    return problems


def _correction_history(batch, reason, new_file_url, new_fingerprint):
    """Archive the active result rows before starting the next cycle."""
    cycle = int(batch.get("cycle") or 1)
    snapshot = [row.as_dict() for row in batch.results]
    batch.append("history", dict(
        occurred_at=now_datetime(), actor=frappe.session.user, cycle=cycle,
        action="Correction Archived", attachment=batch.attachment,
        fingerprint=batch.fingerprint,
        details=json.dumps({
            "cycle": cycle, "reason": reason, "source_attachment": batch.attachment,
            "replacement_attachment": new_file_url, "replacement_fingerprint": new_fingerprint,
            "results": snapshot,
        }, default=str)))
    return cycle


@frappe.whitelist(methods=["POST"])
def correct_import_upload():
    """Receive a corrected workbook without invoking Attach-field replacement."""
    if "System Manager" not in frappe.get_roles():
        frappe.throw("Only System Managers can correct Monthly Attendance Imports", frappe.PermissionError)
    name = frappe.form_dict.get("docname")
    reason = (frappe.form_dict.get("reason") or "").strip()
    content = getattr(frappe.local, "uploaded_file", None)
    filename = getattr(frappe.local, "uploaded_filename", None) or ""
    if not reason:
        frappe.throw("A correction reason is required")
    if not content:
        frappe.throw("Upload a corrected .xlsx workbook")
    if not filename.lower().endswith(".xlsx"):
        frappe.throw("The corrected source must be an .xlsx workbook")
    from monthly_attendance_import.parser import MAX_BYTES
    if len(content) > MAX_BYTES:
        frappe.throw("File exceeds 5 MiB")
    if str(frappe.form_dict.get("is_private")) not in ("1", "true", "True"):
        frappe.throw("Corrected workbooks must be private")

    batch = frappe.get_doc(BATCH, name, for_update=True)
    batch.check_permission("write")
    if batch.status != "Reversed":
        frappe.throw("Only successfully reversed imports can be corrected")
    problems = correction_dependencies(batch)
    if problems:
        frappe.throw("Import cannot be corrected:\n" + "\n".join(problems))

    from frappe.utils.file_manager import save_file
    source_fingerprint = hashlib.sha256(content).hexdigest()
    old_urls = import_source_file_urls(batch.name)
    original_values = {
        "attachment": batch.attachment, "cycle": batch.cycle, "status": batch.status,
        "fingerprint": batch.fingerprint, "requested_by": batch.requested_by,
        "error": batch.error, "results": list(batch.results),
        "history": list(batch.history),
    }
    save_point = "monthly_import_correct"
    frappe.db.savepoint(save_point)
    try:
        source = save_file(filename, content, BATCH, batch.name, folder="Home",
            is_private=1, df="attachment")
        if source.file_url in old_urls:
            frappe.throw("Upload a new source workbook; original audit files cannot be reused")
        if source.attached_to_doctype != BATCH or source.attached_to_name != batch.name:
            frappe.throw("The corrected workbook is already owned by another File; upload a new source copy")
        cycle = _correction_history(batch, reason, source.file_url, source_fingerprint)
        batch.attachment = source.file_url
        batch.cycle = cycle + 1
        batch.status = "Draft"
        batch.fingerprint = ""
        batch.requested_by = None
        batch.error = ""
        batch.set("results", [])
        for field in ("proposed_count", "created_count", "skipped_count", "failed_count"):
            batch.set(field, 0)
        batch.flags.from_monthly_import_correction = True
        try:
            batch.save()
        finally:
            batch.flags.from_monthly_import_correction = False
        frappe.db.commit()
    except Exception:
        rollback_error = rollback_savepoint(save_point)
        if rollback_error:
            frappe.logger("monthly_attendance_import").warning(
                "Correction savepoint cleanup failed: %s", rollback_error)
        # Keep the in-memory document consistent with the rolled-back database
        # transaction for callers that continue using this request context.
        for field in ("attachment", "cycle", "status", "fingerprint", "requested_by", "error"):
            setattr(batch, field, original_values[field])
        batch.set("results", original_values["results"])
        batch.set("history", original_values["history"])
        raise
    return {"status": batch.status, "name": batch.name, "cycle": batch.cycle,
        "attachment": batch.attachment}


def authorize(batch):
    if not frappe.db.get_value("User", frappe.session.user, "enabled"):
        frappe.throw("The initiating user is disabled", frappe.PermissionError)
    batch.check_permission("read")
    batch.check_permission("write")
    frappe.get_doc("Company", batch.company, for_update=True).check_permission("read")


def load_batch(name):
    doc = frappe.get_doc(BATCH, name)
    authorize(doc)
    doc = frappe.get_doc(BATCH, name, for_update=True)
    authorize(doc)
    return doc


def read_source(batch):
    names = frappe.get_all("File", filters={"file_url": batch.attachment,
        "attached_to_doctype": BATCH, "attached_to_name": batch.name, "is_private": 1}, pluck="name")
    if not names:
        frappe.throw("Attach a private .xlsx file to this saved import batch")
    source = frappe.get_doc("File", names[0])
    source.check_permission("read")
    if not source.file_name.lower().endswith(".xlsx") or not source.file_url.startswith("/private/files/"):
        frappe.throw("A local private .xlsx attachment is required")
    from pathlib import Path
    from monthly_attendance_import.parser import MAX_BYTES
    if Path(source.get_full_path()).stat().st_size > MAX_BYTES:
        frappe.throw("File exceeds 5 MiB")
    return source.get_content()


def policy_for(batch):
    policy = frappe.get_doc(POLICY, batch.company, for_update=True)
    policy.check_permission("read")
    if not policy.enabled:
        frappe.throw("Enable and configure the company import policy before previewing")
    validate_policy(policy)
    if policy.identifier_field not in ("name", "employee_number"):
        frappe.throw("Select Employee ID (name) or employee_number in the company policy")
    return policy


def resolve_employee(identifier, policy, company):
    matches = current_rows("Employee", [(policy.identifier_field, "=", identifier)])
    if len(matches) != 1:
        raise RuleError("Employee number is unknown or ambiguous")
    employee = frappe.get_doc("Employee", matches[0].name, for_update=True)
    employee.check_permission("read")
    if employee.company != company:
        raise RuleError("Employee belongs to a different company")
    return employee


def resolve_shift(employee, day, policy):
    assignments = current_rows("Shift Assignment", [
        ("employee", "=", employee.name), ("docstatus", "=", 1),
        ("status", "=", "Active"), ("start_date", "<=", day)])
    assignments = [a for a in assignments if not a.end_date or getdate(a.end_date) >= day]
    if len(assignments) > 1:
        raise RuleError("Multiple shift assignments on this date require manual review")
    shift = assignments[0].shift_type if assignments else employee.default_shift
    if policy.shift_rule == "No shift":
        if shift:
            raise RuleError("No-shift policy conflicts with an assigned/default shift")
        return None
    if policy.shift_rule != "Require assignment or default" or not shift:
        raise RuleError("Configure shift policy and a unique assignment/default shift")
    frappe.get_doc("Shift Type", shift, for_update=True).check_permission("read")
    return shift


def workflow_snapshot(doctype):
    names = frappe.get_all("Workflow", filters={"document_type": doctype, "is_active": 1}, pluck="name")
    if len(names) > 1:
        raise RuleError("Multiple active workflows require administrator review")
    if not names:
        return None
    workflow = frappe.get_doc("Workflow", names[0], for_update=True)
    if not workflow.states or int(workflow.states[0].doc_status) != 0:
        raise RuleError("Importer requires a draft initial workflow state")
    return workflow.as_dict()


def is_holiday_for(employee, shift, day):
    from hrms.utils.holiday_list import get_holiday_list_for_employee, get_holiday_dates_between
    holiday_list = (frappe.get_doc("Shift Type", shift).get_holiday_list(employee, day)
                    if shift else get_holiday_list_for_employee(employee, as_on=day))
    if not holiday_list:
        raise RuleError("Configure an effective Holiday List Assignment before importing")
    frappe.get_doc("Holiday List", holiday_list, for_update=True)
    return bool(get_holiday_dates_between(holiday_list, day, day))


def same_attendance(existing, proposed, approved_leave=False):
    fields = ("employee", "company", "attendance_date", "status", "shift", "leave_type",
              "custom_project",
              "leave_application", "half_day_status", "modify_half_day_status", "late_entry", "early_exit")
    from frappe.model.workflow import get_workflow_name, get_workflow
    native_leave = (approved_leave and existing.docstatus == 1 and proposed.status == "On Leave"
        and proposed.leave_application and not get_workflow_name("Attendance"))
    if existing.docstatus != 0 and not native_leave:
        return False
    for field in fields:
        if str(existing.get(field) or "") != str(proposed.get(field) or ""):
            return False
    if Decimal(str(existing.working_hours or 0)) != Decimal(str(proposed.working_hours or 0)):
        return False
    if (existing.in_time or existing.out_time or existing.attendance_request
        or existing.get("overtime_type") or existing.get("actual_overtime_duration")):
        return False
    if get_workflow_name("Attendance"):
        wf = get_workflow("Attendance")
        if existing.get(wf.workflow_state_field) != proposed.get(wf.workflow_state_field):
            return False
    return True


def resolve_project(project_name, company):
    """Resolve one exact normalized Project Name belonging to the import company."""
    normalized = " ".join((project_name or "").split()).casefold()
    if not normalized:
        raise RuleError("Project Name is missing from Excel cell C2")
    projects = frappe.get_all("Project", fields=["name", "project_name", "company"])
    matches = [p for p in projects if " ".join((p.project_name or "").split()).casefold() == normalized]
    company_matches = [p for p in matches if p.company == company]
    if not company_matches:
        if matches:
            raise RuleError("Project Name matches a Project belonging to another Company")
        raise RuleError(f"Project Name {project_name!r} does not match an existing ERPNext Project")
    if len(company_matches) != 1:
        raise RuleError(f"Project Name {project_name!r} is ambiguous for Company {company}")
    project = frappe.get_doc("Project", company_matches[0].name)
    project.check_permission("read")
    return project


def resolve_leave_application(employee, day, policy, code):
    """Resolve exactly one submitted, approved, full-day application for a code."""
    expected_type = None
    if code == "VACATION" and (policy.get("vacation_handling_mode") or "Strict Annual") == "Strict Annual":
        expected_type = policy.get("vacation_leave_type") or "Annual"
    elif code == "SL":
        expected_type = policy.get("sl_leave_type")
    expected_text = f" matching {expected_type}" if expected_type else ""
    applications = current_rows("Leave Application", [
        ("employee", "=", employee.name), ("from_date", "<=", day), ("to_date", ">=", day)])
    details = dict(matched_leave_type=None, leave_application=None, validation_status="Error")
    if not applications:
        raise LeaveValidationError(
            f"{code} requires one approved full-day Leave Application{expected_text}; none covers {day}", **details)
    if any(app.get("docstatus", 1) != 1 or app.get("status", "Approved") != "Approved" for app in applications):
        raise LeaveValidationError(
            f"{code} date {day} is covered by a Leave Application that is not submitted and approved", **details)
    if any(app.get("half_day") and (not app.get("half_day_date")
            or getdate(app.get("half_day_date")) == day) for app in applications):
        raise LeaveValidationError(
            f"{code} date {day} requires a full-day Leave Application; a half-day application applies", **details)
    if len(applications) != 1:
        raise LeaveValidationError(
            f"{code} date {day} is covered by overlapping approved Leave Applications", **details)
    application = applications[0]
    details.update(matched_leave_type=application.leave_type, leave_application=application.name,
        validation_status="Valid")
    if expected_type and application.leave_type != expected_type:
        raise LeaveValidationError(
            f"{code} requires {expected_type}; approved leave is {application.leave_type}", **details)
    leave_application = frappe.get_doc("Leave Application", application.name)
    leave_application.check_permission("read")
    leave_type = frappe.get_doc("Leave Type", application.leave_type, for_update=True)
    leave_type.check_permission("read")
    return application, leave_type


def validate_document(doc):
    doc._action = "save"
    doc.check_permission("create")
    original_name = doc.name
    # Native mandatory checks require child.parent even before an insert allocates
    # a naming-series value. Keep this temporary identity out of the saved plan.
    if not original_name:
        doc.name = "new-monthly-import-preview"
    doc.set_parent_in_children()
    try:
        doc._validate_links()
        doc.run_method("before_validate")
        doc.run_method("validate")
        doc._validate()
    finally:
        doc.name = original_name
        doc.set_parent_in_children()


def plan_entry(batch, policy, entry, employee, project=None):
    decision = decide(entry, policy)
    normal = normal_hours(entry, policy)
    row = dict(employee_number=entry.identifier, employee=employee.name,
        attendance_date=str(entry.date), normal_hours="" if normal is None else str(normal),
        overtime_hours="" if entry.overtime_hours is None else str(entry.overtime_hours),
        attendance_code=entry.code, source_cells=entry.cells, proposed_status=decision.status,
        project=project.name if project else None,
        result="Skipped", message=decision.reason)
    if not decision.status and not decision.overtime:
        return row, None, None
    if employee.status != "Active" or (employee.date_of_joining and entry.date < getdate(employee.date_of_joining)) or (
        employee.relieving_date and entry.date > getdate(employee.relieving_date)
    ):
        raise RuleError("Date is outside active employment")
    if entry.date > getdate(nowdate()):
        raise RuleError("Future attendance/overtime requires manual processing")
    shift = resolve_shift(employee, entry.date, policy)
    leave, leave_type = [], None
    if decision.status == "On Leave":
        vacation_mode = policy.get("vacation_handling_mode") or "Strict Annual"
        if entry.code == "VACATION" and vacation_mode == "Strict Annual" and not policy.get("vacation_leave_type"):
            raise RuleError("Select the VACATION Leave Type in the import policy")
        if entry.code == "SL" and not policy.get("sl_leave_type"):
            raise RuleError("Select the SL Leave Type in the import policy")
        application, leave_type = resolve_leave_application(employee, entry.date, policy, entry.code)
        leave = [application]
        row.update(matched_leave_type=application.leave_type, leave_application=application.name,
            validation_status="Valid")
    else:
        leave = current_rows("Leave Application", [("employee", "=", employee.name),
            ("docstatus", "=", 1), ("status", "=", "Approved"),
            ("from_date", "<=", entry.date), ("to_date", ">=", entry.date)])
    holiday = is_holiday_for(employee.name, shift, entry.date)
    if holiday:
        if policy.holiday_rule == "Skip":
            if decision.overtime:
                raise RuleError("Holiday skip cannot discard overtime")
            row.update(message="Holiday: explicit skip policy", proposed_status=None)
            return row, None, None
        if policy.holiday_rule != "Use daily rule":
            raise RuleError("Configure holiday handling; holidays are not assumed unpaid")
    if decision.status == "On Leave":
        if holiday and not leave_type.include_holiday:
            if current_rows("Attendance", [("employee", "=", employee.name),
                ("attendance_date", "=", entry.date), ("docstatus", "<", 2)]):
                raise RuleError("Existing attendance on a holiday excluded from leave requires review")
            row.update(message="Holiday excluded by approved Leave Type: no Attendance", proposed_status=None)
            return row, None, None
    elif leave:
        raise RuleError("Conflicts with approved leave; review through the native leave workflow")
    attendance = None
    if decision.status:
        attendance = frappe.new_doc("Attendance")
        attendance.update(dict(employee=employee.name,
            company=batch.company, attendance_date=entry.date, status=decision.status,
            custom_project=project.name if project else None,
            shift=shift, working_hours=float(normal or 0),
            half_day_status="Absent" if decision.status == "Half Day" else None))
        if leave:
            attendance.update(dict(leave_type=leave[0].leave_type, leave_application=leave[0].name))
        attendance._action = "save"
        attendance.validate_workflow()
    existing = current_rows("Attendance", [("employee", "=", employee.name),
        ("attendance_date", "=", entry.date), ("docstatus", "<", 2)])
    if existing:
        if attendance is None:
            raise RuleError("Overtime-only import conflicts with existing attendance; review manually")
        if len(existing) != 1:
            raise RuleError("Multiple existing attendance records require manual review")
        old = frappe.get_doc("Attendance", existing[0].name, for_update=True)
        old.check_permission("read")
        if not same_attendance(old, attendance, approved_leave=decision.status == "On Leave" and bool(leave)):
            raise RuleError("Conflicting attendance values or workflow state; existing record is untouched")
        row["attendance"] = old.name
        attendance = None
    elif attendance:
        expected = attendance.status
        validate_document(attendance)
        if attendance.status != expected:
            raise RuleError("Native attendance validation changed the proposed status")
    row["result"] = "Proposed" if attendance or decision.overtime else "Skipped"
    if row["result"] == "Skipped" and row.get("attendance"):
        row["message"] = "Matching approved leave Attendance already exists" if decision.status == "On Leave" else "Identical draft Attendance already exists"
    return row, attendance, None


def build_plan(batch):
    content = read_source(batch)
    policy = policy_for(batch)
    source_project_name = extract_project_name(content, batch.get("worksheet"))
    project = resolve_project(source_project_name, batch.company)
    entries, errors = parse(content, int(batch.year), int(batch.month), batch.get("worksheet"))
    workflows = [workflow_snapshot(dt) for dt in ("Attendance", OVERTIME)]
    employees, employee_errors = {}, {}
    # Stable employee lock order avoids cross-batch lock inversion.
    identifiers = sorted({e.identifier for e in entries})
    matched = {}
    for identifier in identifiers:
        matches = frappe.get_all("Employee", filters={policy.identifier_field: identifier}, pluck="name")
        matched[identifier] = matches[0] if len(matches) == 1 else ""
    for identifier in sorted(identifiers, key=lambda i: (matched[i], i)):
        try:
            employees[identifier] = resolve_employee(identifier, policy, batch.company)
        except Exception as exc:
            employee_errors[identifier] = str(exc)
    plan = []
    for entry in entries:
        try:
            if entry.identifier in employee_errors:
                raise RuleError(employee_errors[entry.identifier])
            plan.append(plan_entry(batch, policy, entry, employees[entry.identifier], project))
        except Exception as exc:
            message = f"{entry.identifier} {entry.date} ({entry.cells}): {exc}"
            errors.append(message)
            employee = employees.get(entry.identifier)
            row = dict(employee_number=entry.identifier, attendance_date=str(entry.date),
                employee=employee.name if employee else None, attendance_code=entry.code,
                source_cells=entry.cells, project=project.name if "project" in locals() else None,
                validation_status="Error",
                result="Failed", message=message)
            row.update(getattr(exc, "preview_details", {}))
            plan.append((row, None, None))
    if not errors:
        try:
            from monthly_attendance_import.native_overtime import prepare
            prepare(batch, policy, plan)
        except Exception as exc:
            errors.append(str(exc))
    candidate_fields = ("employee", "company", "attendance_date", "status", "shift", "custom_project", "working_hours",
        "leave_type", "leave_application", "half_day_status", "overtime_hours", "approval_status")
    candidates = [[{f: d.get(f) for f in candidate_fields} if d else None for d in (a, o)]
                  for _, a, o in plan]
    payload = dict(file=hashlib.sha256(content).hexdigest(), inputs=[batch.get(f) for f in INPUTS],
        project=dict(name=project.name, project_name=project.project_name, company=project.company,
                     modified=project.modified, status=project.status),
        policy=policy.as_dict(), workflows=workflows, rows=[r for r, _, _ in plan],
        candidates=candidates,
        native_slips=[dict(employee=o.employee, start=str(o.start_date), end=str(o.end_date),
            type_revision=o.flags.overtime_type_revision,
            details=[{f:d.get(f) for f in ("date", "overtime_type", "overtime_duration", "standard_working_hours", "reference_document")} for d in o.overtime_details])
            for _, _, o in plan if o],
        employees={key: value.modified for key, value in employees.items()})
    fingerprint = hashlib.sha256(json.dumps(payload, sort_keys=True, default=str).encode()).hexdigest()
    return plan, errors, fingerprint


def record(batch, action, plan, errors, fingerprint, status):
    batch.status, batch.fingerprint = status, fingerprint
    batch.error = "\n".join(errors)
    batch.set("results", [r for r, _, _ in plan])
    for name, state in (("proposed", "Proposed"), ("created", "Created"), ("skipped", "Skipped")):
        batch.set(name + "_count", sum(r["result"] == state for r, _, _ in plan))
    batch.failed_count = len(errors)
    batch.append("history", dict(occurred_at=now_datetime(), actor=frappe.session.user,
        cycle=int(batch.get("cycle") or 1),
        action=action, attachment=batch.attachment, fingerprint=fingerprint,
        details=json.dumps(dict(status=status, errors=errors, results=[r for r, _, _ in plan]), default=str)))
    save_owned(batch)


def save_owned(batch):
    batch.flags.from_monthly_import = True
    try:
        batch.save()
    finally:
        batch.flags.from_monthly_import = False


def rollback_savepoint(save_point):
    """Restore the transaction even if a validation hook ended the savepoint."""
    try:
        frappe.db.rollback(save_point=save_point)
    except Exception as savepoint_error:
        # A hook may commit or fully roll back the transaction. MariaDB then
        # reports 1305 for the savepoint, which must not replace the original
        # validation/import error. The full rollback is safe here because
        # preview and import are both responsible for the whole transaction.
        frappe.db.rollback()
        return savepoint_error


@frappe.whitelist(methods=["POST"])
def preview(name):
    batch = load_batch(name)
    if batch.status == "Queued":
        from frappe.utils.background_jobs import is_job_enqueued
        if is_job_enqueued(f"monthly-attendance:{batch.name}"):
            frappe.throw("Import is queued or running; wait for it to finish")
        # Also recovers jobs killed by worker timeout: their transaction rolled back.
    if batch.status == "Imported":
        frappe.throw("This batch is locked; create a new batch for a different upload")
    frappe.db.savepoint("monthly_preview")
    message_count = len(frappe.local.message_log)
    try:
        plan, errors, fingerprint = build_plan(batch)
    except Exception as exc:
        plan, errors, fingerprint = [], [str(exc)], ""
    # Native validations msgprint per row (e.g. "Employee is on Leave"); preview reports
    # blocking problems through `errors`, so drop those informational popups.
    del frappe.local.message_log[message_count:]
    # Native validation hooks must not persist incidental writes during preview.
    rollback_error = rollback_savepoint("monthly_preview")
    if rollback_error and not errors:
        errors = [f"Preview transaction cleanup failed: {rollback_error}"]
    record(batch, "Validate & Preview", plan, errors, fingerprint, "Invalid" if errors else "Ready")
    return {"status": batch.status, "errors": errors}


@frappe.whitelist(methods=["POST"])
def start_import(name):
    batch = load_batch(name)
    if batch.status == "Imported":
        return {"status": "Imported"}
    if batch.status == "Queued":
        frappe.throw("This older batch is queued. Wait for it to finish or Validate & Preview after its job ends.")
    if batch.status != "Ready" or not batch.fingerprint:
        frappe.throw("Validate & Preview successfully before importing")
    batch.requested_by = frappe.session.user
    save_owned(batch)
    # Run immediately under the caller's authorization and existing batch lock.
    # Frappe commits the request after validation and atomic posting completes.
    execute(batch)
    return {"status": batch.status}


def run_import(name, actor):
    """Compatibility only for jobs queued before immediate importing was introduced."""
    original_user = frappe.session.user
    try:
        frappe.set_user(actor)
        batch = load_batch(name)
        if batch.status != "Queued" or batch.requested_by != actor:
            return
        execute(batch)
    finally:
        frappe.set_user(original_user)


def report_progress(batch, phase, completed, total):
    # Progress is advisory, outside the business transaction; never commit records
    # to expose intermediate results. A cache outage must not change posting.
    try:
        frappe.cache.set_value("monthly-import-progress:" + batch.name,
            dict(phase=phase, completed=completed, total=total, fingerprint=batch.fingerprint), expires_in_sec=3600)
    except Exception:
        pass


@frappe.whitelist()
def get_progress(name):
    batch = frappe.get_doc(BATCH, name)
    batch.check_permission("read")
    frappe.get_doc("Company", batch.company).check_permission("read")
    progress = (frappe.cache.get_value("monthly-import-progress:" + name) or {}) if batch.status in ("Ready", "Queued") else {}
    if progress.get("fingerprint") != batch.fingerprint:
        progress = {}
    return dict(status=batch.status, phase=progress.get("phase", batch.status),
        completed=progress.get("completed", 0), total=progress.get("total", 0))


def execute(batch):
    plan, fingerprint = [], ""
    frappe.db.savepoint("monthly_post")
    try:
        report_progress(batch, "Validating", 0, 0)
        plan, errors, fingerprint = build_plan(batch)
        if errors:
            raise RuleError("\n".join(errors))
        if fingerprint != batch.fingerprint:
            raise RuleError("Preview is stale: input, policy, employee, workflow or existing records changed; preview again")
        total = len(plan) + sum(bool(o) for _, _, o in plan)
        completed = 0
        report_progress(batch, "Processing entries", 0, total)
        for row, attendance, overtime in plan:
            try:
                if attendance:
                    expected = attendance.status
                    attendance.insert()
                    if attendance.status != expected or attendance.docstatus != 0:
                        raise RuleError("Native hooks changed attendance status; review manually")
                    row["attendance"] = attendance.name
                if attendance:
                    row["result"] = "Created"
                completed += 1
                if completed % 25 == 0 or completed == len(plan):
                    report_progress(batch, "Processing entries", completed, total)
            except Exception as exc:
                raise RuleError(f"{row['employee_number']} {row['attendance_date']} ({row['source_cells']}): {exc}") from exc
        from monthly_attendance_import.native_overtime import insert as insert_overtime
        for row, _, overtime in plan:
            if overtime:
                try:
                    insert_overtime(overtime)
                    completed += 1
                    report_progress(batch, "Recording overtime", completed, total)
                except Exception as exc:
                    raise RuleError(f"{row['employee_number']} {row['attendance_date']} ({row['source_cells']}): {exc}") from exc
        report_progress(batch, "Finalizing", completed, total)
        record(batch, "Import", plan, [], fingerprint, "Imported")
    except Exception as exc:
        rollback_error = rollback_savepoint("monthly_post")
        if rollback_error:
            frappe.logger("monthly_attendance_import").warning(
                "Import savepoint cleanup failed after %s: %s", exc, rollback_error
            )
        # Discard names from rolled-back inserts, preserving only pre-existing links.
        for row, attendance, overtime in plan:
            if attendance:
                row.pop("attendance", None)
            if overtime:
                for slip_row in overtime.flags.import_rows:
                    slip_row.pop("overtime_slip", None)
            if row["result"] in ("Created", "Proposed"):
                row["result"] = "Failed"
                row["message"] = "Batch rolled back: " + str(exc)
        batch.reload()
        record(batch, "Import failed", plan, [str(exc)], "", "Failed")


@frappe.whitelist()
def download_template(name):
    batch = load_batch(name)
    from monthly_attendance_import.template import make_template
    frappe.local.response.filename = "monthly-attendance-template.xlsx"
    frappe.local.response.filecontent = make_template(int(batch.year), int(batch.month), batch.get("worksheet"))
    frappe.local.response.type = "binary"
