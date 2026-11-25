# Monthly Attendance Import

Audited monthly attendance and overtime-hours import for Frappe, ERPNext, and HRMS v16.

Version `0.2.0` validates a fixed-layout Excel workbook, provides a read-only preview, and creates native ERPNext Attendance and HRMS Overtime Slip records in one controlled transaction. The app preserves the original workbook and import history for audit and includes controlled recovery actions for incorrect imports.

The application does not modify Frappe, ERPNext, or HRMS core source.

## Supported stack

- Frappe 16.x
- ERPNext 16.x
- HRMS 16.x
- Python 3.10+
- `openpyxl ~= 3.1.5`

## Features

### Attendance import

- Validates the workbook before posting any business records.
- Matches employees by Employee `name` or `employee_number`, according to the company policy.
- Creates native draft Attendance records using standard document APIs.
- Preserves native validation and workflow rules.
- Detects unknown, ambiguous, cross-company, conflicting, and duplicate attendance data.
- Prevents duplicate Attendance creation when an identical active record already exists.

### Project Mapping

- Reads the Project Name from cell `C2` of the selected worksheet. The existing merged range `C2:AE2` is supported.
- Matches the normalized name to exactly one existing ERPNext Project belonging to the import Company.
- Rejects missing, ambiguous, or cross-company Projects.
- Stores the matched Project in the app's Attendance custom field:

  - Field Label: `Project`
  - Fieldname: `custom_project`
  - Field Type: `Link`
  - Options: `Project`

- The app does not create Project master records and does not add Project tracking to Overtime Slips or Salary Slips.

### Native overtime

- Reads positive O.T values from the workbook.
- Creates one native draft HRMS `Overtime Slip` per employee and month.
- Stores one Overtime Detail per positive-overtime date.
- Uses the configured native Overtime Type and Standard Working Hours.
- Checks overlap, maximum allowed overtime, existing references, and duplicate data.
- Does not submit Overtime Slips, create Additional Salary, calculate payroll, or make payments.

Submitting an Overtime Slip later is a separate standard HRMS/payroll action.

### Attendance codes and policy rules

The parser supports the following N.T codes:

`EA`, `AB`, `SB`, `RH`, `HD`, `SL`, `UH`, `MD`, `P`, `F`, and `VACATION`.

The Monthly Attendance Import Policy explicitly controls how codes, blank values, zero values, overtime-only rows, shifts, holidays, and hours are handled. Unconfigured or ambiguous decisions block validation rather than being guessed.

### VACATION and approved leave

The policy has two VACATION handling modes:

- **Strict Annual** — the backward-compatible default. Each VACATION date requires exactly one submitted, approved, full-day Leave Application with the configured VACATION Leave Type.
- **Approved Leave Type** — each VACATION date uses the actual Leave Type from exactly one applicable submitted and approved full-day HRMS Leave Application.

The application checks the employee, date range, document status, approval status, overlap, and half-day conditions. It supports Annual Leave, Sick Leave, Leave Without Pay, and other configured leave types without hardcoding their names in Approved Leave Type mode.

The importer never creates, submits, approves, cancels, allocates, or modifies Leave Applications or Leave Allocations. It does not consume additional leave balances.

### Friday weekly off and holidays

- `F` is governed by the policy's F Weekly Off Rule.
- The configured rule can block the entry or skip Attendance while retaining positive overtime.
- Holiday handling uses the employee's effective HRMS Holiday List and the applicable Shift Type/assignment resolution.
- Holiday rules can block, follow the daily rule, or skip the date.
- The importer does not invent holidays, convert holidays to leave, or deduct additional leave days.

## Excel workbook format

The existing workbook format is unchanged. Employees are represented by contiguous N.T/O.T row pairs, with days across columns.

| Location | Requirement |
| --- | --- |
| `C2` | Literal Project Name. Merged `C2:AE2` is supported. |
| `M3` | Period header such as `MONTH OF -OCT- 2026`. |
| `D4` / `E4` | Exact headers `EMPLOYEE NUMBER` and `HOURS`. |
| `F5:AJ5` | Day headers 1–31; invalid calendar days must remain empty. |
| `D6`, `D8`, ... | Employee identifiers. |
| `E6/E7`, `E8/E9`, ... | N.T/O.T row pairs. |
| Daily N.T cells | Finite numeric hours or a supported attendance code. |
| Daily O.T cells | Finite numeric overtime hours. |

Additional parser rules:

