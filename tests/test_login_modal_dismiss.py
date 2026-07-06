from __future__ import annotations

import pytest

from studentaid_monarch_sync.portal.client import PortalCredentials, ServicerPortalClient


def _client() -> ServicerPortalClient:
    return ServicerPortalClient(
        base_url="https://example.studentaid.gov",
        creds=PortalCredentials(username="u", password="p"),
    )


# Bootstrap announcement modal (EdFinancial ~July 2026): .modal.show + .modal-backdrop that
# intercepts the "Log In" click.
BOOTSTRAP_MODAL = """
<!doctype html><html><body class="modal-open" style="overflow:hidden;padding-right:15px">
  <a id="login" href="https://myaccount.example.studentaid.gov/">Log In | Create an Account</a>
  <div class="modal fade show" style="display:block">
    <div class="modal-dialog"><div class="modal-content">
      <button type="button" class="btn-close" data-bs-dismiss="modal" aria-label="Close"></button>
      <h2 class="modal-title">Work in Public Service? Act now!!</h2>
    </div></div>
  </div>
  <div class="modal-backdrop fade show"></div>
</body></html>
"""

# Non-Bootstrap overlay (generic ARIA dialog + full-screen backdrop) to prove the dismisser is
# framework-agnostic, not keyed to Bootstrap classes.
ARIA_DIALOG_OVERLAY = """
<!doctype html><html><body>
  <a id="login" href="https://myaccount.example.studentaid.gov/">Log In</a>
  <div class="usa-modal-overlay" style="position:fixed;inset:0;z-index:1000"></div>
  <div role="dialog" aria-modal="true" style="position:fixed;inset:0;z-index:1001">
    <button aria-label="Close">x</button>
    <p>Important announcement</p>
  </div>
</body></html>
"""


@pytest.mark.parametrize(
    "html, overlay_selector, backdrop_selector",
    [
        (BOOTSTRAP_MODAL, ".modal.show", ".modal-backdrop"),
        (ARIA_DIALOG_OVERLAY, '[aria-modal="true"]', ".usa-modal-overlay"),
    ],
)
def test_dismiss_blocking_overlay(html, overlay_selector, backdrop_selector):
    try:
        from playwright.sync_api import sync_playwright
    except ImportError:
        pytest.skip("playwright not installed")

    with sync_playwright() as p:
        try:
            browser = p.chromium.launch()
        except Exception as e:  # browser binary not installed in this env
            pytest.skip(f"chromium unavailable: {e}")
        page = browser.new_page()
        page.set_content(html)

        # Precondition: overlay is up and its backdrop covers the page.
        assert page.locator(overlay_selector).count() >= 1
        assert page.locator(backdrop_selector).count() == 1

        assert _client()._dismiss_blocking_overlay(page) is True

        # Overlay + backdrop gone, and the "Log In" link is clickable (would time out if intercepted).
        assert page.locator(f"{overlay_selector}:visible").count() == 0
        assert page.locator(backdrop_selector).count() == 0
        page.click("#login", timeout=2_000)

        # Nothing left to clear -> no-op, returns False.
        assert _client()._dismiss_blocking_overlay(page) is False
        browser.close()
