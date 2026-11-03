"""Tests for the chapter 14 tool-execution boundary lab."""
from pathlib import Path
import json
import sqlite3
import tempfile
import unittest

import lab


class ToolCallingLabTests(unittest.TestCase):
    def setUp(self):
        self.directory = tempfile.TemporaryDirectory(prefix="tool-calling-test-")
        self.addCleanup(self.directory.cleanup)
        self.runtime_db = Path(self.directory.name) / "runtime.db"
        self.provider_db = Path(self.directory.name) / "provider.db"
        self.clock = [1_000]
        self.principal = lab.default_principal()
        self.runtime = lab.ToolRuntime(
            self.runtime_db,
            self.provider_db,
            self.principal,
            now_fn=lambda: self.clock[0],
        )

    def call(self, call_id, name, arguments, *, runtime=None):
        target = runtime or self.runtime
        return target.execute_call(lab.make_call(call_id, name, arguments))

    def preview(self, *, title="VPN 反复断线", description="客户端显示 TLS 握手超时。"):
        result = self.call(
            "preview",
            "tickets.preview",
            {"title": title, "description": description, "priority": "normal"},
        )
        self.assertEqual(result["status"], "ok", result)
        return result["data"]

    def approve(self, draft, *, expires_at=1_300):
        return self.runtime.approve_from_ui(
            draft_id=draft["draft_id"],
            expected_version=draft["version"],
            expected_hash=draft["payload_hash"],
            action="tickets.submit",
            expires_at=expires_at,
        )

    @staticmethod
    def submit_args(draft, approval):
        return {"draft_id": draft["draft_id"], "approval_id": approval["approval_id"]}

    def test_json_boundary_rejects_invalid_structure_depth_and_unicode(self):
        duplicate = self.runtime.execute_call(
            '{"call_id":"x","name":"tickets.list_own","arguments":{"limit":1,"limit":2}}'
        )
        nonfinite = self.runtime.execute_call(
            '{"call_id":"x","name":"tickets.list_own","arguments":{"limit":NaN}}'
        )
        oversized = self.runtime.execute_call(" " * (lab.MAX_CALL_BYTES + 1))
        nonobject = self.runtime.execute_call("[]")
        deeply_nested = self.runtime.execute_call(
            '{"call_id":"x","name":"tickets.list_own","arguments":{"limit":'
            + "[" * 1_100 + "0" + "]" * 1_100 + "}}"
        )
        lone_surrogate = self.runtime.execute_call(
            r'{"call_id":"x","name":"tickets.list_own","arguments":{"limit":"\ud800"}}'
        )
        self.assertEqual(duplicate["code"], "invalid_json")
        self.assertEqual(nonfinite["code"], "invalid_json")
        self.assertEqual(oversized["code"], "request_too_large")
        self.assertEqual(nonobject["code"], "call_must_be_object")
        self.assertEqual(deeply_nested["code"], "invalid_json")
        self.assertEqual(lone_surrogate["code"], "invalid_json")
        self.assertTrue(all(item["status"] == "error"
                            for item in (duplicate, nonfinite, oversized, nonobject,
                                         deeply_nested, lone_surrogate)))

    def test_router_uses_explicit_tool_and_principal_allowlists(self):
        restricted = lab.ToolRuntime(
            self.runtime_db,
            self.provider_db,
            lab.Principal("acme", "reader", frozenset({"tickets.list_own"})),
            now_fn=lambda: self.clock[0],
        )
        denied = self.call(
            "preview", "tickets.preview",
            {"title": "VPN 故障", "description": "请协助排查连接。", "priority": "normal"},
            runtime=restricted,
        )
        unknown = self.call("admin", "tickets.delete_all", {}, runtime=restricted)
        identity_injection = self.call(
            "identity", "tickets.list_own", {"limit": 10, "tenant_id": "other"}
        )
        self.assertEqual(denied["code"], "tool_not_allowed")
        self.assertEqual(unknown["code"], "tool_not_allowed")
        self.assertEqual(identity_injection["code"], "invalid_arguments")
        with sqlite3.connect(self.runtime_db) as db:
            self.assertEqual(db.execute("SELECT COUNT(*) FROM drafts").fetchone()[0], 0)

    def test_read_tool_is_scoped_to_authenticated_principal(self):
        own_payload = {"title": "自己的工单", "description": "自己的工单正文", "priority": "normal"}
        other_payload = {"title": "别人的工单", "description": "别人的工单正文", "priority": "high"}
        other = lab.default_principal(tenant_id="acme", user_id="employee-8")
        lab.provider_submit(
            self.provider_db, operation_id="op-own", principal=self.principal,
            payload=own_payload, expected_hash=lab.payload_hash(own_payload), now=900,
        )
        lab.provider_submit(
            self.provider_db, operation_id="op-other", principal=other,
            payload=other_payload, expected_hash=lab.payload_hash(other_payload), now=901,
        )
        result = self.call("list", "tickets.list_own", {"limit": 10})
        self.assertEqual(result["status"], "ok")
        self.assertEqual([item["title"] for item in result["data"]["items"]], ["自己的工单"])
        self.assertEqual(result["data"]["total_count"], 1)

    def test_approval_binds_identity_action_version_hash_and_expiry(self):
        draft = self.preview()
        approval = self.approve(draft)
        other_runtime = lab.ToolRuntime(
            self.runtime_db,
            self.provider_db,
            lab.default_principal(user_id="employee-8"),
            now_fn=lambda: self.clock[0],
        )
        with self.assertRaisesRegex(lab.ToolFailure, "draft_not_found"):
            other_runtime.approve_from_ui(
                draft_id=draft["draft_id"], expected_version=1,
                expected_hash=draft["payload_hash"], action="tickets.submit", expires_at=1_300,
            )
        with self.assertRaisesRegex(lab.ToolFailure, "approval_action_not_allowed"):
            self.runtime.approve_from_ui(
                draft_id=draft["draft_id"], expected_version=1,
                expected_hash=draft["payload_hash"], action="tickets.delete", expires_at=1_300,
            )
        revised = self.call(
            "revise", "tickets.revise",
            {"draft_id": draft["draft_id"], "expected_version": 1,
             "title": "VPN 只在公司断线", "description": "补充了新的诊断条件。",
             "priority": "high"},
        )
        self.assertEqual(revised["code"], "preview_revised")
        stale = self.call("submit-stale", "tickets.submit", self.submit_args(draft, approval))
        self.assertEqual((stale["status"], stale["code"]), ("error", "approval_stale"))

        current = revised["data"]
        expiring = self.runtime.approve_from_ui(
            draft_id=current["draft_id"], expected_version=current["version"],
            expected_hash=current["payload_hash"], action="tickets.submit", expires_at=1_001,
        )
        self.clock[0] = 1_001
        expired = self.call("submit-expired", "tickets.submit",
                            self.submit_args(current, expiring))
        self.assertEqual((expired["status"], expired["code"]),
                         ("error", "approval_expired"))
        self.assertEqual(lab.provider_stats(self.provider_db)["submit_calls"], 0)

    def test_server_operation_id_is_stable_and_success_is_reused(self):
        draft = self.preview()
        first_approval = self.approve(draft)
        second_approval = self.approve(draft)
        self.assertEqual(first_approval["operation_id"], second_approval["operation_id"])
        args = self.submit_args(draft, first_approval)
        first = self.call("submit-1", "tickets.submit", args)
        second = self.call("submit-2", "tickets.submit", args)
        self.assertEqual(first["code"], "submitted")
        self.assertEqual(second["code"], "already_submitted")
        self.assertEqual(first["data"]["receipt"], second["data"]["receipt"])
        self.assertEqual(lab.provider_stats(self.provider_db),
                         {"tickets": 1, "submit_calls": 1, "lookup_calls": 0})

    def test_lost_receipt_becomes_unknown_and_reconciles_without_replay(self):
        self.runtime.lose_next_receipt = True
        draft = self.preview()
        approval = self.approve(draft)
        args = self.submit_args(draft, approval)
        first = self.call("submit-1", "tickets.submit", args)
        self.assertEqual((first["status"], first["code"]), ("unknown", "outcome_unknown"))
        self.assertFalse(first["retryable"])
        self.assertEqual(lab.provider_stats(self.provider_db)["submit_calls"], 1)

        replay = self.call("submit-2", "tickets.submit", args)
        self.assertEqual((replay["status"], replay["code"]),
                         ("unknown", "reconciliation_required"))
        self.assertEqual(lab.provider_stats(self.provider_db)["submit_calls"], 1)

        self.clock[0] = 1_301  # Expiry blocks a new write, but not receipt reconciliation.
        reconciled = self.call("verify", "tickets.reconcile", args)
        self.assertEqual((reconciled["status"], reconciled["code"]), ("ok", "reconciled"))
        self.assertEqual(reconciled["data"]["operation_id"], approval["operation_id"])
        self.assertEqual(lab.provider_stats(self.provider_db),
                         {"tickets": 1, "submit_calls": 1, "lookup_calls": 1})

    def test_provider_transaction_deduplicates_and_rejects_payload_conflict(self):
        payload = {"title": "VPN 故障", "description": "连接反复中断。", "priority": "normal"}
        digest = lab.payload_hash(payload)
        created = lab.provider_submit(
            self.provider_db, operation_id="stable-op", principal=self.principal,
            payload=payload, expected_hash=digest, now=900,
        )
        duplicate = lab.provider_submit(
            self.provider_db, operation_id="stable-op", principal=self.principal,
            payload=payload, expected_hash=digest, now=901,
        )
        changed = {**payload, "priority": "high"}
        with self.assertRaises(lab.ProviderConflict):
            lab.provider_submit(
                self.provider_db, operation_id="stable-op", principal=self.principal,
                payload=changed, expected_hash=lab.payload_hash(changed), now=902,
            )
        self.assertEqual(created["receipt_id"], duplicate["receipt_id"])
        self.assertTrue(duplicate["deduplicated"])
        self.assertEqual(lab.provider_stats(self.provider_db),
                         {"tickets": 1, "submit_calls": 3, "lookup_calls": 0})

    def test_large_read_result_is_structurally_truncated_and_marked_partial(self):
        for index in range(20):
            payload = {
                "title": f"工单 {index:02d} " + "很长的标题" * 12,
                "description": "用于测试结果上限。",
                "priority": "normal",
            }
            lab.provider_submit(
                self.provider_db, operation_id=f"bulk-{index}", principal=self.principal,
                payload=payload, expected_hash=lab.payload_hash(payload), now=900 + index,
            )
        result = self.call("list", "tickets.list_own", {"limit": 100})
        self.assertEqual((result["status"], result["code"]),
                         ("partial", "result_truncated"))
        self.assertTrue(result["truncated"])
        self.assertLess(result["data"]["returned_count"], result["data"]["total_count"])
        self.assertTrue(result["data"]["has_more"])
        self.assertLessEqual(len(lab.canonical_json(result).encode("utf-8")), lab.MAX_RESULT_BYTES)
        json.loads(lab.canonical_json(result))  # The boundary returns valid JSON, not byte slicing.

        long_preview = self.call(
            "long-preview", "tickets.preview",
            {"title": "包含长正文的草稿", "description": "长" * 600, "priority": "normal"},
        )
        self.assertEqual((long_preview["status"], long_preview["code"]),
                         ("partial", "result_truncated"))
        self.assertIn("full_result_hash", long_preview["data"])
        self.assertEqual(long_preview["data"]["preview_summary"]["description_chars"], 600)
        self.assertLessEqual(len(lab.canonical_json(long_preview).encode("utf-8")),
                             lab.MAX_RESULT_BYTES)

    def test_business_rejections_never_look_like_success(self):
        missing = self.call(
            "missing", "tickets.submit",
            {"draft_id": "draft-9999", "approval_id": "approval-missing"},
        )
        invalid = self.call("bad-limit", "tickets.list_own", {"limit": 0})
        self.assertEqual((missing["status"], missing["code"]),
                         ("error", "draft_not_found"))
        self.assertEqual((invalid["status"], invalid["code"]),
                         ("error", "invalid_arguments"))
        self.assertIsNone(missing["data"])
        self.assertEqual(lab.provider_stats(self.provider_db)["tickets"], 0)

    def test_audit_records_metadata_without_request_or_ticket_body(self):
        secret_title = "SECRET-TITLE VPN 故障"
        secret_body = "SECRET-BODY 仅用于验证审计最小化"
        self.preview(title=secret_title, description=secret_body)
        with sqlite3.connect(self.runtime_db) as db:
            columns = [row[1] for row in db.execute("PRAGMA table_info(audit_events)")]
            rows = db.execute("SELECT * FROM audit_events").fetchall()
        serialized = json.dumps(rows, ensure_ascii=False)
        self.assertNotIn("payload", columns)
        self.assertNotIn("arguments", columns)
        self.assertNotIn("request", columns)
        self.assertNotIn(secret_title, serialized)
        self.assertNotIn(secret_body, serialized)
        self.assertEqual(len(rows), 1)


if __name__ == "__main__":
    unittest.main()
