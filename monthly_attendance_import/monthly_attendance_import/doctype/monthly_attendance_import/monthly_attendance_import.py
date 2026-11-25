import frappe
from frappe.model.document import Document
from monthly_attendance_import.service import INPUTS


class MonthlyAttendanceImport(Document):
    def validate(self):
        frappe.get_doc("Company", self.company).check_permission("read")
        if not 1 <= int(self.month or 0) <= 12 or not 1900 <= int(self.year or 0) <= 2200:
            frappe.throw("Select a valid month and year (1900–2200)")
        previous = self.get_doc_before_save()
        if (self.flags.from_monthly_import or self.flags.from_monthly_import_reverse
                or self.flags.from_monthly_import_correction):
            return
        owned = ("status", "cycle", "fingerprint", "requested_by", "error", "proposed_count", "created_count",
                 "skipped_count", "failed_count", "results", "history")
        if previous:
            if any(self.get(f) != previous.get(f) for f in INPUTS):
                if previous.status in ("Imported", "Queued", "Reversed"):
                    frappe.throw("Queued, imported, or reversed batches are immutable; use the controlled correction action")
                self.status = "Draft"
                self.fingerprint = ""
                self.error = "Inputs changed; Validate & Preview again"
                self.set("results", [])
                for f in ("proposed_count", "created_count", "skipped_count", "failed_count"):
                    self.set(f, 0)
            else:
                for f in owned:
                    if f not in ("results", "history") and self.get(f) != previous.get(f):
                        frappe.throw("Import results and state are server managed")
                self.set("results", [r.as_dict() for r in previous.results])
            # Always restore append-only history; API clients cannot forge it.
            self.set("history", [r.as_dict() for r in previous.history])
        else:
            self.status = "Draft"
            self.cycle = self.cycle or 1
            for f in owned:
                if f != "status":
                    self.set(f, [] if f in ("results", "history") else None)

    def on_trash(self):
        # Only batches that created records (or may be creating them) are kept for audit.
        if getattr(self.flags, "monthly_import_cleanup", False):
            return
        status = frappe.db.get_value(self.doctype, self.name, "status", for_update=True)
        if status in ("Imported", "Queued", "Reversed"):
            frappe.throw("Imported, queued, or reversed batches are retained for audit")
