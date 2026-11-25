"""Add the app-owned Project link used by Monthly Attendance Import."""
import frappe


def execute():
    if frappe.db.exists("Custom Field", {"dt": "Attendance", "fieldname": "custom_project"}):
        return
    frappe.get_doc({
        "doctype": "Custom Field",
        "dt": "Attendance",
        "fieldname": "custom_project",
        "label": "Project",
        "fieldtype": "Link",
        "options": "Project",
        "insert_after": "company",
        "read_only": 1,
        "module": "Monthly Attendance Import",
    }).insert(ignore_permissions=True)
    frappe.clear_cache(doctype="Attendance")
