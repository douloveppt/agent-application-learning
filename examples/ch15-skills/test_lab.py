from __future__ import annotations

import json
from pathlib import Path
import shutil
import sys
import tempfile
import unittest


HERE = Path(__file__).resolve().parent
sys.path.insert(0, str(HERE))

import lab  # noqa: E402


TOOLS = frozenset({"tickets.list_own", "ticket_drafts.create"})


class SkillHostTests(unittest.TestCase):
    def host(self, **changes) -> lab.SkillHost:
        values = {
            "principal": lab.default_principal(),
            "available_tools": TOOLS,
        }
        values.update(changes)
        return lab.SkillHost(**values)

    def test_catalog_requires_no_skill_package_read(self):
        with tempfile.TemporaryDirectory(prefix="ch15-empty-") as temp_dir:
            host = self.host(content_root=Path(temp_dir))
            self.assertEqual(
                host.catalog()[0]["name"],
                "enterprise-it-triage",
            )

    def test_normal_run_loads_only_entrypoint_and_selected_reference(self):
        result = self.host().run(lab.normal_request())
        self.assertEqual(result["status"], "ok")
        self.assertEqual(result["code"], "draft_ready")
        self.assertEqual(
            result["context"]["loaded_files"],
            ["SKILL.md", "references/vpn.md"],
        )
        self.assertIn(
            "references/email-access.md",
            result["integrity"]["verified_files"],
        )
        self.assertEqual(result["tool_calls"]["executed_count"], 2)
        self.assertEqual(result["artifact"]["status"], "draft_only")

    def test_identity_is_host_owned_and_evidence_is_scoped(self):
        result = self.host().run(lab.normal_request())
        self.assertEqual(result["evidence"]["matching_ticket_ids"], ["IT-2048"])
        serialized = json.dumps(result, ensure_ascii=False)
        self.assertNotIn("IT-9999", serialized)
        self.assertNotIn("IT-7000", serialized)

    def test_artifact_has_version_content_and_handler_provenance(self):
        result = self.host().run(lab.normal_request())
        provenance = result["artifact"]["provenance"]
        self.assertEqual(provenance["version"], "1.0.0")
        self.assertEqual(
            provenance["package_digest"],
            result["integrity"]["package_digest"],
        )
        self.assertEqual(provenance["handler"]["trust"]["kind"], "host_builtin")

    def test_missing_input_stops_before_loading_or_calls(self):
        result = self.host().run(lab.normal_request(symptom="  "))
        self.assertEqual(result["status"], "needs_input")
        self.assertEqual(result["missing_fields"], ["symptom"])
        self.assertEqual(result["context"]["loaded_files"], [])
        self.assertEqual(result["tool_calls"]["executed_count"], 0)

    def test_ambiguous_modes_require_clarification(self):
        result = self.host().run(
            lab.normal_request(mode_candidates=("vpn", "email-access"))
        )
        self.assertEqual(result["status"], "clarification_required")
        self.assertEqual(result["code"], "ambiguous_mode")
        self.assertEqual(result["context"]["loaded_files"], [])
        self.assertEqual(result["tool_calls"]["executed_count"], 0)

    def test_unavailable_tool_stops_before_loading_or_calls(self):
        result = self.host(
            available_tools=frozenset({"tickets.list_own"})
        ).run(lab.normal_request())
        self.assertEqual(result["code"], "tool_unavailable")
        self.assertEqual(result["context"]["loaded_files"], [])
        self.assertEqual(result["tool_calls"]["executed_count"], 0)

    def test_tampered_reference_is_rejected_before_context_or_calls(self):
        with tempfile.TemporaryDirectory(prefix="ch15-tamper-") as temp_dir:
            copied_root = Path(temp_dir)
            shutil.copytree(HERE / "skills", copied_root / "skills")
            reference = (
                copied_root
                / "skills"
                / "enterprise-it-triage"
                / "references"
                / "vpn.md"
            )
            reference.write_text("tampered", encoding="utf-8")
            result = self.host(content_root=copied_root).run(lab.normal_request())
        self.assertEqual(result["code"], "skill_integrity_failed")
        self.assertEqual(result["context"]["loaded_files"], [])
        self.assertEqual(result["tool_calls"]["executed_count"], 0)

    def test_intermediate_skill_root_symlink_is_rejected(self):
        with tempfile.TemporaryDirectory(prefix="ch15-symlink-") as temp_dir:
            copied_root = Path(temp_dir)
            alternate = copied_root / "alternate"
            alternate.mkdir()
            shutil.copytree(
                HERE / "skills" / "enterprise-it-triage",
                alternate / "enterprise-it-triage",
            )
            (copied_root / "skills").symlink_to("alternate", target_is_directory=True)
            result = self.host(content_root=copied_root).run(lab.normal_request())
        self.assertEqual(result["code"], "skill_integrity_failed")
        self.assertIn("symlink", result["reason"])
        self.assertEqual(result["context"]["loaded_files"], [])
        self.assertEqual(result["tool_calls"]["executed_count"], 0)

    def test_skill_candidate_cannot_expand_tool_authority(self):
        result = self.host().run(
            lab.normal_request(
                proposed_tools=(
                    "tickets.list_own",
                    "ticket_drafts.create",
                    "ticket.submit",
                )
            )
        )
        self.assertEqual(result["code"], "tool_not_allowed")
        self.assertEqual(result["denied_tools"], ["ticket.submit"])
        self.assertEqual(result["context"]["loaded_files"], [])
        self.assertEqual(result["tool_calls"]["executed_count"], 0)

    def test_principal_policy_is_independent_of_skill_and_task(self):
        principal = lab.Principal(
            tenant_id="acme",
            user_id="employee-17",
            allowed_tools=frozenset({"tickets.list_own"}),
        )
        result = self.host(principal=principal).run(lab.normal_request())
        self.assertEqual(result["code"], "principal_not_authorized")
        self.assertEqual(result["denied_tools"], ["ticket_drafts.create"])
        self.assertEqual(result["tool_calls"]["executed_count"], 0)


if __name__ == "__main__":
    unittest.main()
