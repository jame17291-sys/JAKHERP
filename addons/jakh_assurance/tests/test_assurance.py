from odoo import fields
from odoo.exceptions import AccessError, ValidationError
from odoo.tests import TransactionCase, tagged
from odoo.tests.common import new_test_user


@tagged('post_install', '-at_install', 'jakh')
class TestJakhAssurance(TransactionCase):
    @classmethod
    def setUpClass(cls):
        super().setUpClass()
        cls.author = new_test_user(cls.env, login='jakh.document.author', groups='jakh_core.group_technical')
        cls.reviewer = new_test_user(cls.env, login='jakh.document.reviewer', groups='jakh_core.group_quality')
        cls.project = cls.env['project.project'].create({'name': 'QA Assurance Project', 'company_id': cls.env.company.id, 'privacy_visibility': 'employees'})
        cls.document = cls.env['jakh.document'].with_user(cls.author).create({'name': 'Foundation Drawing', 'code': 'DRW-QA-01', 'project_id': cls.project.id})

    def _file(self, model, record):
        return self.env['ir.attachment'].with_user(self.author).create({
            'name': 'QA evidence.txt', 'res_model': model, 'res_id': record.id,
            'raw': b'Synthetic QA fixture only', 'mimetype': 'text/plain',
        })

    def test_revision_approval_and_attachment_lock(self):
        revision = self.env['jakh.document.revision'].with_user(self.author).create({
            'document_id': self.document.id, 'name': 'A', 'summary': 'Initial issue',
            'reviewer_id': self.reviewer.id, 'due_date': fields.Date.today(),
        })
        with self.assertRaises(ValidationError):
            revision.action_submit()
        attachment = self._file(revision._name, revision)
        revision.action_submit()
        with self.assertRaises(ValidationError):
            attachment.write({'name': 'changed.txt'})
        revision.with_user(self.reviewer).action_approve()
        self.assertEqual(revision.state, 'approved')
        self.assertEqual(revision.approved_by_id, self.reviewer)
        with self.assertRaises(ValidationError):
            attachment.unlink()
        with self.assertRaises(ValidationError):
            revision.write({'summary': 'overwritten'})
        with self.assertRaises(AccessError):
            revision.write({'state': 'draft'})

    def test_quality_action_independent_verification(self):
        case = self.env['jakh.assurance.case'].with_user(self.author).create({
            'name': 'Synthetic NCR', 'kind': 'ncr', 'project_id': self.project.id,
            'due_date': fields.Date.today(), 'description': '<p>QA inspection finding</p>',
        })
        case.action_submit()
        case.with_user(self.reviewer).action_start()
        case.write({'resolution': '<p>QA corrective action</p>'})
        case.action_resolve()
        with self.assertRaises(ValidationError):
            case.with_user(self.reviewer).action_close()
        attachment = self._file(case._name, case)
        case.with_user(self.reviewer).action_close()
        self.assertEqual(case.state, 'closed')
        with self.assertRaises(ValidationError):
            attachment.unlink()

    def test_lifecycle_fields_not_rpc_writeable(self):
        with self.assertRaises(AccessError):
            self.env['jakh.document.revision'].with_user(self.author).create({
                'document_id': self.document.id, 'name': 'B', 'summary': 'Unsafe direct approval',
                'reviewer_id': self.reviewer.id, 'due_date': fields.Date.today(), 'state': 'approved',
            })
