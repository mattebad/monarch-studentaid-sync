from __future__ import annotations

import logging
import json
import re
import shutil
import time
from dataclasses import dataclass
from datetime import date, datetime, timezone
from pathlib import Path
from typing import Callable, Optional
from urllib.parse import urlparse

from playwright.sync_api import Frame, Page, sync_playwright

from ..models import LoanSnapshot, PaymentAllocation
from ..util.dates import parse_us_date
from ..util.money import money_to_cents
from ..util.debug_bundle import create_debug_bundle
from .selectors import PortalSelectors


logger = logging.getLogger(__name__)


class LoginFormNotFoundError(RuntimeError):
    """
    Raised when we cannot locate the portal login form (username field) after trying common entry points.
    """


@dataclass(frozen=True)
class PortalCredentials:
    username: str
    password: str


class ServicerPortalClient:
    """
    StudentAid servicer portal automation (typically `https://{provider}.studentaid.gov`).
    """

    def __init__(
        self,
        *,
        base_url: str,
        creds: PortalCredentials,
        selectors: Optional[PortalSelectors] = None,
    ) -> None:
        self.base_url = base_url.rstrip("/")
        self.creds = creds
        self.selectors = selectors or PortalSelectors()

        parsed = urlparse(self.base_url)
        self._canonical_host = (parsed.netloc or "").strip().lower()
        self._dark_host = f"dark.{self._canonical_host}" if self._canonical_host else ""

        # Step-by-step debug (screenshots) — configured per `extract()` call.
        self._step_debug_enabled: bool = False
        self._step_counter: int = 0
        self._step_delay_ms: int = 0

    def extract(
        self,
        *,
        groups: list[str],
        headless: bool = True,
        storage_state_path: str = "data/servicer_storage_state.json",
        debug_dir: str = "data/debug",
        max_payments_to_scan: int = 10,
        payments_since: Optional[date] = None,
        mfa_code_provider: Optional[Callable[[], str]] = None,
        mfa_method: str = "email",
        force_fresh_session: bool = False,
        slow_mo_ms: int = 0,
        step_debug: bool = False,
        step_delay_ms: int = 0,
        manual_mfa: bool = False,
    ) -> tuple[list[LoanSnapshot], list[PaymentAllocation]]:
        # Configure per-run step debug behavior.
        self._step_debug_enabled = step_debug
        self._step_counter = 0
        self._step_delay_ms = int(step_delay_ms or 0)

        state_path = Path(storage_state_path) if storage_state_path else None
        Path(debug_dir).mkdir(parents=True, exist_ok=True)

        def _install_context_hooks(ctx) -> None:
            # Rewrite the occasionally-seen "dark" host back to the canonical host.
            # We have observed headful sessions sometimes ending up on:
            #   https://dark.<servicer>.studentaid.gov/...
            # which frequently fails DNS resolution on some machines (NXDOMAIN).
            try:
                if not self._dark_host or not self._canonical_host:
                    raise RuntimeError("canonical host missing")

                def _rewrite(route, request) -> None:
                    url = request.url
                    fixed = url.replace(f"://{self._dark_host}", f"://{self._canonical_host}")
                    route.continue_(url=fixed)

                ctx.route(
                    re.compile(rf".*://{re.escape(self._dark_host)}/.*", re.I),
                    _rewrite,
                )
            except Exception:
                logger.debug("Failed to install dark-host rewrite route.", exc_info=True)

            # The portal uses a Transcend consent manager ("This site uses cookies") that can render
            # inside a shadow root and intercept clicks (including the federal disclaimer).
            # Install an init script so the consent UI is dismissed/hidden as soon as it mounts.
            ctx.add_init_script(
                """
                (() => {
                  const dismiss = () => {
                    // If consent UI is in light DOM, try clicking "Accept all"
                    try {
                      const accept = Array.from(document.querySelectorAll('button'))
                        .find(b => /accept\\s+all/i.test((b.textContent || '').trim()));
                      if (accept && /this\\s+site\\s+uses\\s+cookies/i.test(document.body?.innerText || '')) {
                        accept.click();
                      }
                    } catch (_) {}

                    // If consent UI is in an OPEN shadow root, try clicking "Accept all"
                    try {
                      const host = document.getElementById('transcend-consent-manager');
                      const root = host && host.shadowRoot;
                      if (root) {
                        const accept = Array.from(root.querySelectorAll('button'))
                          .find(b => /accept\\s+all/i.test((b.textContent || '').trim()));
                        if (accept) accept.click();
                      }
                    } catch (_) {}

                    // Always hide the host so it cannot intercept clicks (works even if shadow root is CLOSED)
                    try {
                      const host = document.getElementById('transcend-consent-manager');
                      if (host) {
                        host.style.setProperty('display', 'none', 'important');
                        host.style.setProperty('pointer-events', 'none', 'important');
                      }
                    } catch (_) {}
                  };

                  dismiss();
                  new MutationObserver(() => dismiss()).observe(document.documentElement, { childList: true, subtree: true });
                })();
                """
            )

        with sync_playwright() as p:
            browser = p.chromium.launch(headless=headless, slow_mo=int(slow_mo_ms or 0))
            try:
                # Attempt 1: reuse stored session (unless force_fresh_session).
                # Attempt 2: fresh session (no stored cookies) — helpful when stored state causes
                # weird redirects (e.g. `dark.<provider>.studentaid.gov`) or other edge cases.
                attempts = 1 if force_fresh_session else 2

                for attempt_idx in range(attempts):
                    use_storage = (
                        attempt_idx == 0
                        and not force_fresh_session
                        and state_path is not None
                        and state_path.exists()
                    )

                    # Self-heal: if the persisted Playwright storage_state JSON is corrupted, quarantine it and
                    # fall back to a fresh session (or restore from .bak if available).
                    if use_storage and state_path is not None:
                        use_storage = self._validate_or_restore_storage_state(state_path)

                    ctx_kwargs: dict = {}
                    if use_storage:
                        ctx_kwargs["storage_state"] = str(state_path)

                    # Force light color scheme.
                    ctx_kwargs["color_scheme"] = "light"

                    ctx = None
                    try:
                        ctx = browser.new_context(**ctx_kwargs)
                    except Exception as e:
                        # If storage_state is invalid/corrupt, Playwright can fail before we ever get a Page.
                        if use_storage and state_path is not None:
                            logger.warning(
                                "Failed to create browser context with stored session; falling back to fresh session. (%s)",
                                e,
                            )
                            self._quarantine_file(state_path, prefix="storage_state")
                            ctx_kwargs.pop("storage_state", None)
                            use_storage = False
                            ctx = browser.new_context(**ctx_kwargs)
                        else:
                            raise

                    _install_context_hooks(ctx)

                    page = ctx.new_page()
                    try:
                        self._step(page, debug_dir=debug_dir, name=f"start_attempt_{attempt_idx+1}")
                        self._login(
                            page,
                            mfa_code_provider=mfa_code_provider,
                            mfa_method=mfa_method,
                            debug_dir=debug_dir,
                            manual_mfa=manual_mfa,
                        )

                        # Persist session state to reduce MFA prompts (best-effort).
                        if state_path is not None:
                            state_path.parent.mkdir(parents=True, exist_ok=True)
                            ctx.storage_state(path=str(state_path))
                            self._backup_storage_state(state_path)

                        loans = self._extract_loans(page, groups=groups, debug_dir=debug_dir)
                        payments = self._extract_payment_allocations(
                            page,
                            debug_dir=debug_dir,
                            max_payments_to_scan=max_payments_to_scan,
                            payments_since=payments_since,
                        )
                        return loans, payments
                    except Exception as e:
                        # If the first attempt fails, retry once with a fresh session.
                        #
                        # We always do this for browser/DNS error pages, and we also do it for the common
                        # "login form not found" scenario which can happen when a persisted storage_state
                        # lands us on an unexpected intermediate page.
                        if attempt_idx == 0 and not force_fresh_session:
                            retry_for_browser_error = self._looks_like_browser_error(page)
                            retry_for_login_form = (
                                use_storage
                                and isinstance(e, LoginFormNotFoundError)
                                and not self._looks_logged_in(page)
                            )
                            if retry_for_browser_error or retry_for_login_form:
                                logger.warning(
                                    "Portal navigation/login failed%s; retrying once with a fresh session (no stored cookies).",
                                    " (stored session)" if use_storage else "",
                                )
                                self._save_debug(page, debug_dir=debug_dir, name_prefix="retry_fresh_session")
                                continue
                        raise
                    finally:
                        ctx.close()
            finally:
                browser.close()

    def discover_loan_groups(
        self,
        *,
        headless: bool = True,
        storage_state_path: str = "data/servicer_storage_state.json",
        debug_dir: str = "data/debug",
        mfa_code_provider: Optional[Callable[[], str]] = None,
        mfa_method: str = "email",
        force_fresh_session: bool = False,
        slow_mo_ms: int = 0,
        step_debug: bool = False,
        step_delay_ms: int = 0,
        manual_mfa: bool = False,
    ) -> list[tuple[str, str]]:
        """
        Log into the servicer portal and return discovered loan groups.

        Returns: list of (group_token, group_label)
        - group_token: short ID suitable for LOAN_GROUPS (e.g. "AA", "1-01") when parseable
        - group_label: raw label after "Group:" (may include extra words)
        """
        # Configure per-run step debug behavior.
        self._step_debug_enabled = step_debug
        self._step_counter = 0
        self._step_delay_ms = int(step_delay_ms or 0)

        state_path = Path(storage_state_path) if storage_state_path else None
        Path(debug_dir).mkdir(parents=True, exist_ok=True)

        def _install_context_hooks(ctx) -> None:
            # Keep behavior consistent with `extract()`.
            try:
                if not self._dark_host or not self._canonical_host:
                    raise RuntimeError("canonical host missing")

                def _rewrite(route, request) -> None:
                    url = request.url
                    fixed = url.replace(f"://{self._dark_host}", f"://{self._canonical_host}")
                    route.continue_(url=fixed)

                ctx.route(
                    re.compile(rf".*://{re.escape(self._dark_host)}/.*", re.I),
                    _rewrite,
                )
            except Exception:
                logger.debug("Failed to install dark-host rewrite route.", exc_info=True)

            ctx.add_init_script(
                """
                (() => {
                  const dismiss = () => {
                    try {
                      const accept = Array.from(document.querySelectorAll('button'))
                        .find(b => /accept\\s+all/i.test((b.textContent || '').trim()));
                      if (accept && /this\\s+site\\s+uses\\s+cookies/i.test(document.body?.innerText || '')) {
                        accept.click();
                      }
                    } catch (_) {}

                    try {
                      const host = document.getElementById('transcend-consent-manager');
                      const root = host && host.shadowRoot;
                      if (root) {
                        const accept = Array.from(root.querySelectorAll('button'))
                          .find(b => /accept\\s+all/i.test((b.textContent || '').trim()));
                        if (accept) accept.click();
                      }
                    } catch (_) {}

                    try {
                      const host = document.getElementById('transcend-consent-manager');
                      if (host) {
                        host.style.setProperty('display', 'none', 'important');
                        host.style.setProperty('pointer-events', 'none', 'important');
                      }
                    } catch (_) {}
                  };

                  try {
                    const observer = new MutationObserver(() => dismiss());
                    observer.observe(document.documentElement, { childList: true, subtree: true });
                    dismiss();
                  } catch (_) {}
                })();
                """
            )

        with sync_playwright() as p:
            browser = p.chromium.launch(headless=headless, slow_mo=slow_mo_ms)
            try:
                for attempt_idx in range(2):
                    use_storage = (
                        attempt_idx == 0 and not force_fresh_session and state_path is not None and state_path.exists()
                    )
                    if use_storage and state_path is not None:
                        use_storage = self._validate_or_restore_storage_state(state_path)

                    ctx_kwargs: dict = {}
                    if use_storage:
                        ctx_kwargs["storage_state"] = str(state_path)
                    ctx_kwargs["color_scheme"] = "light"

                    ctx = None
                    try:
                        ctx = browser.new_context(**ctx_kwargs)
                    except Exception as e:
                        if use_storage and state_path is not None:
                            logger.warning(
                                "Failed to create browser context with stored session; falling back to fresh session. (%s)",
                                e,
                            )
                            self._quarantine_file(state_path, prefix="storage_state")
                            ctx_kwargs.pop("storage_state", None)
                            use_storage = False
                            ctx = browser.new_context(**ctx_kwargs)
                        else:
                            raise

                    _install_context_hooks(ctx)

                    page = ctx.new_page()
                    try:
                        self._step(page, debug_dir=debug_dir, name=f"discover_start_attempt_{attempt_idx+1}")
                        self._login(
                            page,
                            mfa_code_provider=mfa_code_provider,
                            mfa_method=mfa_method,
                            debug_dir=debug_dir,
                            manual_mfa=manual_mfa,
                        )

                        if state_path is not None:
                            state_path.parent.mkdir(parents=True, exist_ok=True)
                            ctx.storage_state(path=str(state_path))
                            self._backup_storage_state(state_path)

                        # Navigate to loan details and parse "Group:" headers.
                        self._wait_for_post_login_ready(page, debug_dir=debug_dir, timeout_ms=90_000)
                        self._goto_section(page, self.selectors.nav_my_loans_text, debug_dir=debug_dir)

                        # Some portals render multiple "My Loans" targets (nav, dashboard cards, footer).
                        # We try to click the most likely navigation candidate first, but still keep a hard
                        # fallback: if clicks don't land us on the loan details view, go directly by URL.
                        if not self._wait_for_body_text_contains(page, "Group:", timeout_ms=15_000):
                            try:
                                page.goto(f"{self.base_url}/loan-details", wait_until="domcontentloaded")
                                self._wait_for_settle(page, timeout_ms=30_000)
                            except Exception:
                                # We'll validate below; if still not loaded, we'll raise with debug artifacts.
                                pass

                        if not self._wait_for_body_text_contains(page, "Group:", timeout_ms=30_000):
                            self._save_debug(page, debug_dir=debug_dir, name_prefix="discover_groups_not_loaded")
                            raise RuntimeError(
                                f"Loan details page did not load (missing 'Group:' sections). url={getattr(page, 'url', '')!r}"
                            )

                        body = page.inner_text("body")
                        sections = self._extract_all_group_sections(body)
                        groups: list[tuple[str, str]] = []
                        seen: set[str] = set()
                        for token, label, _ in sections:
                            key = token or label
                            if not key or key in seen:
                                continue
                            groups.append((token, label))
                            seen.add(key)
                        return groups
                    except Exception as e:
                        if attempt_idx == 0 and not force_fresh_session:
                            retry_for_browser_error = self._looks_like_browser_error(page)
                            retry_for_login_form = (
                                use_storage and isinstance(e, LoginFormNotFoundError) and not self._looks_logged_in(page)
                            )
                            if retry_for_browser_error or retry_for_login_form:
                                logger.warning(
                                    "Portal navigation/login failed%s; retrying once with a fresh session (no stored cookies).",
                                    " (stored session)" if use_storage else "",
                                )
                                self._save_debug(page, debug_dir=debug_dir, name_prefix="discover_retry_fresh_session")
                                continue
                        raise
                    finally:
                        ctx.close()
            finally:
                browser.close()

    def browse_and_capture(
        self,
        *,
        debug_dir: str,
        log_file: str,
        out_dir: str = "data",
        headless: bool = False,
        storage_state_path: str = "data/servicer_storage_state.json",
        mfa_code_provider: Optional[Callable[[], str]] = None,
        mfa_method: str = "email",
        force_fresh_session: bool = False,
        slow_mo_ms: int = 0,
        manual_mfa: bool = False,
        no_login: bool = False,
    ) -> Path:
        """
        Open a browser for manual portal exploration while capturing HTML+screenshots on navigation.

        - If no_login=False, we attempt to authenticate first using the normal automation (and optional manual MFA).
        - Captures are written to debug_dir (auto-generated if empty), and then zipped into a debug bundle when the
          browser is closed.
        """
        provider = (self._canonical_host.split(".", 1)[0] if self._canonical_host else "").strip().lower()
        stamp = time.strftime("%Y%m%d_%H%M%S")
        cap_dir = Path(debug_dir) if debug_dir else Path(out_dir) / f"browse_capture_{provider or 'servicer'}_{stamp}"
        cap_dir.mkdir(parents=True, exist_ok=True)

        state_path = Path(storage_state_path) if storage_state_path else None

        def _sanitize(s: str) -> str:
            return re.sub(r"[^a-zA-Z0-9_-]+", "_", (s or "")).strip("_")[:80] or "page"

        capture_counter = {"n": 0}
        last_url_by_page: dict[int, str] = {}

        def _capture(page: Page, *, reason: str) -> None:
            try:
                url = getattr(page, "url", "") or ""
            except Exception:
                url = ""

            pid = id(page)
            prev = last_url_by_page.get(pid, "")
            if url and prev == url and reason != "manual":
                return
            last_url_by_page[pid] = url

            capture_counter["n"] += 1
            n = capture_counter["n"]
            name = _sanitize(url.split("?", 1)[0].split("#", 1)[0]) if url else "unknown"
            prefix = f"cap_{n:03d}_{_sanitize(reason)}_{name}"

            try:
                page.screenshot(path=str(cap_dir / f"{prefix}.png"), full_page=True)
            except Exception:
                pass
            try:
                (cap_dir / f"{prefix}.html").write_text(page.content(), encoding="utf-8")
            except Exception:
                pass
            try:
                (cap_dir / f"{prefix}.txt").write_text(page.inner_text("body"), encoding="utf-8")
            except Exception:
                pass

        def _install_context_hooks(ctx) -> None:
            # Same stability hooks as extract()/discover.
            try:
                if self._dark_host and self._canonical_host:
                    def _rewrite(route, request) -> None:
                        url = request.url
                        fixed = url.replace(f"://{self._dark_host}", f"://{self._canonical_host}")
                        route.continue_(url=fixed)

                    ctx.route(re.compile(rf".*://{re.escape(self._dark_host)}/.*", re.I), _rewrite)
            except Exception:
                logger.debug("Failed to install dark-host rewrite route.", exc_info=True)

            ctx.add_init_script(
                """
                (() => {
                  const dismiss = () => {
                    try {
                      const accept = Array.from(document.querySelectorAll('button'))
                        .find(b => /accept\\s+all/i.test((b.textContent || '').trim()));
                      if (accept && /this\\s+site\\s+uses\\s+cookies/i.test(document.body?.innerText || '')) {
                        accept.click();
                      }
                    } catch (_) {}
                    try {
                      const host = document.getElementById('transcend-consent-manager');
                      if (host) {
                        host.style.setProperty('display', 'none', 'important');
                        host.style.setProperty('pointer-events', 'none', 'important');
                      }
                    } catch (_) {}
                  };
                  try {
                    const observer = new MutationObserver(() => dismiss());
                    observer.observe(document.documentElement, { childList: true, subtree: true });
                    dismiss();
                  } catch (_) {}
                })();
                """
            )

        with sync_playwright() as p:
            browser = p.chromium.launch(headless=headless, slow_mo=int(slow_mo_ms or 0))
            try:
                ctx_kwargs: dict = {"color_scheme": "light"}
                if state_path and state_path.exists() and not force_fresh_session:
                    if self._validate_or_restore_storage_state(state_path):
                        ctx_kwargs["storage_state"] = str(state_path)

                ctx = browser.new_context(**ctx_kwargs)
                try:
                    _install_context_hooks(ctx)

                    page = ctx.new_page()

                    # Capture on URL changes (SPA navigations) + initial load.
                    def _on_nav(frame) -> None:
                        try:
                            if frame == page.main_frame:
                                _capture(page, reason="navigate")
                        except Exception:
                            pass

                    page.on("framenavigated", _on_nav)

                    page.goto(self.base_url, wait_until="domcontentloaded")
                    self._wait_for_settle(page)
                    _capture(page, reason="start")

                    if not no_login:
                        self._login(
                            page,
                            mfa_code_provider=mfa_code_provider,
                            mfa_method=mfa_method,
                            debug_dir=str(cap_dir),
                            manual_mfa=manual_mfa,
                        )
                        if state_path is not None:
                            state_path.parent.mkdir(parents=True, exist_ok=True)
                            ctx.storage_state(path=str(state_path))
                            self._backup_storage_state(state_path)
                        _capture(page, reason="after_login")

                    print(
                        "Browser is open for manual navigation. Close the browser window when finished; "
                        "a debug bundle zip will be created."
                    )

                    # Wait until the page is closed by the user.
                    try:
                        page.wait_for_event("close")
                    except Exception:
                        pass
                finally:
                    try:
                        ctx.close()
                    except Exception:
                        pass
            finally:
                try:
                    browser.close()
                except Exception:
                    pass

        # Always write a bundle on exit (even if user never navigated).
        return create_debug_bundle(
            debug_dir=str(cap_dir),
            log_file=log_file,
            out_dir=out_dir,
            provider=provider,
        )

    def _storage_state_backup_path(self, state_path: Path) -> Path:
        # e.g. data/servicer_storage_state_nelnet.json -> data/servicer_storage_state_nelnet.json.bak
        return state_path.with_name(state_path.name + ".bak")

    def _validate_or_restore_storage_state(self, state_path: Path) -> bool:
        """
        Return True if we should use `state_path` as Playwright storage_state.

        If the JSON is corrupted, we quarantine it and attempt to restore from `<file>.bak`.
        If that fails, return False so the caller uses a fresh session.
        """
        try:
            raw = state_path.read_text(encoding="utf-8")
            data = json.loads(raw)
            if isinstance(data, dict) and ("cookies" in data or "origins" in data):
                return True
        except Exception:
            pass

        logger.warning("storage_state file is invalid JSON; ignoring and attempting restore from backup: %s", state_path)
        self._quarantine_file(state_path, prefix="storage_state")

        bak = self._storage_state_backup_path(state_path)
        if bak.exists():
            try:
                raw = bak.read_text(encoding="utf-8")
                data = json.loads(raw)
                if isinstance(data, dict) and ("cookies" in data or "origins" in data):
                    shutil.copy2(bak, state_path)
                    logger.warning("Restored storage_state from backup: %s", bak)
                    return True
            except Exception:
                logger.debug("Failed to restore storage_state from backup.", exc_info=True)

        return False

    def _backup_storage_state(self, state_path: Path) -> None:
        """
        Best-effort: keep a last-known-good copy of Playwright storage_state so we can self-heal if the JSON corrupts.
        """
        try:
            bak = self._storage_state_backup_path(state_path)
            # Only write a backup if the JSON looks valid.
            if self._validate_or_restore_storage_state(state_path):
                shutil.copy2(state_path, bak)
        except Exception:
            logger.debug("Failed to write storage_state backup.", exc_info=True)

    def _quarantine_file(self, path: Path, *, prefix: str) -> None:
        try:
            stamp = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ")
            path.replace(path.with_name(f"{prefix}.{path.name}.corrupt-{stamp}"))
        except Exception:
            logger.debug("Failed to quarantine file=%s", path, exc_info=True)

    def _login(
        self,
        page: Page,
        *,
        mfa_code_provider: Optional[Callable[[], str]],
        mfa_method: str,
        debug_dir: str,
        manual_mfa: bool = False,
    ) -> None:
        try:
            page.goto(self.base_url, wait_until="domcontentloaded")
            self._wait_for_settle(page)
            self._step(page, debug_dir=debug_dir, name="after_goto")
            self._dismiss_cookie_banner(page)
            self._step(page, debug_dir=debug_dir, name="after_cookie_dismiss")

            # The portal sometimes routes through an OIDC callback loading page ("Please wait while we gather your data.")
            # before it finishes establishing the session. On slower machines (NAS), this can race our extraction.
            self._wait_for_post_login_ready(page, debug_dir=debug_dir, timeout_ms=60_000)
            self._step(page, debug_dir=debug_dir, name="after_post_login_ready")

            # The portal is a SPA; on slower machines the authenticated dashboard may render a few seconds
            # after DOMContentLoaded (even if storage_state is valid). Wait until we're sure we're either
            # logged in, on the login form, or on an MFA step.
            self._wait_for_auth_state_known(page, debug_dir=debug_dir, timeout_ms=25_000)
            self._step(page, debug_dir=debug_dir, name="after_auth_state_known")

            # If we already have a valid session (storage_state), the portal may land us directly on
            # /dashboard or /loan-details. In that case, skip credential + MFA flow entirely.
            if self._looks_logged_in(page):
                self._step(page, debug_dir=debug_dir, name="already_logged_in")
                return

            frame = self._ensure_login_form_visible(page, debug_dir=debug_dir)
            if frame is None:
                # The SPA may finish routing to the authenticated dashboard while we're trying to locate
                # the login form. Treat that as success.
                self._step(page, debug_dir=debug_dir, name="already_logged_in_after_waiting_for_form")
                return
            self._step(page, debug_dir=debug_dir, name="login_form_visible")

            def _first_visible(scope, selector: str):
                loc = scope.locator(selector)
                try:
                    n = min(int(loc.count()), 25)
                except Exception:
                    n = 0
                for i in range(n):
                    cand = loc.nth(i)
                    try:
                        if cand.is_visible():
                            return cand
                    except Exception:
                        continue
                return None

            user_input = _first_visible(frame, self.selectors.username_input)
            if user_input is None:
                # Common on portals that render hidden template inputs or gate the login UI behind a disclaimer.
                self._save_debug(page, debug_dir=debug_dir, name_prefix="login_username_not_visible")
                raise LoginFormNotFoundError("Login form username field found but none are visible.")

            user_input.fill(self.creds.username)
            self._step(page, debug_dir=debug_dir, name="username_filled")

            # Some logins are two-step (username -> next -> password)
            pwd_input = _first_visible(frame, self.selectors.password_input)
            if pwd_input is None:
                self._click_first_by_texts(frame, self.selectors.sign_in_submit_texts)
                self._wait_for_settle(page)
                pwd_input = _first_visible(frame, self.selectors.password_input)

            if pwd_input is None:
                self._save_debug(page, debug_dir=debug_dir, name_prefix="login_password_not_visible")
                raise LoginFormNotFoundError("Login form password field found but none are visible.")

            pwd_input.fill(self.creds.password)
            self._step(page, debug_dir=debug_dir, name="password_filled")
        except Exception:
            self._save_debug(page, debug_dir=debug_dir, name_prefix="login_failure")
            raise

        # Try common sign-in patterns by button text (button or link)
        self._click_first_by_texts(page, self.selectors.sign_in_submit_texts)

        page.wait_for_timeout(1500)

        if self._looks_like_mfa(page):
            if mfa_method != "email":
                raise RuntimeError(f"Only email MFA is supported by this automation (got: {mfa_method})")
            self._step(page, debug_dir=debug_dir, name="mfa_detected")

            if manual_mfa:
                logger.info(
                    "Manual MFA mode: complete the MFA in the opened browser (enter the code + click Verify)."
                )
                # Wait until we are no longer on an MFA-looking page.
                deadline = time.time() + 180
                while time.time() < deadline:
                    if not self._looks_like_mfa(page):
                        break
                    page.wait_for_timeout(500)
                if self._looks_like_mfa(page):
                    self._save_debug(page, debug_dir=debug_dir, name_prefix="mfa_manual_timeout")
                    raise TimeoutError("Timed out waiting for manual MFA completion.")
            else:
                if not mfa_code_provider:
                    raise RuntimeError(
                        "The portal prompted for MFA but no mfa_code_provider was provided. "
                        "Provide Gmail IMAP config or re-run with --manual-mfa (headful)."
                    )
                try:
                    self._complete_email_mfa(page, mfa_code_provider, debug_dir=debug_dir)
                except Exception:
                    # MFA failures are common during early automation; capture exact screen state.
                    self._save_debug(page, debug_dir=debug_dir, name_prefix="mfa_failure")
                    raise

        # Give the app time to settle after login.
        self._wait_for_settle(page, timeout_ms=30_000)
        # After submitting credentials/MFA, the portal often shows a callback loading screen briefly.
        self._wait_for_post_login_ready(page, debug_dir=debug_dir, timeout_ms=90_000)

        # Ensure we truly ended in an authenticated session before proceeding.
        if not self._looks_logged_in(page):
            self._save_debug(page, debug_dir=debug_dir, name_prefix="login_not_completed")
            reason = self._best_effort_login_failure_reason(page)
            if reason:
                raise RuntimeError(reason)
            raise RuntimeError(
                "Portal login did not complete (not authenticated after credentials/MFA). "
                "This may indicate invalid credentials, a redirect loop, or a stuck post-login callback."
            )

        self._step(page, debug_dir=debug_dir, name="login_complete")

    def _best_effort_login_failure_reason(self, page: Page) -> Optional[str]:
        """
        Try to produce an actionable login failure message from the portal UI.

        This is intentionally conservative: if we can't find a clear message, return None and let the
        caller raise a generic error (with debug artifacts saved).
        """
        try:
            body = page.inner_text("body")
        except Exception:
            body = ""

        txt = (body or "").strip()
        if not txt:
            return None

        # Common invalid-credential wording observed on Aidvantage.
        if re.search(r"can\\s*'\\s*t\\s+find\\s+the\\s+user\\s+id\\s+and\\s+password\\s+combination", txt, re.I):
            attempts_left = None
            m = re.search(r"You\\s+have\\s+(\\d+)\\s+more\\s+attempts?", txt, re.I)
            if m:
                attempts_left = m.group(1)

            extra = f" (attempts left: {attempts_left})" if attempts_left else ""
            return (
                "Login failed: the servicer portal rejected your User ID / Password. "
                "Double-check SERVICER_USERNAME and SERVICER_PASSWORD and try again."
                f"{extra}"
            )

        # Generic invalid/incorrect password messages.
        if re.search(r"(invalid|incorrect).*(user\\s*id|username|password)", txt, re.I):
            return (
                "Login failed: the servicer portal reports your credentials are invalid/incorrect. "
                "Double-check SERVICER_USERNAME and SERVICER_PASSWORD and try again."
            )

        # Account lock / throttling hints.
        if re.search(r"account\\s+will\\s+be\\s+locked|account\\s+locked|too\\s+many\\s+attempts", txt, re.I):
            return (
                "Login failed: the portal indicates your account may be locked or you are out of attempts. "
                "Try logging in manually in a browser to confirm account status, then retry."
            )

        return None

    def _looks_logged_in(self, page: Page) -> bool:
        """
        Heuristic detection of an already-authenticated session.
        """
        # --- Strong "logged out" signals ---
        # The portal sometimes keeps you on `/welcome` even if storage_state exists; do NOT treat that as logged in
        # when the page clearly offers login/registration CTAs.
        try:
            login_btn = page.get_by_role("button", name=re.compile(r"^\s*log\s*in\s*$", re.I))
            if login_btn.count() > 0:
                # If the welcome/register CTAs are present alongside a Log In button, we're logged out.
                if (
                    page.get_by_text("Create Online Account", exact=False).count() > 0
                    or page.get_by_text("Create an Account", exact=False).count() > 0
                ):
                    return False
                if page.get_by_text("Previously logged in", exact=False).count() > 0:
                    return False
        except Exception:
            pass

        # If the login form is present, we're not authenticated.
        try:
            if self._find_frame_with_selector(page, self.selectors.username_input) is not None:
                return False
        except Exception:
            pass

        try:
            url = page.url or ""
            if (
                "/dashboard" in url
                or "/loan-details" in url
                or "/payments/" in url
                or "/payment-activity" in url
                or "/manage" in url
            ):
                return True
        except Exception:
            pass

        # Headings that only appear for authenticated users
        for txt in ("Manage My Account", "My Loans for Account", "Payment Activity"):
            try:
                if page.get_by_text(txt, exact=False).count() > 0:
                    return True
            except Exception:
                continue

        # Authenticated main navigation items (stable in HTML snapshots).
        try:
            # "My Loans" primary nav link points to /loan-details when authenticated.
            if page.locator('a[href="/loan-details"]').count() > 0:
                return True
        except Exception:
            pass
        try:
            # Payments dropdown button ("Payments") exists in authenticated nav.
            if page.locator('button#Payments').count() > 0:
                return True
        except Exception:
            pass
        try:
            # Profile dropdown button ("Matthew" in your screenshot) exists in authenticated nav.
            if page.locator("button#myProfileButton").count() > 0:
                return True
        except Exception:
            pass

        # Sign-out UI is a strong signal we are authenticated.
        for pat in (r"sign\s*out", r"log\s*out"):
            try:
                if page.get_by_role("link", name=re.compile(pat, re.I)).count() > 0:
                    return True
            except Exception:
                pass
            try:
                if page.get_by_role("button", name=re.compile(pat, re.I)).count() > 0:
                    return True
            except Exception:
                pass

        return False

    def _ensure_login_form_visible(self, page: Page, *, debug_dir: str) -> Optional[Frame]:
        """
        The portal may show a landing page with a "Sign in" button/link, or may render the form inside an iframe.
        This tries to get us to the actual login form and returns the frame containing it.
        """
        # Try a few times: click sign-in entry, then re-check for form.
        for _ in range(8):
            # If the app finishes routing to an authenticated dashboard while we're hunting for the login form,
            # bail out and let the caller treat this as "already logged in".
            if self._looks_logged_in(page):
                return None

            frame = self._find_frame_with_selector(page, self.selectors.username_input, require_visible=True)
            if frame:
                return frame

            # Ensure consent UI isn't intercepting clicks.
            self._dismiss_cookie_banner(page, timeout_ms=3_000)

            # Some portals gate the login form behind a federal usage disclaimer ("Accept / Decline").
            # Example: Aidvantage renders the inputs in HTML but hidden until clicking `button#Accept`.
            try:
                looks_like_disclaimer = (
                    page.get_by_text("Please Read Before Continuing", exact=False).count() > 0
                    or page.get_by_text("Unauthorized use of this information system", exact=False).count() > 0
                )
                accept = page.locator(self.selectors.federal_disclaimer_accept_selector)
                if looks_like_disclaimer and accept.count() > 0 and accept.first.is_visible():
                    accept.first.click()
                    self._wait_for_settle(page, timeout_ms=20_000)
                    self._step(page, debug_dir=debug_dir, name="after_accept_disclaimer")
                    continue
            except Exception:
                pass

            # Some flows show a pre-login choice page (Access Your Account vs Make a Payment...).
            if self._maybe_complete_login_choice(page):
                self._wait_for_settle(page, timeout_ms=20_000)
                self._step(page, debug_dir=debug_dir, name="after_login_choice")
                continue

            # Try clicking a "Sign in / Log in" entry point if present.
            self._click_first_by_texts(page, self.selectors.sign_in_entry_texts, ignore_missing=True)

            # Fallback: some entry points aren't exposed as semantic buttons/links.
            for t in self.selectors.sign_in_entry_texts:
                try:
                    cand = page.get_by_text(t, exact=False)
                    if cand.count() > 0:
                        cand.first.click()
                        break
                except Exception:
                    continue

            self._wait_for_settle(page)
            self._step(page, debug_dir=debug_dir, name="after_click_signin_entry")

        self._save_debug(page, debug_dir=debug_dir, name_prefix="login_form_not_found")
        raise LoginFormNotFoundError("Could not find login form (username field) on page")

    def _wait_for_auth_state_known(self, page: Page, *, debug_dir: str, timeout_ms: int = 25_000) -> None:
        """
        The portal is a SPA and sometimes renders the authenticated dashboard a few seconds after DOMContentLoaded,
        especially on slower machines. This waits until we can confidently classify the page as one of:
        - already authenticated
        - login form visible (username input present)
        - MFA flow visible

        This is intentionally best-effort (no exception) because portal pages vary; it just reduces race conditions.
        """
        deadline = time.time() + (timeout_ms / 1000)
        while time.time() < deadline:
            # Keep overlays out of the way while we wait.
            self._dismiss_cookie_banner(page, timeout_ms=3_000)

            if self._looks_logged_in(page):
                return

            try:
                if self._find_frame_with_selector(page, self.selectors.username_input, require_visible=True) is not None:
                    return
            except Exception:
                pass

            if self._looks_like_mfa(page):
                return

            page.wait_for_timeout(500)

    def _maybe_complete_login_choice(self, page: Page) -> bool:
        """
        On the portal welcome/login flow, there's a page with radio choices:
        - Access Your Account (desired)
        - Make a Payment for Someone Else
        and a Continue button.
        """
        try:
            # Primary detection via stable attributes from the HTML snapshot.
            borrower_radio = page.locator(self.selectors.login_choice_borrower_radio_selector)
            continue_btn = page.locator(self.selectors.login_choice_continue_selector)
            if borrower_radio.count() > 0 and continue_btn.count() > 0:
                self._dismiss_cookie_banner(page)

                try:
                    borrower_radio.first.check()
                except Exception:
                    borrower_radio.first.click()

                continue_btn.first.click()

                # The portal shows a federal usage disclaimer dialog after clicking Continue.
                try:
                    accept = page.locator(self.selectors.federal_disclaimer_accept_selector).first
                    accept.wait_for(state="visible", timeout=10_000)
                    accept.click()
                except Exception:
                    # If no disclaimer appears (or it was already accepted), continue.
                    pass

                self._wait_for_settle(page, timeout_ms=20_000)
                return True

            # Detect by presence of the radio / label text.
            access_radio = page.get_by_role(
                "radio", name=re.compile(re.escape(self.selectors.login_choice_access_text), re.I)
            )
            if access_radio.count() == 0:
                # Sometimes the label isn't wired to radio role correctly; fall back to text.
                if page.get_by_text(self.selectors.login_choice_access_text, exact=False).count() == 0:
                    return False

            # Ensure cookie banner isn't blocking.
            self._dismiss_cookie_banner(page)

            # Select the desired option.
            if access_radio.count() > 0:
                try:
                    access_radio.first.check()
                except Exception:
                    access_radio.first.click()
            else:
                # Click the label text as fallback
                page.get_by_text(self.selectors.login_choice_access_text, exact=False).first.click()

            # Click Continue
            cont = page.get_by_role(
                "button", name=re.compile(re.escape(self.selectors.login_choice_continue_text), re.I)
            )
            if cont.count() > 0:
                cont.first.click()
                # Attempt to accept disclaimer if prompted
                try:
                    accept = page.locator(self.selectors.federal_disclaimer_accept_selector).first
                    accept.wait_for(state="visible", timeout=10_000)
                    accept.click()
                except Exception:
                    pass
                return True

            # Sometimes Continue is a link-styled button
            cont_link = page.get_by_role(
                "link", name=re.compile(re.escape(self.selectors.login_choice_continue_text), re.I)
            )
            if cont_link.count() > 0:
                cont_link.first.click()
                try:
                    accept = page.locator(self.selectors.federal_disclaimer_accept_selector).first
                    accept.wait_for(state="visible", timeout=10_000)
                    accept.click()
                except Exception:
                    pass
                return True

            return False
        except Exception:
            logger.debug("Failed while attempting login-choice step; continuing.", exc_info=True)
            return False

    def _find_frame_with_selector(self, page: Page, selector: str, *, require_visible: bool = False) -> Optional[Frame]:
        for frame in page.frames:
            try:
                loc = frame.locator(selector)
                if loc.count() <= 0:
                    continue
                if not require_visible:
                    return frame
                # Only consider the selector "present" if at least one match is visible.
                # Some portals render hidden template inputs behind a disclaimer gate.
                for i in range(min(int(loc.count()), 25)):
                    try:
                        if loc.nth(i).is_visible():
                            return frame
                    except Exception:
                        continue
            except Exception:
                continue
        return None

    def _click_first_by_texts(self, scope, texts: tuple[str, ...], *, ignore_missing: bool = False) -> None:
        """
        Click the first matching button/link by accessible name. `scope` can be Page or Frame.
        """
        for t in texts:
            # button
            try:
                btn = scope.get_by_role("button", name=re.compile(re.escape(t), re.I))
                if btn.count() > 0:
                    btn.first.click()
                    return
            except Exception:
                pass
            # link
            try:
                link = scope.get_by_role("link", name=re.compile(re.escape(t), re.I))
                if link.count() > 0:
                    link.first.click()
                    return
            except Exception:
                pass

        if not ignore_missing:
            raise RuntimeError(f"Could not find clickable element for any of: {texts}")

    def _wait_for_settle(self, page: Page, *, timeout_ms: int = 10_000) -> None:
        """
        Avoid `networkidle` — many modern sites keep background requests running forever.
        """
        try:
            page.wait_for_load_state("domcontentloaded", timeout=timeout_ms)
        except Exception:
            pass
        page.wait_for_timeout(500)
        # Consent banners are often injected after initial load; try to clear them quickly.
        self._dismiss_cookie_banner(page, timeout_ms=3_000)

    def _looks_like_post_login_loading(self, page: Page) -> bool:
        """
        Detect the portal's post-login OIDC callback loading view (seen as:
        "Loading... Please wait while we gather your data.").

        On slower machines this can last long enough that our extraction starts before the authenticated
        UI/navigation is available.
        """
        try:
            # Distinctive callback "loading card" (present in your debug HTML).
            if page.locator('[data-cy="loading-card"]').count() > 0:
                return True
        except Exception:
            pass

        try:
            if page.get_by_text("Please wait while we gather your data.", exact=False).count() > 0:
                return True
        except Exception:
            pass

        try:
            # Component tag present in HTML snapshots.
            if page.locator("app-callback").count() > 0:
                return True
        except Exception:
            pass

        return False

    def _wait_for_post_login_ready(self, page: Page, *, debug_dir: str, timeout_ms: int = 90_000) -> None:
        """
        If the portal is currently showing the post-login callback loading view, wait for it to disappear.
        """
        if not self._looks_like_post_login_loading(page):
            return

        logger.info("Portal is finalizing the login session (callback loading). Waiting up to %.1fs...", timeout_ms / 1000)
        deadline = time.time() + (timeout_ms / 1000)
        while time.time() < deadline:
            if not self._looks_like_post_login_loading(page):
                return

            # Consent banners can still pop in and block; keep them cleared.
            self._dismiss_cookie_banner(page, timeout_ms=3_000)
            page.wait_for_timeout(500)

        self._save_debug(page, debug_dir=debug_dir, name_prefix="post_login_loading_timeout")
        raise TimeoutError("Portal post-login loading did not finish in time (still showing callback spinner).")

    def _wait_for_body_text_contains(self, page: Page, needle: str, *, timeout_ms: int) -> bool:
        """
        Browser-side polling for a substring in `document.body.innerText`.
        Returns True if found within timeout, otherwise False.
        """
        try:
            page.wait_for_function(
                "(needle) => (document.body && (document.body.innerText || '')).includes(needle)",
                arg=needle,
                timeout=timeout_ms,
            )
            return True
        except Exception:
            return False

    def _dismiss_cookie_banner(self, page: Page, *, timeout_ms: int = 20_000) -> None:
        """
        Best-effort cookie banner dismissal (doesn't fail if not present).

        The portal uses a Transcend consent manager that may be injected late and/or inside an iframe,
        and sometimes isn't exposed via accessibility roles. We therefore search all frames and
        click by visible text.
        """
        # First, try to hide the Transcend host (works even if consent is rendered in a CLOSED shadow root).
        try:
            page.evaluate(
                """
                () => {
                  const host = document.getElementById('transcend-consent-manager');
                  if (!host) return false;
                  host.style.setProperty('display', 'none', 'important');
                  host.style.setProperty('pointer-events', 'none', 'important');
                  return true;
                }
                """
            )
        except Exception:
            pass

        # Poll briefly; the consent UI is often injected after DOMContentLoaded.
        attempts = max(1, int(timeout_ms / 250))
        for _ in range(attempts):
            try:
                frames = page.frames
            except Exception:
                frames = []

            for fr in frames:
                try:
                    # If the banner isn't present, don't spam clicks.
                    banner_present = fr.get_by_text("This site uses cookies", exact=False)
                    if banner_present.count() == 0:
                        continue

                    # Consent banner buttons: "Accept all" / "Reject all"
                    accept_btn = fr.locator('button:has-text("Accept all")')
                    if accept_btn.count() > 0:
                        accept_btn.first.click(timeout=2_000, force=True)
                        page.wait_for_timeout(300)
                        return

                    # Text fallback (sometimes the element isn't a <button>)
                    accept_all = fr.get_by_text("Accept all", exact=False)
                    if accept_all.count() > 0:
                        accept_all.first.click(timeout=2_000, force=True)
                        page.wait_for_timeout(300)
                        return

                    # Fallback: other common consent button phrasings
                    for txt in ("Accept", "I agree", "Agree", "Got it", "OK"):
                        cand = fr.get_by_text(txt, exact=False)
                        if cand.count() > 0:
                            cand.first.click(timeout=2_000, force=True)
                            page.wait_for_timeout(300)
                            return
                except Exception:
                    continue

            page.wait_for_timeout(250)

        # Final fallback: attempt to remove the host entirely (only affects consent UI).
        try:
            page.evaluate(
                """
                () => {
                  const host = document.getElementById('transcend-consent-manager');
                  if (!host) return;
                  host.remove();
                }
                """
            )
        except Exception:
            pass

    def _looks_like_mfa(self, page: Page) -> bool:
        # Heuristic: presence of a numeric code input and Email option.
        code_inputs = page.locator(self.selectors.mfa_code_input)
        if code_inputs.count() > 0:
            return True
        # Text hints
        for hint in ("verification code", "one-time", "MFA", "security code"):
            if page.get_by_text(hint, exact=False).count() > 0:
                return True
        return False

    def _complete_email_mfa(self, page: Page, mfa_code_provider: Callable[[], str], *, debug_dir: str) -> None:
        # Best-effort click Email option if present.
        try:
            email_choice = page.get_by_text(self.selectors.mfa_email_option_text, exact=False)
            if email_choice.count() > 0:
                email_choice.first.click()
        except Exception:
            logger.debug("Could not click Email MFA option (continuing).", exc_info=True)

        # Best-effort click Send/Continue.
        for t in (self.selectors.mfa_send_code_text, "Continue", "Next"):
            try:
                btn = page.get_by_role("button", name=t)
                if btn.count() > 0:
                    btn.first.click()
                    break
            except Exception:
                continue

        # Wait for code input to be visible.
        page.wait_for_timeout(1000)
        self._step(page, debug_dir=debug_dir, name="mfa_code_input_visible")

        # Best-effort: check "remember this device/client" so the portal may skip MFA for ~90 days.
        # If the portal sets a trust cookie, our storage_state persistence will carry it between runs.
        for pat in (r"remember.*90", r"remember", r"90\s*days"):
            try:
                cb = page.get_by_role("checkbox", name=re.compile(pat, re.I))
                if cb.count() > 0:
                    cb.first.check()
                    logger.info("Checked 'remember device' option during MFA.")
                    break
            except Exception:
                continue

        code = mfa_code_provider()

        # Capture before typing the code (avoid saving the actual code in screenshots).
        self._step(page, debug_dir=debug_dir, name="mfa_before_code_entry")
        code_input = page.locator(self.selectors.mfa_code_input).first
        code_input.fill(code)

        # Verify
        try:
            page.get_by_role("button", name=self.selectors.mfa_verify_text).click()
        except Exception:
            # Some flows use "Submit"
            page.get_by_role("button", name=re.compile(r"verify|submit|continue", re.I)).click()

        self._wait_for_settle(page)
        self._step(page, debug_dir=debug_dir, name="mfa_after_submit")
        page.wait_for_timeout(750)

        # If the code was rejected, stop immediately (otherwise later steps fail confusingly).
        if page.get_by_text("Invalid code entered", exact=False).count() > 0:
            raise RuntimeError("Portal rejected the MFA code as invalid (likely stale/incorrect email parsed).")

        # If we're still on an MFA-looking page, also stop.
        if self._looks_like_mfa(page):
            raise RuntimeError("Portal MFA did not complete (still showing MFA prompt after submitting code).")

    def _looks_like_browser_error(self, page: Page) -> bool:
        """
        Detect Chrome/Chromium "This site can't be reached" style error pages.
        These often surface as `chrome-error://...` and can happen if the portal redirects to
        `dark.<servicer>.studentaid.gov` which may not resolve for some users.
        """
        try:
            url = page.url or ""
            if url.startswith("chrome-error://"):
                return True
        except Exception:
            pass

        try:
            title = page.title() or ""
            if self._dark_host and self._dark_host in title.lower():
                return True
        except Exception:
            pass

        try:
            body = page.inner_text("body")
            if "DNS_PROBE_FINISHED_NXDOMAIN" in body:
                return True
            if "This site can’t be reached" in body or "This site can't be reached" in body:
                return True
        except Exception:
            pass

        return False

    def _extract_loans(self, page: Page, *, groups: list[str], debug_dir: str) -> list[LoanSnapshot]:
        self._step(page, debug_dir=debug_dir, name="loans_before_nav_my_loans")
        # Race-condition guard: on slower machines the app may still be on the post-login callback screen.
        self._wait_for_post_login_ready(page, debug_dir=debug_dir, timeout_ms=90_000)

        # Try nav first (best-effort), but fall back to direct navigation if nav isn't ready/available.
        self._goto_section(page, self.selectors.nav_my_loans_text, debug_dir=debug_dir)

        # Wait for the loan details content to actually render before parsing.
        if not self._wait_for_body_text_contains(page, "Group:", timeout_ms=15_000):
            try:
                page.goto(f"{self.base_url}/loan-details", wait_until="domcontentloaded")
                self._wait_for_settle(page, timeout_ms=30_000)
            except Exception:
                # We'll validate below; if still not loaded, we'll raise with debug artifacts.
                pass

        if not self._wait_for_body_text_contains(page, "Group:", timeout_ms=30_000):
            self._save_debug(page, debug_dir=debug_dir, name_prefix="loan_details_not_loaded")
            raise RuntimeError("Loan details page did not load (missing 'Group:' sections).")

        self._step(page, debug_dir=debug_dir, name="loans_after_nav_my_loans")

        # The "My Loans" page lists all groups on a single page. Our earlier approach of
        # parsing the entire page for each group caused every group to pick the *first* match.
        # Instead, slice the page text per-group and parse within that slice.
        full_text = page.inner_text("body")

        sections = self._extract_all_group_sections(full_text)
        if not sections:
            self._save_debug(page, debug_dir=debug_dir, name_prefix="loan_details_no_groups_found")
            raise RuntimeError("Could not find any 'Group:' sections on the loan details page.")

        out: list[LoanSnapshot] = []
        for group in groups:
            try:
                self._step(page, debug_dir=debug_dir, name=f"loans_before_parse_group_{group}")
                group_text = self._match_group_section_text(sections, group=group)
                out.append(self._parse_loan_snapshot(group=group, body_text=group_text))
            except Exception:
                self._save_debug(page, debug_dir=debug_dir, name_prefix=f"loan_{group}_error")
                raise
        return out

    def _extract_all_group_sections(self, full_text: str) -> list[tuple[str, str, str]]:
        """
        Return a list of discovered group sections from the loan-details page.

        Each item is a tuple: (group_token, group_label, section_text)
        - group_label: the raw text after "Group:" on the header line
        - group_token: a short ID parsed from the start of group_label (e.g. "AA", "1-01") when possible

        Notes:
        - Servicers are not consistent about group label formats. We avoid hardcoding AA/AB assumptions.
        - group_token is best-effort and may be empty if we can't parse a token.
        """
        # Find every "Group:" header and slice to the next header (or end of text).
        matches = list(re.finditer(r"Group:\s*([^\n\r]+)", full_text, flags=re.I))
        if not matches:
            return []

        out: list[tuple[str, str, str]] = []
        for i, m in enumerate(matches):
            start = m.start()
            end = matches[i + 1].start() if i + 1 < len(matches) else len(full_text)
            section_text = full_text[start:end]

            label = (m.group(1) or "").strip()
            tok_m = re.match(r"([A-Z0-9][A-Z0-9-]{1,31})", label, flags=re.I)
            token = tok_m.group(1).upper() if tok_m else ""

            out.append((token, label, section_text))
        return out

    def _match_group_section_text(self, sections: list[tuple[str, str, str]], *, group: str) -> str:
        """
        Resolve a configured group ID to a discovered section_text.

        Matching strategy:
        1) token match (configured group == parsed token)
        2) prefix match (group_label startswith configured group)
        3) raw label match (configured group == group_label)
        """
        g = (group or "").strip()
        if not g:
            raise RuntimeError("Empty loan group provided.")

        g_up = g.upper()

        # 1) exact token match
        for token, label, section_text in sections:
            if token and token.upper() == g_up:
                return section_text

        # 2) label prefix match (covers cases like "Group: 1-01 Direct Loan - Subsidized" with group="1-01")
        for token, label, section_text in sections:
            if (label or "").strip().upper().startswith(g_up):
                return section_text

        # 3) raw label match (fallback)
        for token, label, section_text in sections:
            if (label or "").strip().upper() == g_up:
                return section_text

        # Not found: build a helpful error with discovered groups.
        discovered_tokens = [t for (t, _, _) in sections if t]
        # De-dupe but preserve order
        seen: set[str] = set()
        tokens = []
        for t in discovered_tokens:
            if t in seen:
                continue
            tokens.append(t)
            seen.add(t)

        labels = [lbl for (_, lbl, _) in sections if (lbl or "").strip()]
        labels = labels[:12]  # keep error readable

        hint = ""
        if tokens:
            hint = f" Discovered group IDs: {', '.join(tokens)}."
        elif labels:
            hint = f" Discovered group labels: {', '.join(labels)}."

        raise RuntimeError(
            f"Could not locate a loan group section for group={group!r}.{hint} "
            "Tip: run `studentaid_monarch_sync list-loan-groups` to print a copy/paste LOAN_GROUPS value."
        )

    def _extract_group_section_text(self, full_text: str, *, group: str) -> str:
        """
        Extract the text for a single loan group from the "My Loans" page.

        The UI renders each group section with a header like "Group: AA" (some servicers
        include longer identifiers, e.g. "Group: 1-01 Direct Loan - Subsidized").
        We slice from that header to the next "Group:" header (or end of text).
        """
        # Case-insensitive: some portals may render labels with mixed case, while config/env
        # normalization may uppercase tokens.
        start_match = re.search(rf"Group:\s*{re.escape(group)}\b", full_text, flags=re.I)
        if not start_match:
            raise RuntimeError(f"Could not locate group section header for group={group}")

        start = start_match.start()
        remainder = full_text[start_match.end() :]

        # Find the next group header. Do not assume a specific ID format; servicers vary.
        next_match = re.search(r"\n\s*Group:\s*", remainder, flags=re.I)
        end = start_match.end() + next_match.start() if next_match else len(full_text)

        return full_text[start:end]

    def _open_group(self, page: Page, *, group: str, debug_dir: str) -> None:
        # Best-effort click into a group details view.
        patterns = [
            f"Group: {group}",
            f"Group {group}",
            group,
        ]
        clicked = False
        for pat in patterns:
            try:
                loc = page.get_by_text(pat, exact=False)
                if loc.count() > 0:
                    loc.first.click()
                    clicked = True
                    break
            except Exception:
                continue

        if not clicked:
            self._save_debug(page, debug_dir=debug_dir, name_prefix=f"loan_{group}_not_found")
            raise RuntimeError(f"Could not find clickable element for loan group {group}")

        # Wait for group header/text
        page.wait_for_timeout(750)
        if page.get_by_text(f"Group: {group}", exact=False).count() == 0:
            # Not fatal; some pages only show group code.
            logger.debug("Group header text not found after click (group=%s).", group)

    def _parse_loan_snapshot(self, *, group: str, body_text: str) -> LoanSnapshot:
        # Money fields
        principal = self._money_after(r"Principal Balance:\s*", body_text)
        outstanding = self._money_after(r"Outstanding Balance:\s*", body_text)
        daily_interest = self._money_after(r"Total Daily Interest Accrual:\s*", body_text, default=0)

        # Unpaid accrued interest line includes an \"as-of\" date embedded; ignore the date for now.
        accrued_interest = self._money_after(r"Unpaid Accrued Interest.*?:\s*", body_text)

        # Dates
        due_date = self._date_after(r"Due Date:\s*", body_text, default=None)

        last_payment_amount, last_payment_date = self._last_payment(body_text)

        eff_rate = self._text_after(r"Effective Interest Rate:\s*", body_text, default=None)
        reg_rate = self._text_after(r"Regulatory Interest Rate:\s*", body_text, default=None)

        return LoanSnapshot(
            group=group,
            principal_balance_cents=principal,
            accrued_interest_cents=accrued_interest,
            outstanding_balance_cents=outstanding,
            daily_interest_accrual_cents=daily_interest,
            due_date=due_date,
            last_payment_date=last_payment_date,
            last_payment_amount_cents=last_payment_amount,
            raw_effective_interest_rate=eff_rate,
            raw_regulatory_interest_rate=reg_rate,
        )

    def _last_payment(self, body_text: str) -> tuple[Optional[int], Optional[date]]:
        m = re.search(
            r"Last Payment Received:\s*(\$?[\d,]+\.\d{2})\s+on\s+(\d{1,2}/\d{1,2}/\d{4})",
            body_text,
        )
        if not m:
            return None, None
        return money_to_cents(m.group(1)), parse_us_date(m.group(2))

    def _money_after(self, label_pattern: str, text: str, *, default: Optional[int] = None) -> int:
        m = re.search(label_pattern + r"(\$?[\d,]+\.\d{2})", text)
        if not m:
            if default is None:
                raise RuntimeError(f"Could not find money for pattern: {label_pattern}")
            return default
        return money_to_cents(m.group(1))

    def _date_after(self, label_pattern: str, text: str, *, default: Optional[date]) -> Optional[date]:
        m = re.search(label_pattern + r"(\d{1,2}/\d{1,2}/\d{4})", text)
        if not m:
            return default
        return parse_us_date(m.group(1))

    def _text_after(self, label_pattern: str, text: str, *, default: Optional[str]) -> Optional[str]:
        m = re.search(label_pattern + r"([^\n\r]+)", text)
        if not m:
            return default
        return m.group(1).strip()

    def _extract_payment_allocations(
        self,
        page: Page,
        *,
        debug_dir: str,
        max_payments_to_scan: int,
        payments_since: Optional[date] = None,
    ) -> list[PaymentAllocation]:
        # Best-effort: navigate to payment activity and open the first N payment details.
        self._wait_for_post_login_ready(page, debug_dir=debug_dir, timeout_ms=90_000)
        self._step(page, debug_dir=debug_dir, name="payments_before_nav_payment_activity")
        self._goto_section(page, self.selectors.nav_payment_activity_text, debug_dir=debug_dir)
        self._step(page, debug_dir=debug_dir, name="payments_after_nav_payment_activity")

        # Primary strategy: click the Payment Date links in the history table (they are the most stable entry point).
        # These appear as links like "11/26/2025".
        # Payment date entries may be links, buttons, or plain clickable cells depending on UI changes.
        date_re = re.compile(r"^\s*\d{1,2}/\d{1,2}/\d{4}\s*$")
        date_texts: list[str] = []
        for loc in (
            page.get_by_role("link", name=date_re),
            page.get_by_role("button", name=date_re),
            page.get_by_text(date_re),
        ):
            try:
                date_texts = [t.strip() for t in loc.all_inner_texts() if t.strip()]
            except Exception:
                date_texts = []
            if date_texts:
                break

        if date_texts:
            # Keep order but drop duplicates.
            seen: set[str] = set()
            ordered_dates: list[str] = []
            for t in date_texts:
                if t in seen:
                    continue
                seen.add(t)
                ordered_dates.append(t)

            allocations: list[PaymentAllocation] = []
            opened = 0
            for raw_idx, dt_str in enumerate(ordered_dates):
                if opened >= max_payments_to_scan:
                    break

                payment_dt = parse_us_date(dt_str)
                if payments_since and payment_dt < payments_since:
                    # The Payment Activity list is typically newest-first. Stop scanning once we hit
                    # entries older than the cutoff to avoid opening lots of historical detail pages.
                    logger.info(
                        "Stopping payment scan at %s (older than cutoff %s).",
                        payment_dt.isoformat(),
                        payments_since.isoformat(),
                    )
                    break

                idx = opened
                opened += 1
                try:
                    # Ensure we're on the Payment Activity list before each click.
                    self._goto_section(page, self.selectors.nav_payment_activity_text, debug_dir=debug_dir)
                    self._step(page, debug_dir=debug_dir, name=f"payments_before_open_{idx}_{dt_str}")

                    # Click the row/date by text (most robust across role changes / shadow DOM).
                    try:
                        page.get_by_role("link", name=dt_str).first.click()
                    except Exception:
                        try:
                            page.get_by_role("button", name=dt_str).first.click()
                        except Exception:
                            page.get_by_text(dt_str, exact=True).first.click()
                    self._wait_for_settle(page)
                    self._step(page, debug_dir=debug_dir, name=f"payments_after_open_{idx}_{dt_str}")

                    body_text = page.inner_text("body")
                    allocations.extend(self._parse_payment_allocations(body_text, payment_date=payment_dt))
                except Exception:
                    self._save_debug(page, debug_dir=debug_dir, name_prefix=f"payment_detail_{idx}_error")
                    raise
                finally:
                    # Return to Payment Activity list without relying on browser history.
                    try:
                        self._goto_section(page, self.selectors.nav_payment_activity_text, debug_dir=debug_dir)
                        self._wait_for_settle(page)
                    except Exception:
                        self._close_payment_detail(page)

            return allocations

        # Gather clickable \"View/Details\" elements.
        openers = None
        for open_text in self.selectors.payment_detail_open_texts:
            try:
                candidate = page.get_by_role("link", name=re.compile(open_text, re.I))
                if candidate.count() == 0:
                    candidate = page.get_by_role("button", name=re.compile(open_text, re.I))
                if candidate.count() > 0:
                    openers = candidate
                    break
            except Exception:
                continue

        if openers is None or openers.count() == 0:
            logger.warning("Could not find payment detail openers; skipping payment allocation extraction.")
            self._save_debug(page, debug_dir=debug_dir, name_prefix="payment_activity_no_openers")
            return []

        allocations: list[PaymentAllocation] = []
        count = min(openers.count(), max_payments_to_scan)
        for idx in range(count):
            try:
                openers.nth(idx).click()
                page.wait_for_timeout(750)

                # Wait for details page to contain expected text.
                for ready_text in self.selectors.payment_detail_ready_texts:
                    if page.get_by_text(ready_text, exact=False).count() > 0:
                        break

                body_text = page.inner_text("body")
                parsed = self._parse_payment_allocations(body_text)
                if payments_since and parsed and parsed[0].payment_date < payments_since:
                    logger.info(
                        "Stopping payment scan at %s (older than cutoff %s).",
                        parsed[0].payment_date.isoformat(),
                        payments_since.isoformat(),
                    )
                    break
                allocations.extend(parsed)
            except Exception:
                self._save_debug(page, debug_dir=debug_dir, name_prefix=f"payment_detail_{idx}_error")
                raise
            finally:
                self._close_payment_detail(page)

        return allocations

    def _parse_payment_allocations(self, body_text: str, payment_date: Optional[date] = None) -> list[PaymentAllocation]:
        # Payment date (prefer caller-provided date from the clicked row to avoid ambiguity)
        if payment_date is None:
            payment_date = self._find_payment_date(body_text)

        # Payment reference (optional)
        ref = None
        for pat in (
            r"Confirmation\s*Number:\s*([A-Z0-9-]+)",
            r"Payment\s*ID:\s*([A-Z0-9-]+)",
            r"Reference:\s*([A-Z0-9-]+)",
        ):
            m = re.search(pat, body_text, re.I)
            if m:
                ref = m.group(1)
                break

        lines = [ln.strip() for ln in body_text.splitlines() if ln.strip()]

        group_rows: list[tuple[str, int, int, int]] = []
        total_payment_cents: Optional[int] = None

        for ln in lines:
            # Group rows like: \"AA  $31.20  $19.78  $11.42\"
            m = re.match(
                r"^([A-Z0-9][A-Z0-9-]{1,31})\s+\$?([\d,]+\.\d{2})\s+\$?([\d,]+\.\d{2})\s+\$?([\d,]+\.\d{2})\s*$",
                ln,
            )
            if m:
                group = m.group(1).upper()
                total_applied = money_to_cents(m.group(2))
                principal = money_to_cents(m.group(3))
                interest = money_to_cents(m.group(4))
                group_rows.append((group, total_applied, principal, interest))
                continue

            # Total row: \"Total $278.52 $184.12 $94.40\"
            m2 = re.match(
                r"^Total\s+\$?([\d,]+\.\d{2})\s+\$?([\d,]+\.\d{2})\s+\$?([\d,]+\.\d{2})\s*$",
                ln,
                re.I,
            )
            if m2 and total_payment_cents is None:
                total_payment_cents = money_to_cents(m2.group(1))

        if not group_rows:
            raise RuntimeError("Could not parse any group allocation rows from payment detail page")

        if total_payment_cents is None:
            # Fallback: sum the group totals (should equal payment total)
            total_payment_cents = sum(r[1] for r in group_rows)

        return [
            PaymentAllocation(
                payment_date=payment_date,
                group=group,
                total_applied_cents=total_applied,
                principal_applied_cents=principal,
                interest_applied_cents=interest,
                payment_total_cents=total_payment_cents,
                payment_reference=ref,
            )
            for (group, total_applied, principal, interest) in group_rows
        ]

    def _find_payment_date(self, body_text: str) -> date:
        m = re.search(r"(Payment\s*Date|Date)\s*:\s*(\d{1,2}/\d{1,2}/\d{4})", body_text, re.I)
        if m:
            return parse_us_date(m.group(2))
        # Fallback: if there is exactly one date in the details view, use it.
        dates = re.findall(r"\b\d{1,2}/\d{1,2}/\d{4}\b", body_text)
        uniq = list(dict.fromkeys(dates))
        if len(uniq) == 1:
            return parse_us_date(uniq[0])
        raise RuntimeError(f"Could not reliably determine payment date from detail page (found {len(uniq)} dates)")

    def _close_payment_detail(self, page: Page) -> None:
        # Best-effort: close modal/detail view without relying on browser history (SPA).
        for t in self.selectors.payment_detail_close_texts:
            try:
                btn = page.get_by_role("button", name=t)
                if btn.count() > 0:
                    btn.first.click()
                    page.wait_for_timeout(500)
                    return
            except Exception:
                continue

        # Try navigating back to Payment Activity explicitly.
        try:
            self._goto_section(page, self.selectors.nav_payment_activity_text, debug_dir="data/debug")
            page.wait_for_timeout(500)
            return
        except Exception:
            pass

        # Fallback: browser back.
        try:
            page.go_back()
            page.wait_for_timeout(500)
            return
        except Exception:
            pass

        # Last resort: ESC
        try:
            page.keyboard.press("Escape")
            page.wait_for_timeout(500)
        except Exception:
            pass

    def _goto_section(self, page: Page, nav_texts: tuple[str, ...], *, debug_dir: str) -> None:
        """
        Best-effort navigation helper for a SPA.

        Notes:
        - Portals often render multiple matching elements (header nav, dashboard cards, footer).
          Clicking `.first` is brittle; we instead try all candidates.
        - We prefer candidates that look like real navigation targets:
          elements with `href` or `routerlink` attributes, and ones whose URL contains words
          from the nav label (e.g. "payment-activity" for "Payment Activity").
        - Some labels (e.g. "Payments") may be dropdown toggles (no href/routerlink). We'll click them
          to expand menus, then re-scan for the real destination links.
        """

        def _label_words(label: str) -> list[str]:
            # Keep alphanumerics, split on spaces/punct. Drop very short/common words.
            raw = re.findall(r"[a-z0-9]+", (label or "").casefold())
            stop = {"the", "and", "or", "my", "a", "an", "to", "of"}
            return [w for w in raw if len(w) >= 3 and w not in stop]

        def _candidate_score(*, label: str, href: str, routerlink: str, visible: bool) -> int:
            target = (href or routerlink or "").strip().casefold()
            score = 0
            if href:
                score += 50
            if routerlink:
                score += 50
            if target:
                for w in _label_words(label):
                    if w in target:
                        score += 10
            if visible:
                score += 5
            else:
                score -= 100
            return score

        def _try_locator_group(label: str, loc) -> tuple[bool, bool]:
            """
            Try clicking all candidates in a locator group.
            Returns (navigated, clicked_non_nav):
            - navigated=True if we successfully clicked a candidate with href/routerlink
            - clicked_non_nav=True if we clicked something without href/routerlink (likely a menu toggle)
            """
            try:
                n = loc.count()
            except Exception:
                return False, False

            # Cap to avoid pathological matches in case a label is too generic.
            n = min(int(n), 25)
            if n <= 0:
                return False, False

            candidates: list[tuple[int, object, str, str]] = []
            for i in range(n):
                el = loc.nth(i)
                href = ""
                routerlink = ""
                visible = False
                try:
                    visible = bool(el.is_visible())
                except Exception:
                    visible = False
                try:
                    href = (el.get_attribute("href") or "").strip()
                except Exception:
                    href = ""
                try:
                    # Angular uses `routerlink` (lowercase in HTML), but keep this defensive.
                    routerlink = (el.get_attribute("routerlink") or el.get_attribute("routerLink") or "").strip()
                except Exception:
                    routerlink = ""

                score = _candidate_score(label=label, href=href, routerlink=routerlink, visible=visible)
                candidates.append((score, el, href, routerlink))

            # Highest score first.
            candidates.sort(key=lambda x: x[0], reverse=True)

            clicked_non_nav = False
            for score, el, href, routerlink in candidates:
                try:
                    if not el.is_visible():
                        continue
                    el.scroll_into_view_if_needed(timeout=2_000)
                    el.click(timeout=5_000)
                    self._wait_for_settle(page)
                    if href or routerlink:
                        logger.debug(
                            "Navigation click succeeded (label=%r href=%r routerlink=%r score=%s)",
                            label,
                            href,
                            routerlink,
                            score,
                        )
                        return True, False
                    # Likely a toggle; allow a rescan so newly visible menu items can be clicked.
                    logger.debug(
                        "Clicked non-nav candidate (label=%r; no href/routerlink; score=%s) - will rescan.",
                        label,
                        score,
                    )
                    clicked_non_nav = True
                    return False, True
                except Exception:
                    continue

            return False, clicked_non_nav

        # Do a few rounds to allow "toggle then click submenu" patterns.
        for _round in range(3):
            expanded_menu = False
            for t in nav_texts:
                pat = re.compile(re.escape(t), re.I)

                navigated, clicked_non_nav = _try_locator_group(t, page.get_by_role("link", name=pat))
                if navigated:
                    return
                expanded_menu = expanded_menu or clicked_non_nav

                navigated, clicked_non_nav = _try_locator_group(t, page.get_by_role("button", name=pat))
                if navigated:
                    return
                expanded_menu = expanded_menu or clicked_non_nav

                # Fallback: for longer labels, try clicking by visible text (can match custom elements).
                # We keep this low-impact by still prioritizing candidates with href/routerlink.
                if len(t) >= 7 or " " in t:
                    navigated, clicked_non_nav = _try_locator_group(t, page.get_by_text(t, exact=False))
                    if navigated:
                        return
                    expanded_menu = expanded_menu or clicked_non_nav

            if not expanded_menu:
                break

        # If we cannot navigate, dump debug and keep going (caller may still be on correct page).
        logger.warning("Could not navigate using texts=%s; continuing.", nav_texts)
        self._save_debug(page, debug_dir=debug_dir, name_prefix="nav_failed")

    def _save_debug(self, page: Page, *, debug_dir: str, name_prefix: str) -> None:
        try:
            out_dir = Path(debug_dir)
            out_dir.mkdir(parents=True, exist_ok=True)
            page.screenshot(path=str(out_dir / f"{name_prefix}.png"), full_page=True)
            (out_dir / f"{name_prefix}.html").write_text(page.content(), encoding="utf-8")
            # Also save the rendered body text so parsing can be debugged offline without DOM tooling.
            try:
                (out_dir / f"{name_prefix}.txt").write_text(page.inner_text("body"), encoding="utf-8")
            except Exception:
                pass
        except Exception:
            logger.debug("Failed to save debug artifacts.", exc_info=True)

    def _step(self, page: Page, *, debug_dir: str, name: str) -> None:
        """
        If enabled, save a step-by-step screenshot so the user can see where the flow fails.
        """
        if not self._step_debug_enabled:
            return

        self._step_counter += 1
        safe = re.sub(r"[^a-zA-Z0-9_-]+", "_", name).strip("_")[:60] or "step"
        prefix = f"step_{self._step_counter:02d}_{safe}"

        try:
            out_dir = Path(debug_dir)
            out_dir.mkdir(parents=True, exist_ok=True)
            page.screenshot(path=str(out_dir / f"{prefix}.png"), full_page=True)
        except Exception:
            logger.debug("Failed to save step screenshot (name=%s).", name, exc_info=True)

        try:
            logger.info("Step %02d %s (url=%s)", self._step_counter, name, getattr(page, "url", ""))
        except Exception:
            pass

        if self._step_delay_ms > 0:
            try:
                page.wait_for_timeout(self._step_delay_ms)
            except Exception:
                pass


