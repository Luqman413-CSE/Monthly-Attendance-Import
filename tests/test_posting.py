"""Focused service tests with mocked Frappe I/O; not database integration proof."""
import unittest
from contextlib import ExitStack
from datetime import date
from decimal import Decimal
from unittest.mock import MagicMock, patch

import frappe
from monthly_attendance_import import service as s
from monthly_attendance_import.parser import Entry
from monthly_attendance_import.rules import RuleError
from monthly_attendance_import.monthly_attendance_import.doctype.monthly_attendance_import_deletion_log.monthly_attendance_import_deletion_log import MonthlyAttendanceImportDeletionLog
from monthly_attendance_import.monthly_attendance_import.doctype.monthly_attendance_import.monthly_attendance_import import MonthlyAttendanceImport


class PostingTests(unittest.TestCase):
    def test_imported_and_reversed_batches_reject_attachment_changes(self):
        for status in ('Imported', 'Reversed'):
            with self.subTest(status=status):
                doc = MagicMock()
                previous = frappe._dict(company='Company A', month=10, year=2026,
                    attachment='/private/files/original.xlsx', worksheet=None, status=status)
                doc.get_doc_before_save.return_value = previous
                doc.flags = frappe._dict()
                doc.company = 'Company A'
                doc.month = 10
                doc.year = 2026
                doc.attachment = '/private/files/replacement.xlsx'
                doc.worksheet = None
                doc.get.side_effect = lambda field: getattr(doc, field, None)
                with patch.object(frappe, 'get_doc', return_value=MagicMock()):
                    with self.assertRaises(frappe.ValidationError):
                        MonthlyAttendanceImport.validate(doc)

    def test_attachment_ui_locks_imported_and_reversed_batches(self):
        from pathlib import Path
        path = Path(__file__).parents[1] / 'monthly_attendance_import' / 'monthly_attendance_import' / 'doctype' / 'monthly_attendance_import' / 'monthly_attendance_import.js'
        source = path.read_text()
        self.assertIn('["Imported", "Reversed"].includes(frm.doc.status)', source)
        self.assertIn('set_df_property("attachment", "read_only"', source)
        self.assertIn('toggle_enable("attachment", !attachment_locked)', source)
        self.assertIn('Correct & Reimport', source)
        self.assertIn('correct_import_upload', source)

    def test_reversed_batch_cannot_be_manually_changed_to_draft(self):
        doc = MagicMock()
        previous = frappe._dict(company='Company A', month=10, year=2026,
            attachment='/private/files/original.xlsx', worksheet=None, status='Reversed', cycle=1)
        doc.get_doc_before_save.return_value = previous
        doc.flags = frappe._dict()
        doc.company = 'Company A'
        doc.month = 10
        doc.year = 2026
        doc.attachment = previous.attachment
        doc.worksheet = None
        doc.status = 'Draft'
        doc.cycle = 1
        doc.get.side_effect = lambda field: getattr(doc, field, None)
        with patch.object(frappe, 'get_doc', return_value=MagicMock()):
            with self.assertRaises(frappe.ValidationError):
                MonthlyAttendanceImport.validate(doc)

    def test_protect_file_allows_unchanged_source_validation(self):
        doc = MagicMock()
        doc.get_doc_before_save.return_value = doc
        doc.is_new.return_value = False
        doc.attached_to_doctype = 'Monthly Attendance Import'
        doc.attached_to_name = 'MAI-1'
        doc.file_url = '/private/files/source.xlsx'
        doc.is_private = 1
        doc.flags = frappe._dict()
        doc.get.side_effect = lambda field: getattr(doc, field, None)
        with patch.object(s, 'import_source_file_urls', return_value={doc.file_url}):
            s.protect_file(doc, 'validate')

    def test_protect_file_does_not_warn_for_unrelated_import_attachment(self):
        doc = MagicMock()
        doc.is_new.return_value = False
        doc.attached_to_doctype = 'Monthly Attendance Import'
        doc.attached_to_name = 'MAI-1'
        doc.file_url = '/private/files/unrelated.pdf'
        doc.flags = frappe._dict()
        with patch.object(s, 'import_source_file_urls', return_value={'/private/files/source.xlsx'}):
            s.protect_file(doc, 'on_trash')

    def test_protect_file_still_blocks_current_and_historical_source_deletion(self):
        doc = MagicMock()
        doc.is_new.return_value = False
        doc.attached_to_doctype = 'Monthly Attendance Import'
        doc.attached_to_name = 'MAI-1'
        doc.file_url = '/private/files/source.xlsx'
        doc.flags = frappe._dict()
        with patch.object(s, 'import_source_file_urls', return_value={doc.file_url}):
            with self.assertRaises(frappe.ValidationError):
                s.protect_file(doc, 'on_trash')

    def test_reverse_import_requires_system_manager_reason_and_imported_status(self):
        with patch.object(frappe, 'get_roles', return_value=['HR Manager']), \
             patch.object(frappe, 'throw', side_effect=frappe.PermissionError):
            with self.assertRaises(frappe.PermissionError):
                s.reverse_import('MAI-1', 'wrong workbook')
        batch = MagicMock(status='Reversed')
        with patch.object(frappe, 'get_roles', return_value=['System Manager']), \
             patch.object(frappe, 'get_doc', return_value=batch):
            with self.assertRaises(frappe.ValidationError):
                s.reverse_import('MAI-1', 'again')

    def test_reverse_import_blocks_dependency_preflight(self):
        batch = MagicMock(status='Imported')
        with patch.object(frappe, 'get_roles', return_value=['System Manager']), \
             patch.object(frappe, 'get_doc', return_value=batch), \
             patch.object(s, 'reverse_import_dependencies', return_value={
                 'problems': ['Submitted Salary Slip SAL-1 overlaps an imported employee'],
                 'warnings': [], 'rows': [], 'transactions': [], 'submitted_salary_slips': []}), \
             patch.object(frappe, 'db', MagicMock()) as db:
            with self.assertRaises(frappe.ValidationError):
                s.reverse_import('MAI-1', 'payroll review')
            db.savepoint.assert_not_called()

    def test_reverse_import_deletes_drafts_and_preserves_original_ids(self):
        batch = MagicMock(name='MAI-1', status='Imported', attachment='/private/files/source.xlsx', fingerprint='fp')
        result = frappe._dict(name='RES-1', attendance='ATT-1', overtime_slip='OT-1',
            reversed_attendance=None, reversed_overtime_slip=None)
        batch.results = [result]
        attendance = MagicMock(docstatus=0)
        overtime = MagicMock(docstatus=0)
        plan = {'problems': [], 'warnings': ['leave is preserved'], 'rows': [result],
            'submitted_salary_slips': [], 'transactions': [
                dict(kind='Attendance', name='ATT-1', doc=attendance, action='Delete draft', employee='EMP-1', date='2026-10-01', leave_application='LA-1'),
                dict(kind='Overtime Slip', name='OT-1', doc=overtime, action='Delete draft', employee='EMP-1', date='2026-10-01', leave_application=None, payroll_reference=None)]}
        with patch.object(frappe, 'get_roles', return_value=['System Manager']), \
             patch.object(frappe, 'get_doc', return_value=batch), \
             patch.object(s, 'reverse_import_dependencies', return_value=plan), \
             patch.object(frappe, 'session', frappe._dict(user='administrator')), \
             patch.object(frappe, 'db', MagicMock()) as db:
            result_data = s.reverse_import('MAI-1', 'wrong workbook')
        self.assertEqual(result_data['status'], 'Reversed')
        self.assertEqual(result.reversed_attendance, 'ATT-1')
        self.assertEqual(result.reversed_overtime_slip, 'OT-1')
        self.assertIsNone(result.attendance)
        self.assertIsNone(result.overtime_slip)
        attendance.delete.assert_called_once()
        overtime.delete.assert_called_once()
        batch.save.assert_called_once()
        batch.append.assert_not_called()
        db.commit.assert_called_once()

    def test_reverse_import_cancels_submitted_transactions(self):
        batch = MagicMock(name='MAI-1', status='Imported', fingerprint='fp')
        batch.results = []
        attendance = MagicMock(docstatus=1)
        plan = {'problems': [], 'warnings': [], 'rows': [], 'submitted_salary_slips': [],
            'transactions': [dict(kind='Attendance', name='ATT-1', doc=attendance,
                action='Cancel submitted', employee='EMP-1', date='2026-10-01', leave_application=None)]}
        with patch.object(frappe, 'get_roles', return_value=['System Manager']), \
             patch.object(frappe, 'get_doc', return_value=batch), \
             patch.object(s, 'reverse_import_dependencies', return_value=plan), \
             patch.object(frappe, 'session', frappe._dict(user='administrator')), \
             patch.object(frappe, 'db', MagicMock()):
            s.reverse_import('MAI-1', 'cancel submitted test')
        attendance.cancel.assert_called_once()
        attendance.delete.assert_not_called()

    def test_reverse_import_rolls_back_when_transaction_action_fails(self):
        batch = MagicMock(name='MAI-1', status='Imported', fingerprint='fp')
        batch.results = []
        attendance = MagicMock(docstatus=0)
        attendance.delete.side_effect = ValueError('delete failed')
        plan = {'problems': [], 'warnings': [], 'rows': [], 'submitted_salary_slips': [],
            'transactions': [dict(kind='Attendance', name='ATT-1', doc=attendance,
                action='Delete draft', employee='EMP-1', date='2026-10-01', leave_application=None)]}
        with patch.object(frappe, 'get_roles', return_value=['System Manager']), \
             patch.object(frappe, 'get_doc', return_value=batch), \
             patch.object(s, 'reverse_import_dependencies', return_value=plan), \
             patch.object(frappe, 'session', frappe._dict(user='administrator')), \
             patch.object(frappe, 'db', MagicMock()) as db:
            with self.assertRaises(ValueError):
                s.reverse_import('MAI-1', 'rollback test')
        db.rollback.assert_called_once_with(save_point='monthly_import_reverse')

    def test_correction_requires_system_manager_and_reversed_status(self):
        with patch.object(frappe, 'get_roles', return_value=['HR Manager']):
            with self.assertRaises(frappe.PermissionError):
                s.correct_import_upload()
        batch = MagicMock(status='Imported')
        with patch.object(frappe, 'get_roles', return_value=['System Manager']), \
             patch.object(frappe, 'form_dict', frappe._dict(docname='MAI-1', reason='replace')), \
             patch.object(frappe, 'local', frappe._dict(uploaded_file=b'xlsx', uploaded_filename='corrected.xlsx')), \
             patch.object(frappe, 'get_doc', return_value=batch), \
             patch.object(frappe, 'throw', side_effect=ValueError):
            with self.assertRaises(ValueError):
                s.correct_import_upload()

    def test_correction_blocks_active_previous_transactions(self):
        row = frappe._dict(employee=None, attendance='ATT-1', overtime_slip=None)
        batch = MagicMock(status='Reversed', company='Company A', results=[row])
        with patch.object(frappe.db, 'get_value', return_value=frappe._dict(docstatus=1, company='Company A')), \
             patch.object(frappe, 'get_all', return_value=[]):
            problems = s.correction_dependencies(batch)
        self.assertTrue(any('ATT-1 is still active' in problem for problem in problems))

    def test_correction_archives_results_and_transaction_ids(self):
        row = MagicMock()
        row.as_dict.return_value = dict(name='RES-1', employee='EMP-1', attendance='ATT-1',
            reversed_attendance='ATT-OLD', result='Created')
        batch = MagicMock(cycle=1, attachment='/private/files/original.xlsx', fingerprint='old-fp', results=[row])
        batch.get.side_effect = lambda field: {'cycle': 1}.get(field)
        batch.append = MagicMock()
        with patch.object(frappe, 'session', frappe._dict(user='administrator')):
            cycle = s._correction_history(batch, 'wrong workbook', '/private/files/corrected.xlsx', 'new-fp')
        self.assertEqual(cycle, 1)
        entry = batch.append.call_args.args[1]
        details = __import__('json').loads(entry['details'])
        self.assertEqual(entry['action'], 'Correction Archived')
        self.assertEqual(details['results'][0]['attendance'], 'ATT-1')
        self.assertEqual(details['results'][0]['reversed_attendance'], 'ATT-OLD')
        self.assertEqual(entry['attachment'], '/private/files/original.xlsx')

    def test_correction_upload_preserves_original_source_and_starts_new_cycle(self):
        row = MagicMock()
        row.employee = None
        row.get.side_effect = lambda field: None
        row.as_dict.return_value = dict(name='RES-1', employee=None, attendance=None,
            overtime_slip=None, reversed_attendance='ATT-OLD', result='Created')
        batch = MagicMock(status='Reversed', name='MAI-1', company='Company A', cycle=1,
            attachment='/private/files/original.xlsx', fingerprint='old-fp', results=[row])
        batch.name = 'MAI-1'
        batch.get.side_effect = lambda field: {'cycle': 1}.get(field)
        source = MagicMock(file_url='/private/files/corrected.xlsx')
        source.attached_to_doctype = 'Monthly Attendance Import'
        source.attached_to_name = 'MAI-1'
        with patch.object(frappe, 'get_roles', return_value=['System Manager']), \
             patch.object(frappe, 'form_dict', frappe._dict(docname='MAI-1', reason='corrected source', is_private='1')), \
             patch.object(frappe, 'local', frappe._dict(uploaded_file=b'corrected workbook', uploaded_filename='corrected.xlsx')), \
             patch.object(frappe, 'get_doc', return_value=batch), \
             patch.object(s, 'import_source_file_urls', return_value={'/private/files/original.xlsx'}), \
             patch('frappe.utils.file_manager.save_file', return_value=source), \
             patch.object(s, 'now_datetime', return_value='2026-11-23 00:00:00'), \
             patch.object(frappe, 'session', frappe._dict(user='administrator')), \
             patch.object(frappe, 'db', MagicMock()) as db:
            result = s.correct_import_upload()
        self.assertEqual(result['status'], 'Draft')
        self.assertEqual(result['cycle'], 2)
        self.assertEqual(result['attachment'], '/private/files/corrected.xlsx')
        self.assertEqual(batch.attachment, '/private/files/corrected.xlsx')
        self.assertEqual(batch.status, 'Draft')
        batch.set.assert_any_call("results", [])
        batch.save.assert_called_once()
        db.commit.assert_called_once()

    def test_correction_rolls_back_when_parent_save_fails(self):
        batch = MagicMock(status='Reversed', name='MAI-1', company='Company A', cycle=1,
            attachment='/private/files/original.xlsx', fingerprint='old-fp', results=[])
        batch.name = 'MAI-1'
        batch.get.side_effect = lambda field: {'cycle': 1}.get(field)
        batch.save.side_effect = ValueError('save failed')
        source = MagicMock(file_url='/private/files/corrected.xlsx')
        source.attached_to_doctype = 'Monthly Attendance Import'
        source.attached_to_name = 'MAI-1'
        with patch.object(frappe, 'get_roles', return_value=['System Manager']), \
             patch.object(frappe, 'form_dict', frappe._dict(docname='MAI-1', reason='retry', is_private='1')), \
             patch.object(frappe, 'local', frappe._dict(uploaded_file=b'corrected workbook', uploaded_filename='corrected.xlsx')), \
             patch.object(frappe, 'get_doc', return_value=batch), \
             patch.object(s, 'import_source_file_urls', return_value={'/private/files/original.xlsx'}), \
             patch('frappe.utils.file_manager.save_file', return_value=source), \
             patch.object(s, 'now_datetime', return_value='2026-11-23 00:00:00'), \
             patch.object(frappe, 'session', frappe._dict(user='administrator')), \
             patch.object(frappe, 'db', MagicMock()) as db:
            with self.assertRaises(ValueError):
                s.correct_import_upload()
        db.rollback.assert_called_once_with(save_point='monthly_import_correct')
        self.assertEqual(batch.attachment, '/private/files/original.xlsx')

    def test_deletion_log_controller_rejects_manual_create_edit_and_delete(self):
        log = MonthlyAttendanceImportDeletionLog({'doctype': 'Monthly Attendance Import Deletion Log'})
        with self.assertRaises(frappe.PermissionError):
            log.validate()
        log.name = 'MAI-DEL-2026-00001'
        log.flags.from_monthly_import_delete = True
        with self.assertRaises(frappe.PermissionError):
            log.validate()
        with self.assertRaises(frappe.PermissionError):
            log.on_trash()

    def test_deletion_log_uses_year_naming_series_without_renaming_existing_logs(self):
        import json
        from pathlib import Path
        path = Path(__file__).parents[1] / 'monthly_attendance_import' / 'monthly_attendance_import' / 'doctype' / 'monthly_attendance_import_deletion_log' / 'monthly_attendance_import_deletion_log.json'
        definition = json.loads(path.read_text())
        self.assertEqual(definition['autoname'], 'naming_series:')
        self.assertEqual(definition['fields'][0]['fieldname'], 'naming_series')
        self.assertEqual(definition['fields'][0]['default'], 'MAI-DEL-.YYYY.-')
        self.assertNotIn('create', definition['permissions'][0])

    def test_delete_import_requires_system_manager_and_reason(self):
        with patch.object(frappe, 'get_roles', return_value=['HR Manager']):
            with self.assertRaises(frappe.PermissionError):
                s.delete_import('MAI-1', 'cleanup')
        with patch.object(frappe, 'get_roles', return_value=['System Manager']), \
             patch.object(frappe, 'throw', side_effect=ValueError('reason required')) as throw:
            with self.assertRaises(ValueError):
                s.delete_import('MAI-1', ' ')
            throw.assert_called_once()

    def test_delete_import_blocks_active_linked_transactions(self):
        batch = MagicMock(name='MAI-1', company='Company A', status='Imported')
        with patch.object(frappe, 'get_roles', return_value=['System Manager']), \
             patch.object(frappe, 'get_doc', return_value=batch), \
             patch.object(s, 'import_delete_dependencies', return_value=(['Attendance ATT-1 is still active'], 1)), \
             patch.object(s, 'detach_source_files_for_cleanup') as detach:
            with self.assertRaises(frappe.ValidationError):
                s.delete_import('MAI-1', 'test cleanup')
            detach.assert_not_called()
            batch.delete.assert_not_called()

    def test_delete_import_logs_reason_detaches_files_and_deletes_batch(self):
        batch = MagicMock(name='MAI-1', company='Company A', status='Imported', attachment='/private/files/a.xlsx')
        log = MagicMock()
        def get_doc(doctype, name=None, **kwargs):
            if isinstance(doctype, dict) and doctype.get('doctype') == 'Monthly Attendance Import Deletion Log':
                return log
            return log if doctype == 'Monthly Attendance Import Deletion Log' else batch
        with patch.object(frappe, 'get_roles', return_value=['System Manager']), \
             patch.object(frappe, 'get_doc', side_effect=get_doc), \
             patch.object(s, 'import_delete_dependencies', return_value=([], 12)), \
             patch.object(s, 'detach_source_files_for_cleanup', return_value=1), \
             patch.object(frappe, 'session', frappe._dict(user='administrator')), \
             patch.object(frappe, 'db', MagicMock()) as db:
            result = s.delete_import('MAI-1', 'remove invalid local test import')

        self.assertEqual(result, {'name': 'MAI-1', 'result_rows': 12, 'detached_files': 1})
        log.insert.assert_called_once_with(ignore_permissions=True)
        self.assertTrue(log.flags.from_monthly_import_delete)
        batch.delete.assert_called_once()
        db.commit.assert_called_once()

    def test_import_runs_immediately_without_enqueuing(self):
        batch = frappe._dict(name='BATCH', status='Ready', fingerprint='preview')
        def finish(doc):
            self.assertEqual(doc.requested_by, 'importer')
            self.assertEqual(doc.status, 'Ready')
            doc.status = 'Imported'
        with patch.object(s, 'load_batch', return_value=batch), \
             patch.object(s, 'save_owned'), patch.object(s, 'execute', side_effect=finish) as execute, \
             patch.object(frappe, 'enqueue') as enqueue, \
             patch.object(frappe, 'session', frappe._dict(user='importer')):
            self.assertEqual(s.start_import('BATCH'), {'status': 'Imported'})
            execute.assert_called_once_with(batch)
            enqueue.assert_not_called()
            self.assertEqual(s.start_import('BATCH'), {'status': 'Imported'})
            execute.assert_called_once()

    def test_legacy_removal_refuses_records_or_audit_links(self):
        from monthly_attendance_import.patches.remove_unused_overtime_entry import execute
        for records, linked in [(1, False), (0, True)]:
            with patch.object(frappe, 'db', MagicMock()) as db, \
                 patch.object(frappe, 'throw', side_effect=ValueError('blocked')), \
                 patch.object(frappe, 'delete_doc') as delete:
                db.exists.return_value = True
                db.count.return_value = records
                db.has_column.return_value = linked
                with self.assertRaises(ValueError): execute()
                delete.assert_not_called()

    def test_progress_permissions_and_committed_status(self):
        batch = MagicMock(status='Ready', fingerprint='preview')
        with patch.object(frappe, 'get_doc', return_value=batch), patch.object(frappe, 'cache', MagicMock()) as cache:
            batch.check_permission.side_effect = frappe.PermissionError('denied')
            with self.assertRaises(frappe.PermissionError): s.get_progress('PRIVATE')
            cache.get_value.assert_not_called()
            batch.check_permission.side_effect = None
            cache.get_value.return_value = dict(phase='Finalizing', completed=10, total=10, fingerprint='preview')
            progress = s.get_progress('PRIVATE')
            self.assertEqual(progress['status'], 'Ready')
            self.assertEqual(progress['completed'], 10)
            batch.fingerprint = 'changed'
            self.assertEqual(s.get_progress('PRIVATE')['completed'], 0)
            batch.status = 'Failed'
            result = s.get_progress('PRIVATE')
            self.assertEqual(result['phase'], 'Failed')
            self.assertEqual(result['completed'], 0)

    def test_progress_cache_failure_does_not_change_transaction(self):
        with patch.object(frappe, 'cache', MagicMock()) as cache, patch.object(frappe, 'db', MagicMock()) as db:
            cache.set_value.side_effect = RuntimeError('cache unavailable')
            s.report_progress(frappe._dict(name='BATCH'), 'Processing entries', 25, 100)
            db.commit.assert_not_called()
            db.rollback.assert_not_called()

    def test_preview_preserves_validation_error_when_savepoint_is_gone(self):
        batch = MagicMock(name='BATCH', status='Ready')
        db = MagicMock()
        db.rollback.side_effect = lambda **kwargs: (
            (_ for _ in ()).throw(RuntimeError('SAVEPOINT monthly_preview does not exist'))
            if kwargs else None
        )
        with patch.object(s, 'load_batch', return_value=batch), \
             patch.object(s, 'build_plan', return_value=([], ['native validation failed'], '')), \
             patch.object(s, 'record') as record, \
             patch.object(frappe, 'db', db), \
             patch.object(frappe, 'local', frappe._dict(message_log=[])):
            result = s.preview('BATCH')

        self.assertEqual(result['errors'], ['native validation failed'])
        self.assertEqual(record.call_args.args[3], ['native validation failed'])
        db.rollback.assert_any_call(save_point='monthly_preview')
        db.rollback.assert_any_call()

    def test_rollback_savepoint_falls_back_to_full_rollback(self):
        db = MagicMock()
        db.rollback.side_effect = [RuntimeError('SAVEPOINT missing'), None]
        with patch.object(frappe, 'db', db):
            error = s.rollback_savepoint('monthly_preview')

        self.assertEqual(str(error), 'SAVEPOINT missing')
        db.rollback.assert_has_calls([unittest.mock.call(save_point='monthly_preview'), unittest.mock.call()])

    def test_atomic_failure_clears_rolled_back_links(self):
        batch = MagicMock(fingerprint='fresh')
        attendance = MagicMock(status='Present', docstatus=0)
        attendance.name = 'ATT-NEW'
        overtime = MagicMock()
        overtime.insert.side_effect = ValueError('overtime permission denied')
        row = dict(employee_number='0007', attendance_date='2024-11-01', source_cells='F6/F7', result='Proposed')
        plan = [(row, attendance, overtime)]
        with patch.object(s, 'build_plan', return_value=(plan, [], 'fresh')), \
             patch.object(s, 'record') as record, patch.object(frappe, 'db', MagicMock()) as db:
            s.execute(batch)
            attendance.insert.assert_called_once()
            db.rollback.assert_called_once_with(save_point='monthly_post')
            self.assertNotIn('attendance', row)
            self.assertNotIn('overtime_entry', row)
            self.assertEqual(row['result'], 'Failed')
            self.assertEqual(record.call_args.args[-1], 'Failed')
            self.assertIn('F6/F7', record.call_args.args[3][0])

    def test_stale_preview_creates_nothing(self):
        batch = MagicMock(fingerprint='old')
        record_doc = MagicMock()
        row = dict(result='Proposed')
        with patch.object(s, 'build_plan', return_value=([(row, record_doc, None)], [], 'changed')), \
             patch.object(s, 'record') as record, patch.object(frappe, 'db', MagicMock()):
            s.execute(batch)
            record_doc.insert.assert_not_called()
            self.assertIn('stale', record.call_args.args[3][0])

    def test_whole_batch_validated_before_any_insert(self):
        doc = MagicMock()
        with patch.object(s, 'build_plan', return_value=([(dict(result='Proposed'), doc, None)], ['bad second employee'], 'x')), \
             patch.object(s, 'record'), patch.object(frappe, 'db', MagicMock()):
            s.execute(MagicMock())
            doc.insert.assert_not_called()

    def test_identical_retry_does_not_insert(self):
        batch = MagicMock(fingerprint='same')
        row = dict(result='Skipped', attendance='existing')
        with patch.object(s, 'build_plan', return_value=([(row, None, None)], [], 'same')), \
             patch.object(s, 'record') as record, patch.object(frappe, 'db', MagicMock()) as db:
            s.execute(batch)
            db.rollback.assert_not_called()
            self.assertEqual(record.call_args.args[-1], 'Imported')

    def test_employee_matching_unknown_ambiguous_company_and_permission(self):
        policy = frappe._dict(identifier_field='employee_number')
        for matches in ([], [frappe._dict(name='a'), frappe._dict(name='b')]):
            with patch.object(s, 'current_rows', return_value=matches), self.assertRaises(RuleError):
                s.resolve_employee('0007', policy, 'Company A')
        employee = MagicMock(company='Company B')
        with patch.object(s, 'current_rows', return_value=[frappe._dict(name='a')]), \
             patch.object(frappe, 'get_doc', return_value=employee):
            with self.assertRaisesRegex(RuleError, 'different company'):
                s.resolve_employee('0007', policy, 'Company A')
            employee.company = 'Company A'
            employee.check_permission.side_effect = frappe.PermissionError('denied')
            with self.assertRaises(frappe.PermissionError):
                s.resolve_employee('0007', policy, 'Company A')

    def test_project_resolution_is_exact_and_company_scoped(self):
        project = frappe._dict(name='PROJ-1', project_name='AR RJUM GOLD - MAADEN PROJECT (PEB-00045)',
            company='Company A', status='Open', modified='2026-11-22')
        project.check_permission = MagicMock()
        with patch.object(frappe, 'get_all', return_value=[project]), \
             patch.object(frappe, 'get_doc', return_value=project):
            resolved = s.resolve_project('  AR RJUM GOLD - MAADEN PROJECT (PEB-00045)  ', 'Company A')
        self.assertEqual(resolved.name, 'PROJ-1')
        project.check_permission.assert_called_once_with('read')

        for projects, company, message in [
            ([], 'Company A', 'does not match'),
            ([frappe._dict(name='PROJ-1', project_name='P', company='Company B')], 'Company A', 'another Company'),
            ([frappe._dict(name='1', project_name='P', company='Company A'),
              frappe._dict(name='2', project_name='P', company='Company A')], 'Company A', 'ambiguous'),
        ]:
            with self.subTest(message=message), patch.object(frappe, 'get_all', return_value=projects):
                with self.assertRaisesRegex(RuleError, message):
                    s.resolve_project('P', company)

    def test_plan_entry_assigns_project_to_new_attendance_and_preview_row(self):
        entry = Entry('0007', date(2026, 10, 22), None, None, 'P', 'ALAbraqi!F16/F17')
        employee = frappe._dict(name='E', company='C', status='Active', date_of_joining=None, relieving_date=None)
        project = frappe._dict(name='PROJ-1', project_name='Project One', company='C', status='Open')
        attendance = MagicMock(status='Present', docstatus=0)
        policy = frappe._dict(present_hours=8, holiday_rule='Use daily rule')
        with patch.object(s, 'resolve_shift', return_value=None), \
             patch.object(s, 'is_holiday_for', return_value=False), \
             patch.object(s, 'nowdate', return_value='2026-11-01'), \
             patch.object(s, 'current_rows', side_effect=[[], []]), \
             patch.object(frappe, 'new_doc', return_value=attendance), \
             patch.object(s, 'validate_document') as validate:
            row, proposed, _ = s.plan_entry(frappe._dict(company='C'), policy, entry, employee, project)
        self.assertEqual(row['project'], 'PROJ-1')
        self.assertIs(proposed, attendance)
        self.assertEqual(attendance.update.call_args.args[0]['custom_project'], 'PROJ-1')
        validate.assert_called_once_with(attendance)
        attendance.insert.assert_not_called()

    def test_permission_gate_before_batch_lock(self):
        batch = MagicMock()
        batch.check_permission.side_effect = frappe.PermissionError('denied')
        with patch.object(frappe, 'get_doc', return_value=batch) as get_doc, \
             patch.object(frappe, 'db', MagicMock()), patch.object(frappe, 'session', frappe._dict(user='test')):
            with self.assertRaises(frappe.PermissionError): s.load_batch('private-batch')
            self.assertEqual(get_doc.call_count, 1)

    def test_existing_attendance_state_and_hours(self):
        fields = dict(employee='E', company='C', attendance_date=date(2024, 11, 1), status='Present',
            shift=None, working_hours=8, docstatus=0, in_time=None, out_time=None, attendance_request=None)
        old = frappe._dict(fields)
        proposed = frappe._dict(fields)
        with patch('frappe.model.workflow.get_workflow_name', return_value=None):
            self.assertTrue(s.same_attendance(old, proposed))
            old.working_hours = 9
            self.assertFalse(s.same_attendance(old, proposed))
            old.working_hours, old.docstatus = 8, 1
            self.assertFalse(s.same_attendance(old, proposed))
            old.status = proposed.status = 'On Leave'
            old.working_hours = proposed.working_hours = 0
            old.leave_application = proposed.leave_application = 'APPROVED-LEAVE'
            old.leave_type = proposed.leave_type = 'ANNUAL LEAVE'
            self.assertFalse(s.same_attendance(old, proposed))
            self.assertTrue(s.same_attendance(old, proposed, approved_leave=True))
            old.leave_application = 'DIFFERENT-LEAVE'
            self.assertFalse(s.same_attendance(old, proposed, approved_leave=True))
            old.leave_application = proposed.leave_application
            with patch('frappe.model.workflow.get_workflow_name', return_value='Custom Workflow'):
                self.assertFalse(s.same_attendance(old, proposed, approved_leave=True))

    def test_leave_conflict_blocks_attendance(self):
        entry = Entry('0007', date(2024, 11, 1), Decimal(8), None, None, 'F6/F7')
        employee = frappe._dict(name='E', company='C', status='Active', date_of_joining=None, relieving_date=None)
        policy = frappe._dict(thresholds_confirmed=1, present_hours=8, half_day_hours=4, half_day_remainder='Absent')
        with patch.object(s, 'resolve_shift', return_value=None), \
             patch.object(s, 'is_holiday_for', return_value=False), \
             patch.object(s, 'nowdate', return_value='2024-12-01'), \
             patch.object(s, 'current_rows', return_value=[frappe._dict(name='leave')]):
            with self.assertRaisesRegex(RuleError, 'approved leave'):
                s.plan_entry(frappe._dict(company='C'), policy, entry, employee)

    def test_leave_code_requires_selected_type_and_approved_application(self):
        employee = frappe._dict(name='E', company='C', status='Active')
        for code, expected in [('VACATION', 'ANNUAL LEAVE'), ('SL', 'Sick Leave')]:
            entry = Entry('0007', date(2024, 11, 1), None, None, code, 'F6/F7')
            policy = frappe._dict({code.lower() + '_rule': 'Approved Leave', code.lower() + '_leave_type': expected})
            with patch.object(s, 'resolve_shift', return_value=None), \
                 patch.object(s, 'is_holiday_for', return_value=False), \
                 patch.object(s, 'nowdate', return_value='2024-12-01'), \
                 patch.object(frappe, 'get_doc', return_value=MagicMock()) as get_doc, \
                 patch.object(s, 'current_rows', return_value=[]) as rows:
                with self.assertRaisesRegex(RuleError, expected):
                    s.plan_entry(frappe._dict(company='C'), policy, entry, employee)
                get_doc.return_value.check_permission.assert_not_called()
                rows.return_value = [frappe._dict(half_day=0, leave_type='Wrong Type')]
                with self.assertRaisesRegex(RuleError, 'approved leave is Wrong Type'):
                    s.plan_entry(frappe._dict(company='C'), policy, entry, employee)
                policy[code.lower() + '_leave_type'] = None
                with self.assertRaisesRegex(RuleError, 'Select the'):
                    s.plan_entry(frappe._dict(company='C'), policy, entry, employee)

    def test_vacation_strict_annual_and_approved_leave_type_modes(self):
        employee = frappe._dict(name='E')
        day = date(2026, 10, 22)
        for mode, leave_type in [('Strict Annual', 'Annual'), ('Approved Leave Type', 'sick leave'),
                                 ('Approved Leave Type', 'Leave Without Pay')]:
            with self.subTest(mode=mode, leave_type=leave_type):
                application = frappe._dict(name='L-' + leave_type, leave_type=leave_type,
                    docstatus=1, status='Approved', half_day=0, half_day_date=None)
                leave_doc, type_doc = MagicMock(), MagicMock(include_holiday=1)
                policy = frappe._dict(vacation_handling_mode=mode, vacation_leave_type='Annual')
                with patch.object(s, 'current_rows', return_value=[application]), \
                     patch.object(frappe, 'get_doc', side_effect=[leave_doc, type_doc]):
                    resolved, resolved_type = s.resolve_leave_application(employee, day, policy, 'VACATION')
                self.assertEqual(resolved.name, application.name)
                self.assertIs(resolved_type, type_doc)
                leave_doc.check_permission.assert_called_once_with('read')
                type_doc.check_permission.assert_called_once_with('read')

    def test_vacation_strict_annual_rejects_non_annual_leave(self):
        application = frappe._dict(name='SICK', leave_type='sick leave', docstatus=1,
            status='Approved', half_day=0, half_day_date=None)
        with patch.object(s, 'current_rows', return_value=[application]), \
             patch.object(frappe, 'get_doc') as get_doc:
            with self.assertRaisesRegex(RuleError, 'requires Annual; approved leave is sick leave'):
                s.resolve_leave_application(frappe._dict(name='E'), date(2026, 10, 22),
                    frappe._dict(vacation_handling_mode='Strict Annual', vacation_leave_type='Annual'), 'VACATION')
            get_doc.assert_not_called()

    def test_vacation_missing_unapproved_cancelled_overlapping_and_half_day(self):
        day = date(2026, 10, 25)
        cases = [
            ([], 'none covers'),
            ([frappe._dict(name='DRAFT', leave_type='Annual', docstatus=0, status='Draft')],
             'not submitted and approved'),
            ([frappe._dict(name='CANCELLED', leave_type='Annual', docstatus=2, status='Approved')],
             'not submitted and approved'),
            ([frappe._dict(name='A', leave_type='Annual', docstatus=1, status='Approved'),
              frappe._dict(name='B', leave_type='sick leave', docstatus=1, status='Approved')],
             'overlapping'),
            ([frappe._dict(name='HALF', leave_type='Annual', docstatus=1, status='Approved',
                half_day=1, half_day_date=day)], 'half-day'),
        ]
        for applications, message in cases:
            with self.subTest(message=message), patch.object(s, 'current_rows', return_value=applications), \
                 patch.object(frappe, 'get_doc') as get_doc:
                with self.assertRaisesRegex(RuleError, message):
                    s.resolve_leave_application(frappe._dict(name='E'), day,
                        frappe._dict(vacation_handling_mode='Approved Leave Type'), 'VACATION')
                get_doc.assert_not_called()

    def test_vacation_can_use_different_approved_leave_types_by_date(self):
        employee = frappe._dict(name='E')
        annual = frappe._dict(name='ANNUAL-APP', leave_type='Annual', docstatus=1,
            status='Approved', half_day=0, half_day_date=None)
        sick = frappe._dict(name='SICK-APP', leave_type='sick leave', docstatus=1,
            status='Approved', half_day=0, half_day_date=None)
        with patch.object(s, 'current_rows', side_effect=[[annual], [sick]]), \
             patch.object(frappe, 'get_doc', side_effect=[MagicMock(), MagicMock(), MagicMock(), MagicMock()]):
            first, _ = s.resolve_leave_application(employee, date(2026, 10, 21),
                frappe._dict(vacation_handling_mode='Approved Leave Type'), 'VACATION')
            second, _ = s.resolve_leave_application(employee, date(2026, 10, 22),
                frappe._dict(vacation_handling_mode='Approved Leave Type'), 'VACATION')
        self.assertEqual((first.leave_type, second.leave_type), ('Annual', 'sick leave'))

    def test_vacation_preview_row_contains_leave_match_without_inserting(self):
        entry = Entry('0007', date(2026, 10, 22), None, None, 'VACATION', 'F16/F17')
        employee = frappe._dict(name='E', status='Active', date_of_joining=None, relieving_date=None)
        policy = frappe._dict(vacation_rule='Approved Leave', vacation_handling_mode='Approved Leave Type',
            holiday_rule='Use daily rule')
        application = frappe._dict(name='SICK-APP', leave_type='sick leave', docstatus=1,
            status='Approved', half_day=0, half_day_date=None)
        attendance = MagicMock(status='On Leave', docstatus=0)
        with patch.object(s, 'resolve_shift', return_value=None), \
             patch.object(s, 'is_holiday_for', return_value=False), \
             patch.object(s, 'nowdate', return_value='2026-11-01'), \
             patch.object(s, 'current_rows', side_effect=[[application], []]), \
             patch.object(frappe, 'get_doc', side_effect=[MagicMock(), MagicMock(include_holiday=1)]), \
             patch.object(frappe, 'new_doc', return_value=attendance), \
             patch.object(s, 'validate_document') as validate:
            row, proposed, _ = s.plan_entry(frappe._dict(company='C'), policy, entry, employee)
        self.assertEqual(row['matched_leave_type'], 'sick leave')
        self.assertEqual(row['leave_application'], 'SICK-APP')
        self.assertEqual(row['validation_status'], 'Valid')
        self.assertEqual(row['proposed_status'], 'On Leave')
        self.assertIs(proposed, attendance)
        validate.assert_called_once_with(attendance)
        attendance.insert.assert_not_called()

    def test_approved_leave_excludes_holidays_without_creating_attendance(self):
        entry = Entry('0007', date(2024, 11, 1), None, None, 'VACATION', 'F6/F7')
        employee = frappe._dict(name='E', company='C', status='Active')
        policy = frappe._dict(vacation_rule='Approved Leave', vacation_leave_type='Annual', holiday_rule='Use daily rule')
        leave = frappe._dict(name='APPROVED', half_day=0, leave_type='Annual')
        with patch.object(s, 'resolve_shift', return_value=None), \
             patch.object(s, 'is_holiday_for', return_value=True), \
             patch.object(s, 'nowdate', return_value='2024-12-01'), \
             patch.object(frappe, 'get_doc', return_value=MagicMock(include_holiday=0)), \
             patch.object(s, 'current_rows', side_effect=[[leave], []]), \
             patch.object(frappe, 'new_doc') as new_doc:
            row, attendance, overtime = s.plan_entry(frappe._dict(company='C'), policy, entry, employee)
            self.assertEqual(row['result'], 'Skipped')
            self.assertIn('Holiday excluded', row['message'])
            self.assertIsNone(attendance)
            new_doc.assert_not_called()

    def test_all_native_writers_use_same_lock(self):
        from monthly_attendance_import.hooks import doc_events
        for dt in ('Attendance', 'Leave Application', 'Shift Assignment'):
            self.assertEqual(doc_events[dt]['before_validate'], 'monthly_attendance_import.service.lock_employee_document')
        table = MagicMock()
        qb = MagicMock()
        qb.DocType.return_value = table
        doc = MagicMock()
        doc.get.return_value = 'EMP'
        doc.is_new.return_value = True
        with patch.object(frappe, 'qb', qb):
            s.lock_employee_document(doc)
            qb.from_.return_value.select.return_value.where.return_value.for_update.assert_called_once()


if __name__ == '__main__':
    unittest.main()
