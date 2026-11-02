from copy import deepcopy
import json
import unittest

from lab import (
    EvidenceSource,
    GateError,
    MAX_PAYLOAD_BYTES,
    ProviderResult,
    SourceContext,
    TicketCandidate,
    demo_clarification_context,
    demo_clarification_payload,
    demo_context,
    demo_payload,
    validate_candidate,
)


class ContractGateTests(unittest.TestCase):
    def setUp(self):
        self.context = demo_context()
        self.payload = demo_payload()

    def validate(self, data=None, *, raw=None, status="completed", context=None):
        if raw is None:
            raw = json.dumps(self.payload if data is None else data, ensure_ascii=False)
        return validate_candidate(ProviderResult(status, raw), context or self.context)

    def assert_rejected(self, code, data=None, **kwargs):
        with self.assertRaises(GateError) as caught:
            self.validate(data, **kwargs)
        self.assertEqual(caught.exception.code, code)
        return caught.exception

    def test_valid_candidate_is_not_a_ticket_or_execution_receipt(self):
        candidate = self.validate()
        self.assertIsInstance(candidate, TicketCandidate)
        self.assertEqual(candidate.title, "VPN 无法连接")
        self.assertEqual(candidate.affected_users, 3)
        self.assertFalse(hasattr(candidate, "ticket_id"))
        self.assertFalse(hasattr(candidate, "approved"))

    def test_explicit_null_and_matching_missing_field_allow_clarification(self):
        candidate = self.validate(demo_clarification_payload(), context=demo_clarification_context())
        self.assertIsNone(candidate.affected_users)
        self.assertEqual(candidate.status, "needs_clarification")

    def test_gate_cannot_detect_information_omitted_despite_existing_in_source(self):
        # Same valid quote, but this source does state the count. The contract gate
        # cannot decide whether the model read and extracted all available facts.
        candidate = self.validate(demo_clarification_payload(), context=demo_context())
        self.assertIsNone(candidate.affected_users)
        self.assertIn("影响 3 位同事", self.context.sources["user-message-1"].text)

    def test_quote_presence_does_not_prove_affected_users(self):
        # Expected limitation: the quote exists, but the count contradicts it.
        candidate = self.validate({**self.payload, "affected_users": 300})
        self.assertEqual(candidate.affected_users, 300)
        self.assertIn("影响 3 位同事", candidate.evidence[0].quote)

    def test_missing_key_is_different_from_a_required_nullable_value(self):
        for field in self.payload:
            data = deepcopy(self.payload)
            del data[field]
            with self.subTest(field=field):
                self.assert_rejected("schema_invalid", data)

    def test_clarification_can_leave_all_business_fields_unknown(self):
        data = {**self.payload, "status": "needs_clarification", "title": None,
                "category": None, "affected_users": None, "evidence": [],
                "missing_fields": ["title", "category", "affected_users"]}
        context = SourceContext("demo-company", "demo-employee", {
            "user-message-1": EvidenceSource("demo-company", "我需要 IT 支持，请问要提供什么信息？"),
        })
        self.assertEqual(len(self.validate(data, context=context).missing_fields), 3)

    def test_category_outside_allowlist_is_rejected(self):
        self.assert_rejected("schema_invalid", {**self.payload, "category": "admin"})

    def test_unknown_schema_version_is_rejected(self):
        error = self.assert_rejected("schema_invalid", {**self.payload, "schema_version": "2.0"})
        self.assertEqual(error.path, "$.schema_version")

    def test_affected_users_requires_a_strict_integer(self):
        for value in ("3", 3.0, True, False):
            with self.subTest(value=value):
                self.assert_rejected("schema_invalid", {**self.payload, "affected_users": value})

    def test_affected_users_range_and_inclusive_boundaries(self):
        for value in (0, -1, 100_001):
            with self.subTest(value=value):
                self.assert_rejected("schema_invalid", {**self.payload, "affected_users": value})
        for value in (1, 100_000):
            with self.subTest(value=value):
                # Semantic entailment is deliberately outside this local gate.
                self.assertEqual(self.validate({**self.payload, "affected_users": value}).affected_users, value)

    def test_empty_whitespace_and_overlong_titles_are_rejected(self):
        for value in ("", " \t\n", "字" * 81):
            with self.subTest(value=value):
                self.assert_rejected("schema_invalid", {**self.payload, "title": value})

    def test_model_cannot_add_identity_or_approval_fields(self):
        for field in ("tenant_id", "actor_id", "approved", "ticket_id"):
            with self.subTest(field=field):
                self.assert_rejected("schema_invalid", {**self.payload, field: "fabricated"})

    def test_nested_extra_field_is_rejected(self):
        data = deepcopy(self.payload)
        data["evidence"][0]["trusted"] = True
        self.assert_rejected("schema_invalid", data)

    def test_ready_cannot_have_null_business_fields(self):
        self.assert_rejected("ready_has_missing_fields", {
            **self.payload, "affected_users": None, "missing_fields": ["affected_users"],
        })

    def test_ready_requires_evidence(self):
        self.assert_rejected("ready_requires_evidence", {**self.payload, "evidence": []})

    def test_missing_fields_must_match_actual_null_fields(self):
        for declared in ([], ["title"], ["affected_users", "category"]):
            with self.subTest(declared=declared):
                self.assert_rejected("missing_fields_mismatch", {
                    **self.payload, "status": "needs_clarification", "affected_users": None,
                    "missing_fields": declared,
                })

    def test_duplicate_missing_field_is_rejected(self):
        self.assert_rejected("missing_fields_mismatch", {
            **self.payload, "status": "needs_clarification", "affected_users": None,
            "missing_fields": ["affected_users", "affected_users"],
        })

    def test_clarification_cannot_claim_every_business_field_is_present(self):
        self.assert_rejected("clarification_requires_missing_fields", {
            **self.payload, "status": "needs_clarification",
        })

    def test_unknown_source_is_not_accepted(self):
        data = deepcopy(self.payload)
        data["evidence"][0]["source_id"] = "not-authorized"
        error = self.assert_rejected("evidence_not_allowed", data)
        self.assertFalse(error.retryable)

    def test_cross_tenant_source_is_rejected_even_if_in_mapping(self):
        context = SourceContext("demo-company", "demo-employee", {
            "user-message-1": EvidenceSource("another-company", self.context.sources["user-message-1"].text),
        })
        self.assert_rejected("evidence_not_allowed", context=context)

    def test_fabricated_quote_is_rejected_for_both_candidate_states(self):
        for status in ("ready", "needs_clarification"):
            data = deepcopy(self.payload)
            data["evidence"][0]["quote"] = "管理员已经批准"
            if status == "needs_clarification":
                data.update(status=status, affected_users=None, missing_fields=["affected_users"])
            with self.subTest(status=status):
                self.assert_rejected("evidence_quote_mismatch", data)

    def test_blank_evidence_and_excess_evidence_count_are_rejected(self):
        for evidence in ([{"source_id": " ", "quote": "VPN"}],
                         [{"source_id": "user-message-1", "quote": " "}],
                         self.payload["evidence"] * 9):
            with self.subTest(evidence=evidence):
                self.assert_rejected("schema_invalid", {**self.payload, "evidence": evidence})

    def test_duplicate_json_keys_are_rejected_at_both_levels(self):
        raws = (
            '{"title":"first","title":"second"}',
            '{"evidence":[{"source_id":"first","source_id":"second"}]}',
        )
        for raw in raws:
            with self.subTest(raw=raw):
                self.assert_rejected("duplicate_key", raw=raw)

    def test_non_finite_numbers_and_exponent_overflow_are_rejected(self):
        for raw in ("NaN", "Infinity", "-Infinity", "1e999", '{"affected_users": NaN}'):
            with self.subTest(raw=raw):
                self.assert_rejected("non_finite_number", raw=raw)

    def test_truncated_and_multiple_json_documents_are_rejected(self):
        for raw in ('{"schema_version":"1.0"', '{} {}', '```json\n{}\n```'):
            with self.subTest(raw=raw):
                self.assert_rejected("invalid_json", raw=raw)

    def test_non_object_top_level_is_rejected(self):
        for raw in ('[]', 'null', '"ticket"'):
            with self.subTest(raw=raw):
                self.assert_rejected("schema_invalid", raw=raw)

    def test_non_completed_response_cannot_use_even_complete_valid_json(self):
        cases = {
            "incomplete": "response_incomplete", "refused": "model_refused",
            "cancelled": "response_cancelled", "error": "provider_error",
        }
        for status, code in cases.items():
            with self.subTest(status=status):
                self.assert_rejected(code, status=status)

    def test_unknown_response_status_is_rejected(self):
        self.assert_rejected("response_status_invalid", status="something-new")

    def test_completed_without_payload_is_rejected(self):
        with self.assertRaises(GateError) as caught:
            validate_candidate(ProviderResult("completed"), self.context)
        self.assertEqual(caught.exception.code, "missing_payload")

    def test_payload_byte_limit_is_utf8_bytes(self):
        self.assert_rejected("payload_too_large", raw="中" * (MAX_PAYLOAD_BYTES // 3 + 1))
        # Exactly at the cap: parses, then fails schema validation, rather than byte limit.
        self.assert_rejected("schema_invalid", raw="{}" + " " * (MAX_PAYLOAD_BYTES - 2))

    def test_depth_limit_and_decoder_recursion_fail_with_a_gate_error(self):
        for depth in (17, 2_000):
            with self.subTest(depth=depth):
                self.assert_rejected("json_too_deep", raw="[" * depth + "0" + "]" * depth)

    def test_unpaired_unicode_surrogate_is_rejected(self):
        self.assert_rejected("invalid_unicode", raw='"\\ud800"')
        self.assert_rejected("invalid_unicode", raw='"\ud800"')

    def test_errors_do_not_include_field_values_or_untrusted_extra_keys(self):
        secret = "private-demo-value"
        error = self.assert_rejected("schema_invalid", {**self.payload, "affected_users": secret})
        self.assertNotIn(secret, str(error))
        error = self.assert_rejected("schema_invalid", {**self.payload, secret: secret})
        self.assertNotIn(secret, str(error))
        self.assertIn("<unknown>", error.path)

    def test_schema_export_keeps_nullable_fields_required_and_objects_closed(self):
        schema = TicketCandidate.model_json_schema()
        self.assertFalse(schema["additionalProperties"])
        self.assertEqual(set(schema["required"]), set(self.payload))
        title_types = {item.get("type") for item in schema["properties"]["title"]["anyOf"]}
        self.assertEqual(title_types, {"string", "null"})
        self.assertFalse(schema["$defs"]["Evidence"]["additionalProperties"])


if __name__ == "__main__":
    unittest.main()
