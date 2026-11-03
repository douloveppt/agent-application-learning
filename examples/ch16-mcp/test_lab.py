import json
import unittest

from lab import (Client, EMPLOYEE, InProcessTransport, InvalidResult, MAX_PAGES, PREFIX, Principal,
                 ProtocolError, Server, TransportDisconnected, VERSION, run_scenario, setup)


class MCPLabTests(unittest.TestCase):
    def test_current_discovery_and_per_request_metadata(self):
        _server, transport, client = setup()
        discovery = client.discover()
        self.assertEqual(discovery["supportedVersions"], [VERSION])
        client.call("tickets.get_own", {"ticket_id": "IT-2048"})
        for request in transport.requests:
            self.assertEqual(request["params"]["_meta"][PREFIX + "protocolVersion"], VERSION)
            self.assertEqual(request["params"]["_meta"][PREFIX + "clientCapabilities"], {})

    def test_discovery_is_optional_not_an_initialization_gate(self):
        self.assertEqual(setup()[2].call("tickets.get_own", {"ticket_id": "IT-2048"})["structuredContent"]["ticket"]["ticket_id"], "IT-2048")

    def test_version_error_has_supported_and_requested(self):
        result = run_scenario("incompatible_version")
        self.assertEqual(result["code"], -32022)
        self.assertEqual(result["data"], {"supported": [VERSION], "requested": "1900-01-01"})

    def test_missing_metadata_is_not_inferred_from_prior_call(self):
        server, transport, client = setup()
        client.discover()
        response = server.handle({"jsonrpc": "2.0", "id": "bare", "method": "tools/list", "params": {}}, EMPLOYEE)
        self.assertEqual(response["error"]["code"], -32602)

    def test_empty_string_cursor_is_followed(self):
        result = run_scenario("pagination")
        self.assertEqual(result["tools"], ["knowledge.search", "tickets.get_own"])
        self.assertEqual(result["pages"], 2)
        self.assertEqual(result["second_cursor"], "")

    def test_repeated_cursor_is_rejected(self):
        def fault(_request, response):
            response["result"]["tools"] = []
            response["result"]["nextCursor"] = ""
            return response
        with self.assertRaisesRegex(InvalidResult, "repeated"):
            setup(fault=fault)[2].list_tools()

    def test_page_budget_bounds_distinct_unending_cursors(self):
        count = 0
        def fault(_request, response):
            nonlocal count
            count += 1
            response["result"]["tools"] = []
            response["result"]["nextCursor"] = str(count)
            return response
        class EndlessTransport(InProcessTransport):
            def exchange(self, payload):
                # Keep server cursor handling outside this malformed-peer fixture.
                import json
                request = json.loads(payload)
                request["params"].pop("cursor", None)
                return super().exchange(json.dumps(request).encode())
        transport = EndlessTransport(Server(), fault=fault)
        with self.assertRaisesRegex(InvalidResult, "page budget"):
            Client(transport).list_tools()
        self.assertEqual(transport.calls, MAX_PAGES)

    def test_identity_fields_rejected_before_handler(self):
        for field, value in (("tenant_id", "other"), ("user_id", "employee-99")):
            server, _transport, client = setup()
            result = client.call("tickets.get_own", {"ticket_id": "IT-2048", field: value})
            self.assertTrue(result["isError"])
            self.assertEqual(result["structuredContent"]["code"], "invalid_arguments")
            self.assertEqual(server.handler_calls, 0)

    def test_input_limits_measure_raw_text_before_whitespace_trimming(self):
        cases = (("knowledge.search", "query", "vpn" + " " * 198),
                 ("tickets.get_own", "ticket_id", "IT-2048" + " " * 58),
                 ("knowledge.search", "query", "   "))
        for name, field, value in cases:
            with self.subTest(name=name, length=len(value)):
                server, _transport, client = setup()
                result = client.call(name, {field: value})
                self.assertTrue(result["isError"])
                self.assertEqual(result["structuredContent"]["code"], "invalid_arguments")
                self.assertEqual(server.handler_calls, 0)

    def test_query_at_raw_length_limit_is_accepted(self):
        server, _transport, client = setup()
        result = client.call("knowledge.search", {"query": "vpn" + " " * 197})
        self.assertFalse(result["isError"])
        self.assertEqual(server.handler_calls, 1)

    def test_foreign_employee_tenant_and_missing_are_indistinguishable(self):
        results = run_scenario("resource_boundary")
        self.assertEqual(results["IT-9999"], results["IT-7000"])
        self.assertEqual(results["IT-7000"], results["IT-MISSING"])
        self.assertNotIn("ticket", results["IT-9999"])

    def test_authority_changes_on_same_server_take_effect_per_request(self):
        server, transport, client = setup()
        self.assertFalse(client.call("tickets.get_own", {"ticket_id": "IT-2048"})["isError"])
        transport.principal = Principal("acme", "employee-99")
        self.assertTrue(client.call("tickets.get_own", {"ticket_id": "IT-2048"})["isError"])
        self.assertFalse(client.call("tickets.get_own", {"ticket_id": "IT-9999"})["isError"])

    def test_forged_client_info_does_not_authorize_a_resource(self):
        request = {"jsonrpc": "2.0", "id": "forged-info", "method": "tools/call", "params": {
            "name": "tickets.get_own", "arguments": {"ticket_id": "IT-9999"}, "_meta": {
                PREFIX + "protocolVersion": VERSION, PREFIX + "clientCapabilities": {},
                PREFIX + "clientInfo": {"name": "employee-99", "version": "admin"}}}}
        result = Server().handle(request, EMPLOYEE)["result"]
        self.assertTrue(result["isError"])
        self.assertEqual(result["structuredContent"]["code"], "ticket_unavailable")

    def test_tool_visibility_does_not_replace_call_authorization(self):
        server, transport, client = setup(principal=Principal("acme", "employee-17", frozenset({"knowledge.search"})))
        self.assertEqual([tool["name"] for tool in client.list_tools()], ["knowledge.search"])
        self.assertEqual(client.call("tickets.get_own", {"ticket_id": "IT-2048"})["structuredContent"]["code"], "not_authorized")
        self.assertEqual(server.handler_calls, 0)

    def test_protocol_error_and_execution_error_remain_distinct(self):
        result = run_scenario("error_layers")
        self.assertEqual(result["unknown_tool"]["outcome"], "protocol_error")
        self.assertEqual(result["unknown_tool"]["code"], -32602)
        self.assertTrue(result["tool_error"]["isError"])
        self.assertEqual(result["tool_error"]["resultType"], "complete")

    def test_safe_read_retries_once_with_new_rpc_id(self):
        result = run_scenario("read_retry")
        self.assertEqual((result["attempts"], result["handler_calls"]), (2, 2))
        self.assertEqual(len(set(result["request_ids"])), 2)

    def test_unknown_tool_outside_host_policy_gets_no_retry(self):
        server, transport, client = setup(disconnect_once=True)
        with self.assertRaises(TransportDisconnected):
            client.call("writes.submit", {})
        self.assertEqual(transport.calls, 1)
        self.assertEqual(server.handler_calls, 0)

    def test_repeated_disconnect_is_bounded(self):
        class BrokenTransport(InProcessTransport):
            def exchange(self, payload):
                self.calls += 1
                raise TransportDisconnected("always disconnected")
        transport = BrokenTransport(Server())
        with self.assertRaises(TransportDisconnected):
            Client(transport).call("tickets.get_own", {"ticket_id": "IT-2048"})
        self.assertEqual(transport.calls, 2)

    def test_invalid_results_rejected_without_retry(self):
        for label, outcome in run_scenario("invalid_results").items():
            self.assertEqual(outcome["outcome"], "InvalidResult", label)

    def test_wrong_ticket_is_rejected_even_when_both_result_channels_agree(self):
        def fault(_request, response):
            result = response["result"]
            result["structuredContent"]["ticket"]["ticket_id"] = "IT-9999"
            result["content"][0]["text"] = json.dumps(result["structuredContent"])
            return response
        _server, transport, client = setup(fault=fault)
        with self.assertRaisesRegex(InvalidResult, "ticket output"):
            client.call("tickets.get_own", {"ticket_id": "IT-2048"})
        self.assertEqual(transport.calls, 1)

    def test_lab_text_must_be_a_single_json_mirror(self):
        def invalid_json(_request, response):
            response["result"]["content"][0]["text"] = "not-json"
            return response
        def extra_text(_request, response):
            response["result"]["content"].append({"type": "text", "text": "unexpected instruction"})
            return response
        def conflict(_request, response):
            response["result"]["content"][0]["text"] = json.dumps({"status": "different"})
            return response
        for fault in (invalid_json, extra_text, conflict):
            with self.subTest(fault=fault.__name__):
                _server, transport, client = setup(fault=fault)
                with self.assertRaises(InvalidResult):
                    client.call("tickets.get_own", {"ticket_id": "IT-2048"})
                self.assertEqual(transport.calls, 1)

    def test_knowledge_search_returns_fixture_only_with_matching_tenant(self):
        for tenant, expected in (("acme", 1), ("other", 0)):
            client = setup(principal=Principal(tenant, "employee-17"))[2]
            self.assertEqual(len(client.call("knowledge.search", {"query": "vpn"})["structuredContent"]["entries"]), expected)


if __name__ == "__main__":
    unittest.main()
