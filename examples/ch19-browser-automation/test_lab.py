"""Contract tests need only Python 3.11 standard library."""
import unittest

import lab
from server import Approval, Context, Denied, State, validate_approval


class ContractTests(unittest.TestCase):
    def setUp(self):
        self.state = State()
        self.preview = self.state.prepare({"title": "VPN 无法连接", "description": "证书已过期"})
        self.context = Context()
        self.approval = self.state.approve(self.context, self.preview)

    def test_approval_is_bound_to_tenant_and_actor(self):
        for context in (Context("tenant-b"), Context(actor="employee-8")):
            with self.subTest(context=context), self.assertRaises(Denied):
                validate_approval(context, self.preview, self.approval)
        self.assertEqual(self.state.create_calls, 0)

    def test_changed_action_cannot_reuse_approval(self):
        with self.assertRaises(Denied):
            validate_approval(self.context, {**self.preview, "action": "delete_all"}, self.approval)

    def test_changed_draft_version_cannot_reuse_approval(self):
        updated = self.state.prepare({"title": "VPN 无法连接", "description": "证书已过期"})
        with self.assertRaises(Denied):
            validate_approval(self.context, updated, self.approval)
        self.assertEqual(self.state.create_calls, 0)

    def test_changed_content_cannot_reuse_approval(self):
        for key, value in (("title", "其他故障"), ("client_reference", "op-other")):
            with self.subTest(key=key), self.assertRaises(Denied):
                validate_approval(self.context, {**self.preview, key: value}, self.approval)

    def test_missing_or_expired_approval_is_rejected(self):
        for approval in (None, Approval(self.approval.preview_hash, 0)):
            with self.subTest(approval=approval), self.assertRaises(Denied):
                validate_approval(self.context, self.preview, approval)

    def test_site_rejects_an_outdated_preview_before_effect(self):
        self.state.prepare({"title": "新标题", "description": "新描述"})
        with self.assertRaises(Denied):
            self.state.submit(self.preview)
        self.assertEqual(self.state.create_calls, 0)

    def test_receipt_requires_every_correlation_field(self):
        receipt = self.state.submit(self.preview)
        self.assertEqual(lab.verify_receipt(receipt, self.preview), "TICKET-000001")
        for key in self.preview:
            wrong = {**receipt, key: "wrong"}
            with self.subTest(key=key), self.assertRaises(Denied):
                lab.verify_receipt(wrong, self.preview)

    def test_receipt_without_business_id_is_not_success(self):
        with self.assertRaises(Denied):
            lab.verify_receipt(self.preview, self.preview)

    def test_unexpected_fields_cannot_inject_identity(self):
        with self.assertRaises(Denied):
            self.state.prepare({"title": "故障", "description": "说明", "tenant": "tenant-b"})


if __name__ == "__main__":
    unittest.main()
