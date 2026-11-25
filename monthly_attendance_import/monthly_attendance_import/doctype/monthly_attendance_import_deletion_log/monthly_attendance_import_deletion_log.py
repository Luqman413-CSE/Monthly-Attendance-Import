import frappe
from frappe.model.document import Document


class MonthlyAttendanceImportDeletionLog(Document):
    def validate(self):
        if not self.is_new() or not getattr(self.flags, "from_monthly_import_delete", False):
            frappe.throw("Deletion logs are created only by the Delete Import workflow", frappe.PermissionError)

    def on_trash(self):
        frappe.throw("Deletion logs are immutable and cannot be deleted", frappe.PermissionError)