- `.xlsx` files only.
- Maximum input size: 5 MiB.
- Maximum expanded workbook size: 30 MiB.
- Maximum ZIP members: 1,000.
- Maximum selected worksheet size: 2,000 rows × 80 columns.
- Maximum employees: 100 pairs.
- Formula cells and Excel error cells are rejected.
- Normal plus overtime hours cannot exceed 24.
- A single explicitly selected worksheet is imported. Other worksheets are not combined.
- Merged attendance codes may cover daily cells for one employee pair. Merged numeric hours and cross-employee code ranges are rejected.

Use **Download Template** from an import batch to obtain a compatible workbook template.

## Installation

Use a Frappe/ERPNext/HRMS v16 bench and verify the repository revision before installation.

```bash
cd /path/to/frappe-bench
bench get-app https://github.com/Luqman413-CSE/Monthly-Attendance-Import
bench --site your-site.localhost install-app monthly_attendance_import
bench --site your-site.localhost migrate
bench build --app monthly_attendance_import
bench --site your-site.localhost clear-cache
```

Back up the site's database and private files before installing or upgrading. Run migration when installing a revision containing schema or custom-field patches.

The app declares ERPNext and HRMS as required applications. It adds app-owned DocTypes and the Attendance Project custom field; it does not modify ERPNext or HRMS core source.

## Initial configuration

1. Create or verify Employees for the target Company.
2. Ensure the selected Employee identifier (`name` or `employee_number`) is unique and populated.
3. Create the matching ERPNext Project in the same Company. The workbook Project Name must resolve to exactly one Project.
4. Configure the effective HRMS Holiday List and any required Shift Assignment or Employee default shift.
5. Submit and approve Leave Applications before importing leave-coded dates.
6. Select an existing HRMS Overtime Type if O.T values will be imported.
7. Create one Monthly Attendance Import Policy per Company.
8. Configure all required hour, blank, zero, shift, holiday, code, overtime, F, and VACATION decisions before enabling the policy.

The importer does not create leave records, leave allocations, Projects, Overtime Types, salary records, or payroll transactions.

## User workflow

### Validate and preview

1. Open **Monthly Attendance Import** and create a new batch.
2. Select Company, Month, Year, and the exact Worksheet name when required.
3. Attach a private `.xlsx` workbook and save.
4. Click **Validate & Preview**.
5. Review the Results table and resolve every error.

Preview rows include employee, date, Excel code, source cells, hours, proposed Attendance status, Project, matched Leave Type, Leave Application, validation status, result, and message.

Preview is read-only with respect to business records. It uses savepoint rollback protection and does not create Attendance, Overtime, Leave, Project, or payroll records.

### Import

1. Confirm that the batch is in `Ready` status.
2. Click **Import**.
3. The server reparses the workbook and recomputes the plan.
4. The preview fingerprint is checked against the current workbook, policy, employee, workflow, Project, and overtime context.
5. Attendance records are inserted first, followed by grouped native Overtime Slips.
6. The batch becomes `Imported` only after the complete operation succeeds.

The import is atomic. If validation or posting fails, the transaction is rolled back and the failure is recorded for review.

## Import lifecycle and recovery

Normal lifecycle:

```text
Draft → Ready → Imported
                 ↓
              Reversed → Draft (new cycle) → Ready → Imported
```

The `cycle` field tracks repeated correction cycles under the same parent Import ID. Status is server-controlled; users cannot manually change `Reversed` back to `Draft`.

### Reverse Import

**Reverse Import** is available only to System Managers for successfully Imported batches.

The action requires a mandatory reason and confirmation. Before acting, the server verifies that linked Attendance and Overtime records:

- Are owned by the selected import result rows.
- Belong to the import Company and month.
- Have a supported document status.
- Do not have unsafe Attendance Request, payroll, or protected dependencies.

Draft Attendance and Overtime documents are deleted using supported Frappe APIs. Submitted records are cancelled using standard document APIs; submitted records are not forcibly deleted.

Original transaction IDs are retained in reversal fields before draft links are cleared. The original workbook, parent Import document, result history, and source-file audit trail remain available.

### Correct & Reimport

After successful reversal, System Managers can use **Correct & Reimport** under the same Import ID.

1. Enter a mandatory correction reason.
2. Upload a new private `.xlsx` workbook through the dedicated correction uploader.
3. Confirm the correction.
4. The server verifies that previous transactions are inactive and protected dependencies are clear.
5. Previous active results and transaction IDs are archived in Import History.
6. The cycle counter increments and active results reset.
7. The new source becomes the active source and the batch returns to `Draft`.
8. Validate & Preview and Import can run for the new cycle.

