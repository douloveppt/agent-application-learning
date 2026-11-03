"""Real Chromium interactions against a loopback-only fake ticket system."""
from __future__ import annotations

import argparse
import json
from pathlib import Path
from urllib.parse import urlsplit

from server import Context, Denied, State, running_site, validate_approval


SCENARIOS = (
    "success", "rerender", "ambiguous_target", "stale_approval", "expired_approval",
    "commit_then_disconnect", "unknown_no_receipt", "login_expired", "injected_page",
    "identity_mismatch",
)


def verify_receipt(receipt, preview):
    if (not isinstance(receipt, dict) or set(receipt) != {*preview, "ticket_id"}
            or not isinstance(receipt["ticket_id"], str) or not receipt["ticket_id"]
            or any(receipt[key] != value for key, value in preview.items())):
        raise Denied("receipt_does_not_match_intent")
    return receipt["ticket_id"]


def run_scenario(browser, scenario, headed=False, trace_dir=None):
    from playwright.sync_api import Error, expect

    state = State(scenario)
    trusted_context = Context("tenant-b") if scenario == "identity_mismatch" else Context()
    events = []
    result = {"scenario": scenario, "status": "failed", "ticket_id": None}
    with running_site(state) as base:
        context = browser.new_context(viewport={"width": 1100, "height": 900}, service_workers="block")
        # Demo defense-in-depth only; a Playwright route is not a production egress firewall.
        context.route("**/*", lambda route: route.continue_()
                      if urlsplit(route.request.url).netloc == urlsplit(base).netloc
                      else route.abort())
        context.add_cookies([{"name": "lab_session", "value": state.session_cookie,
                             "url": base, "httpOnly": True, "sameSite": "Strict"}])
        if trace_dir:
            context.tracing.start(screenshots=True, snapshots=True, sources=True)
        try:
            page = context.new_page()
            page.set_default_timeout(2500)
            page.goto(base)
            expect(page.get_by_label("会话状态")).to_have_text("会话有效")
            identity = page.get_by_label("当前身份").inner_text()
            if identity != trusted_context.tenant + " / " + trusted_context.actor:
                result["status"] = "identity_rejected"
                return finish(result, state, events)
            events.append("identity_verified")
            form = page.get_by_role("region", name="新建工单", exact=True)
            form.get_by_label("标题", exact=True).fill("VPN 无法连接")
            form.get_by_label("问题描述", exact=True).fill("证书已过期，请协助处理。")
            form.get_by_role("button", name="生成预览", exact=True).click()
            dialog = page.get_by_role("dialog", name="提交预览", exact=True)
            expect(dialog).to_be_visible()
            preview = json.loads(dialog.get_by_label("操作预览").inner_text())
            expected = {"tenant": trusted_context.tenant, "actor": trusted_context.actor,
                        "action": "create_own_ticket", "client_reference": state.reference,
                        "draft_version": 1, "title": "VPN 无法连接",
                        "description": "证书已过期，请协助处理。"}
            if preview != expected:
                raise Denied("preview_does_not_match_intent")
            events.append("preview_verified")
            # This deterministic fixture stands in for an already authorized user approval.
            approval = state.approve(trusted_context, preview, ttl=-1 if scenario == "expired_approval" else 60)
            if scenario == "rerender":
                old = dialog.element_handle()
                dialog.get_by_role("button", name="刷新布局").click()
                expect(dialog).to_be_visible()
                assert old is not None and not old.evaluate("node => node.isConnected")
                events.append("old_node_detached_locator_reused")
            if scenario == "ambiguous_target":
                try:
                    page.get_by_role("button", name="提交工单", exact=True).click(timeout=300)
                    raise AssertionError("ambiguous click unexpectedly succeeded")
                except Error as error:
                    if "strict mode violation" not in str(error):
                        raise
                events.append("ambiguous_target_refused_then_scoped")
            if scenario == "stale_approval":
                form.get_by_label("问题描述", exact=True).fill("新增信息：所有设备均失败。")
                form.get_by_role("button", name="生成预览", exact=True).click()
                expect(dialog.get_by_label("操作预览")).to_contain_text("新增信息")
            if scenario == "login_expired":
                state.session_active = False
            page.get_by_role("button", name="刷新身份", exact=True).click()
            if scenario == "login_expired":
                expect(page.get_by_label("会话状态")).to_have_text("登录已失效")
                result["status"] = "login_required"
                return finish(result, state, events)
            expect(page.get_by_label("会话状态")).to_have_text("会话有效")
            if page.get_by_label("当前身份").inner_text() != identity:
                raise Denied("identity_changed")
            current = json.loads(dialog.get_by_label("操作预览").inner_text())
            try:
                validate_approval(trusted_context, current, approval)
            except Denied:
                result["status"] = "approval_required"
                return finish(result, state, events)
            # The dispatch boundary must be recorded BEFORE the UI action.
            # This example only records in memory; a production runtime needs durable storage.
            events.append("dispatched")
            result["status"] = "unknown"
            dialog.get_by_role("button", name="提交工单", exact=True).click()
            expect(dialog.get_by_label("提交状态")).to_have_text(
                "提交结果未知" if scenario in {"commit_then_disconnect", "unknown_no_receipt"}
                else "已收到提交响应")
            # No second submit path exists after dispatch, even when lookup finds nothing.
            page.get_by_role("button", name="核实结果", exact=True).click()
            expect(page.get_by_label("核实状态")).to_have_text(
                "尚未找到回执" if scenario == "unknown_no_receipt" else "找到原工单")
            evidence = page.get_by_label("业务回执").inner_text()
            if evidence:
                result["ticket_id"] = verify_receipt(json.loads(evidence), current)
                result["status"] = "succeeded"
                events.append("receipt_verified")
            else:
                events.append("no_authoritative_receipt_keep_unknown")
            if headed:
                page.wait_for_timeout(500)
            return finish(result, state, events)
        finally:
            if trace_dir:
                path = Path(trace_dir)
                path.mkdir(parents=True, exist_ok=True)
                context.tracing.stop(path=str(path / (scenario + ".zip")))
            context.close()


def finish(result, state, events):
    return {**result, "create_calls": state.create_calls, "lookup_calls": state.lookup_calls,
            "tickets": int(state.receipt is not None), "wrong_clicks": state.wrong_clicks,
            "events": events}


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--scenario", choices=(*SCENARIOS, "all"), default="all")
    parser.add_argument("--headed", action="store_true")
    parser.add_argument("--trace-dir", help="Optional local trace output; contains page data")
    args = parser.parse_args()
    from playwright.sync_api import sync_playwright
    with sync_playwright() as playwright:
        browser = playwright.chromium.launch(headless=not args.headed)
        try:
            for scenario in SCENARIOS if args.scenario == "all" else (args.scenario,):
                print(json.dumps(run_scenario(browser, scenario, args.headed, args.trace_dir), ensure_ascii=False))
        finally:
            browser.close()


if __name__ == "__main__":
    main()
