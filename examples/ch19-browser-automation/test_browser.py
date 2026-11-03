"""Real Chromium acceptance. Missing browser/runtime is a failure, never a skip."""
import unittest

import lab


class BrowserTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        from playwright.sync_api import sync_playwright
        cls.playwright = sync_playwright().start()
        try:
            cls.browser = cls.playwright.chromium.launch()
        except Exception:
            cls.playwright.stop()
            raise

    @classmethod
    def tearDownClass(cls):
        cls.browser.close()
        cls.playwright.stop()

    def check(self, scenario, status, creates=1, tickets=1, event=None):
        result = lab.run_scenario(self.browser, scenario)
        self.assertEqual(result["status"], status, result)
        self.assertEqual(result["create_calls"], creates, result)
        self.assertEqual(result["tickets"], tickets, result)
        self.assertEqual(result["wrong_clicks"], 0, result)
        if event:
            self.assertIn(event, result["events"])
        return result

    def test_success_is_verified_by_readback(self):
        result = self.check("success", "succeeded", event="receipt_verified")
        self.assertEqual(result["lookup_calls"], 1)

    def test_detached_node_does_not_break_semantic_locator(self):
        self.check("rerender", "succeeded", event="old_node_detached_locator_reused")

    def test_ambiguous_button_is_refused_then_scoped(self):
        self.check("ambiguous_target", "succeeded", event="ambiguous_target_refused_then_scoped")

    def test_changed_preview_requires_new_approval(self):
        self.check("stale_approval", "approval_required", creates=0, tickets=0)

    def test_expired_approval_prevents_submit(self):
        self.check("expired_approval", "approval_required", creates=0, tickets=0)

    def test_lost_response_is_reconciled_without_second_submit(self):
        result = self.check("commit_then_disconnect", "succeeded")
        self.assertEqual(result["lookup_calls"], 1)

    def test_empty_lookup_remains_unknown_and_does_not_replay(self):
        result = self.check("unknown_no_receipt", "unknown", tickets=0,
                            event="no_authoritative_receipt_keep_unknown")
        self.assertEqual(result["lookup_calls"], 1)

    def test_expired_login_stops_before_dispatch(self):
        self.check("login_expired", "login_required", creates=0, tickets=0)

    def test_untrusted_page_text_does_not_change_fixed_host_intent(self):
        self.check("injected_page", "succeeded")

    def test_wrong_account_stops_before_draft_creation(self):
        self.check("identity_mismatch", "identity_rejected", creates=0, tickets=0)


if __name__ == "__main__":
    unittest.main()