The original source file is never overwritten, detached, deleted, or reused by this action. A further correction requires the new cycle to be reversed safely first.

### Delete Import

**Delete Import** is a separate System Manager-only action. It requires a mandatory reason, confirmation, and dependency preflight.

Deletion is blocked when linked Attendance or Overtime records are active, missing, cross-company, or otherwise unsafe. The workflow creates an immutable **Monthly Attendance Import Deletion Log** containing the import ID, user, timestamp, reason, Company, period, and result-row count.

Deletion logs use the `MAI-DEL-.YYYY.-` naming series and cannot be manually created, edited, or deleted through normal Frappe document operations.

Source files are retained for audit. The authorized cleanup path may detach a source association while preserving the underlying private File; ordinary File deletion, replacement, or mutation remains protected.

## Source-file protection

The app registers File validation hooks for current and historical import source URLs.

- New Draft batches may upload, replace, or clear a private workbook.
- Imported and Reversed batches have a read-only Attachment field.
- Protected files cannot be deleted, replaced, made public, reassigned, or modified through normal File operations.
- Correct & Reimport always creates a new private File.
- The original workbook remains available for audit.

The warning `Import source files are retained for audit; upload a new attachment instead` is an intentional protection error when a protected source is changed or removed.

## Technical architecture

| Component | Responsibility |
| --- | --- |
| `parser.py` | Fixed workbook parsing, size limits, worksheet selection, merged cells, C2 Project extraction, codes, dates, and hours. |
| `rules.py` | Policy validation and attendance decisions. |
| `service.py` | Authorization, locking, Project/leave/holiday resolution, preview, import, reversal, correction, deletion, file protection, and transaction recovery. |
| `native_overtime.py` | Native Overtime Slip planning, overlap checks, details, and draft insertion. |
| `hooks.py` | Employee/document locks and File validation/trash protection. |
| Form JavaScript | Status/role-based actions, attachment locking, correction upload, confirmations, and progress display. |
| `patches/add_attendance_project_field.py` | Adds the app-owned Attendance `custom_project` Link field. |

Main DocTypes:

- **Monthly Attendance Import** — parent batch, source attachment, status, cycle, fingerprint, counters, active results, and history.
- **Monthly Attendance Import Result** — employee/date/code details, Project/leave information, transaction links, and reversed IDs.
- **Monthly Attendance Import History** — source, cycle, actor, fingerprint, action, and serialized snapshots.
- **Monthly Attendance Import Policy** — company-specific attendance, leave, holiday, shift, and overtime rules.
- **Monthly Attendance Import Deletion Log** — immutable audit for controlled import deletion.

The app uses standard Frappe document APIs, permission checks, employee locks, current locking reads, savepoints, and atomic commits. It does not use direct SQL deletion for business records or bypass document validation.

## Security and permissions

- HR Managers configure policies and work with imports according to DocType permissions.
- System Managers alone can Reverse Import, Correct & Reimport, and Delete Import.
- All server actions check user, document, Company, File, Employee, and related-record permissions.
- Deletion logs are read-only to users, including System Managers.
- Source workbooks are private and audit-protected.
- Leave Applications, Leave Allocations, Employees, Projects, Salary Slips, and payroll settings are not automatically modified.

## Testing

Run portable tests from the app checkout with the v16 Python environment:

```bash
PYTHONPATH=/path/to/monthly_attendance_import \
/path/to/v16/env/bin/python -m unittest discover \
  -s /path/to/monthly_attendance_import/tests -v
```

Run the app's Frappe tests on an isolated local test site only:

```bash
bench --site your-test-site.localhost run-tests --app monthly_attendance_import
```

Tests cover workbook parsing, merged Project/header behavior, policy rules, VACATION handling, native overtime, duplicate prevention, preview rollback, source-file protection, Reverse Import, Correct & Reimport, deletion-log immutability, permissions, and workspace routing. Do not enable integration tests against production data.

## Limitations

- One explicitly selected worksheet is processed per import; worksheets are not combined.
- The workbook layout is fixed and bounded by parser safety limits.
- Overtime Slips remain drafts; payroll submission and payment are outside this app.
- Leave approval, leave balances, allocations, holiday calendars, payroll, Salary Slips, and Project master maintenance remain standard ERPNext/HRMS responsibilities.
- Reverse, correction, and deletion operations are intentionally conservative and stop when protected dependencies cannot be safely handled.
- Test results depend on the exact app revision, Frappe site configuration, policies, and fixture data used.

## License

MIT
