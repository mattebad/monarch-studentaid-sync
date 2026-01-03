from __future__ import annotations

import re

from studentaid_monarch_sync.cri.mfa import _extract_code, _strip_html_to_text


def test_strip_html_to_text_removes_style_and_tags_and_unescapes() -> None:
    html = """
    <html>
      <head>
        <style>
          body { color: #265179; }
        </style>
        <script>console.log('x')</script>
      </head>
      <body>
        Hello&nbsp;<b>World</b>
        <!-- comment -->
      </body>
    </html>
    """
    text = _strip_html_to_text(html)
    assert "265179" not in text  # stripped from <style>
    assert "console.log" not in text
    assert "comment" not in text
    assert text == "Hello World"


def test_extract_code_prefers_html_h2_callout() -> None:
    body = '<p class="h2 something"> 437311 </p><p>ignore 123456</p>'
    preferred = [re.compile(r'<p[^>]*class="[^"]*\\bh2\\b[^"]*"[^>]*>\\s*(\\d{6})\\s*</p>', re.I)]
    fallback = re.compile(r"\b(\d{6})\b")
    assert _extract_code(body, preferred_res=preferred, fallback_re=fallback) == "437311"


def test_extract_code_ignores_css_hex_like_numbers() -> None:
    # The digits are present, but preceded by '#', which should be ignored (CSS hex).
    body = "Primary color is #265179. No OTP here."
    preferred: list[re.Pattern[str]] = []
    fallback = re.compile(r"\b(\d{6})\b")
    assert _extract_code(body, preferred_res=preferred, fallback_re=fallback) is None


def test_extract_code_matches_phrase_based_patterns_after_stripping() -> None:
    body = "<div>Authorization Code: <b>123456</b></div>"
    preferred = [re.compile(r"authorization\\s+code[^0-9]{0,30}(\\d{6})", re.I)]
    fallback = re.compile(r"\b(\d{6})\b")
    assert _extract_code(body, preferred_res=preferred, fallback_re=fallback) == "123456"


def test_extract_code_returns_none_when_missing() -> None:
    body = "<html><body>no code here</body></html>"
    preferred: list[re.Pattern[str]] = []
    fallback = re.compile(r"\b(\d{6})\b")
    assert _extract_code(body, preferred_res=preferred, fallback_re=fallback) is None


