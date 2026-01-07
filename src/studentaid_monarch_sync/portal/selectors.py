from __future__ import annotations

from dataclasses import dataclass


@dataclass(frozen=True)
class PortalSelectors:
    """
    StudentAid servicer portals are web portals; selectors may change over time.
    Keep all UI selectors/text hooks here for easy maintenance.
    """

    # Login
    username_input: str = (
        'input[name="username"], input[id="username"], input[name*="user" i], input[id*="user" i], '
        'input[type="email"], input[autocomplete="username"], input#okta-signin-username'
    )
    password_input: str = (
        'input[name="password"], input[id="password"], input[name*="pass" i], input[id*="pass" i], '
        'input[type="password"], input[autocomplete="current-password"], input#okta-signin-password'
    )
    # Some portals require clicking an initial "Sign in" / "Log in" before fields appear.
    sign_in_entry_texts: tuple[str, ...] = ("Sign in", "Sign In", "Log in", "Log In", "Login")
    sign_in_submit_texts: tuple[str, ...] = ("Sign in", "Sign In", "Log in", "Log In", "Login", "Continue", "Next")

    # Pre-login choice page (seen after clicking Log In on /welcome)
    # Example options:
    # - "Access Your Student Loan Account" (desired)
    # - "Make a Payment for Someone Else"
    login_choice_access_text: str = "Access Your Student Loan Account"
    login_choice_continue_text: str = "Continue"
    login_choice_borrower_radio_selector: str = 'input[type="radio"][data-cy="borrower-radio"], input[type="radio"]#borrower'
    login_choice_continue_selector: str = 'button#continue-button, button[data-cy="submit-form"]'
    # Various portals show a federal usage disclaimer gate that must be accepted before proceeding.
    # Known variants:
    # - CRI-style: button#accept-disclaimer / data-cy="accept-disclaimer"
    # - Aidvantage-style: button#Accept (shows "Please Read Before Continuing" with Accept/Decline)
    federal_disclaimer_accept_selector: str = (
        'button#accept-disclaimer, button[data-cy="accept-disclaimer"], button#Accept'
    )

    # MFA
    mfa_email_option_text: str = "Email"
    mfa_send_code_text: str = "Send"
    mfa_code_input: str = 'input[type="tel"], input[inputmode="numeric"], input[name*="code" i]'
    mfa_verify_text: str = "Verify"

    # Navigation
    nav_my_loans_text: tuple[str, ...] = ("My Loans", "My Loan", "Loans")
    nav_payment_activity_text: tuple[str, ...] = ("Payment Activity", "Payment History", "Payments")

    # Payment activity
    payment_detail_open_texts: tuple[str, ...] = ("View", "Details")
    payment_detail_ready_texts: tuple[str, ...] = ("Applied to Principal", "Applied to Interest", "Total Applied")
    payment_detail_close_texts: tuple[str, ...] = ("Back", "Close")


# Backward-compatible alias (older code referenced `CriSelectors`).
CriSelectors = PortalSelectors
