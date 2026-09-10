"""Invoice table and customer drawer must scroll under the Iterable theme.

The Iterable theme used to set overflow:hidden on .table-wrap, which beat
the base overflow:auto rule and clipped ~144 invoice rows with no scrollbar.
"""

from __future__ import annotations

import http.server
import os
import re
import subprocess
import tempfile
import threading
import unittest
from pathlib import Path

APP = Path(__file__).resolve().parent / "app.py"
CHROME = "/Applications/Google Chrome.app/Contents/MacOS/Google Chrome"
LIVE_URL = os.environ.get(
    "AR_E2E_URL",
    "https://ar-collections-123894796141.us-central1.run.app/",
)


def css_from_source(src: str) -> str:
    start = src.index('CSS = """') + len('CSS = """')
    end = src.index('"""', start)
    return src[start:end]


def css_from_app() -> str:
    return css_from_source(APP.read_text())


def rule_body(css: str, selector: str) -> str:
    pattern = re.escape(selector) + r"\s*\{([^}]*)\}"
    match = re.search(pattern, css)
    if not match:
        raise AssertionError(f"missing CSS rule for {selector!r}")
    return " ".join(match.group(1).split())


def overflow_value(decls: str, prop: str = "overflow") -> str | None:
    match = re.search(rf"{prop}\s*:\s*([^;]+);?", decls)
    return match.group(1).strip() if match else None


def fixture_html(css: str) -> str:
    rows = "\n".join(
        f"<tr><td>{i}</td><td>Customer {i}</td><td>INV{i:05d}</td>"
        f"<td>$1,000.00</td><td>120+ days</td></tr>"
        for i in range(1, 41)
    )
    drawer_lines = "\n".join(f"<p>Contact {i} — detail row</p>" for i in range(1, 80))
    return f"""<!DOCTYPE html>
<html data-theme="iterable">
<head>
<meta charset="utf-8">
<style>{css}</style>
</head>
<body>
<div class="table-wrap" id="table-wrap">
  <table id="ar-table">
    <thead><tr><th>Acct</th><th>Customer</th><th>Invoice</th><th>Amount</th><th>Bucket</th></tr></thead>
    <tbody>{rows}</tbody>
    <tfoot><tr><td colspan="5">40 invoices</td></tr></tfoot>
  </table>
</div>
<div class="drawer open" id="customer-drawer">
  <div class="drawer-header"><h3>Customer detail</h3></div>
  <div class="drawer-body">{drawer_lines}</div>
</div>
<script>
function report(id, wrap) {{
  const cs = getComputedStyle(wrap);
  const y = (cs.overflowY || cs.overflow || "").toLowerCase();
  const clipped = y === "hidden";
  const overflowed = wrap.scrollHeight > wrap.clientHeight + 1;
  wrap.scrollTop = 0;
  wrap.scrollTop = 400;
  const scrolled = wrap.scrollTop > 50;
  const ok = overflowed && !clipped && scrolled;
  document.body.setAttribute("data-" + id + "-overflow", y);
  document.body.setAttribute("data-" + id + "-scroll-height", String(wrap.scrollHeight));
  document.body.setAttribute("data-" + id + "-client-height", String(wrap.clientHeight));
  document.body.setAttribute("data-" + id + "-scroll-top", String(wrap.scrollTop));
  document.body.setAttribute("data-" + id + "-result", ok ? "PASS" : "FAIL");
}}
report("table", document.getElementById("table-wrap"));
report("drawer", document.getElementById("customer-drawer"));
</script>
</body>
</html>
"""


def loading_fixture_html(css: str) -> str:
    return f"""<!DOCTYPE html>
<html data-theme="iterable">
<head><meta charset="utf-8"><style>{css}</style></head>
<body>
<main class="ar-loading-shell" id="ar-loading-shell" aria-busy="true">
  <section class="loading-metrics">
    <div class="loading-card"><span></span><span></span><span></span></div>
    <div class="loading-card"><span></span><span></span><span></span></div>
    <div class="loading-card"><span></span><span></span><span></span></div>
  </section>
  <section class="loading-stage">
    <div class="loading-orbit"></div>
    <strong>Loading accounts receivable</strong>
    <p>Connecting to Snowflake and preparing collections data…</p>
  </section>
</main>
<script>
requestAnimationFrame(function () {{
  document.body.setAttribute(
    "data-animation-count",
    String(document.getAnimations().length)
  );
}});
</script>
</body>
</html>
"""


def chrome_dump(html: str) -> str:
    if not Path(CHROME).exists():
        raise unittest.SkipTest("Google Chrome is not installed")
    with tempfile.TemporaryDirectory() as tmp:
        (Path(tmp) / "index.html").write_text(html)

        class Handler(http.server.SimpleHTTPRequestHandler):
            def __init__(self, *args, **kwargs):
                super().__init__(*args, directory=tmp, **kwargs)

            def log_message(self, *args):
                return

        httpd = http.server.HTTPServer(("127.0.0.1", 0), Handler)
        threading.Thread(target=httpd.serve_forever, daemon=True).start()
        url = f"http://127.0.0.1:{httpd.server_address[1]}/index.html"
        try:
            proc = subprocess.run(
                [
                    CHROME,
                    "--headless=new",
                    "--disable-gpu",
                    "--no-first-run",
                    "--window-size=1280,800",
                    "--dump-dom",
                    url,
                ],
                capture_output=True,
                text=True,
                timeout=30,
            )
        finally:
            httpd.shutdown()
            httpd.server_close()
        if proc.returncode != 0:
            raise AssertionError(
                f"Chrome exited {proc.returncode}: {proc.stderr[-2000:]}"
            )
        return proc.stdout


def _attr(dom: str, name: str) -> str | None:
    match = re.search(rf'data-{name}="([^"]*)"', dom)
    return match.group(1) if match else None


class TestIterableTableOverflowCss(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.css = css_from_app()

    def test_base_table_wrap_scrolls(self):
        body = rule_body(self.css, ".table-wrap")
        self.assertEqual(overflow_value(body), "auto")
        self.assertIn("max-height: 70vh", body)

    def test_iterable_theme_does_not_clip_table(self):
        body = rule_body(self.css, '[data-theme="iterable"] .table-wrap')
        self.assertNotEqual(overflow_value(body), "hidden")
        self.assertNotEqual(overflow_value(body, "overflow-y"), "hidden")

    def test_customer_drawer_scrolls(self):
        body = rule_body(self.css, ".drawer")
        self.assertEqual(overflow_value(body, "overflow-y"), "auto")


class TestIterableTableScrollInChrome(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.dom = chrome_dump(fixture_html(css_from_app()))

    def test_invoice_table_scrolls_under_iterable_theme(self):
        self.assertEqual(_attr(self.dom, "table-result"), "PASS")
        self.assertNotEqual(_attr(self.dom, "table-overflow"), "hidden")

    def test_customer_drawer_scrolls(self):
        self.assertEqual(_attr(self.dom, "drawer-result"), "PASS")
        self.assertNotEqual(_attr(self.dom, "drawer-overflow"), "hidden")


class TestNonBlockingLoadingShell(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.source = APP.read_text()

    def test_snowflake_does_not_block_module_import(self):
        self.assertNotIn("_startup_conn = _get_connection()", self.source)
        self.assertIn("def _load_data_and_warm_cache()", self.source)
        self.assertIn('@app.on_event("startup")', self.source)
        self.assertRegex(
            self.source,
            r"threading\.Thread\(\s*target=_background_refresh,"
        )
        self.assertRegex(
            self.source,
            r"def _background_refresh\(\):\s+_load_data_and_warm_cache\(\)",
        )

    def test_loading_shell_is_accessible_and_polls_readiness(self):
        self.assertIn('id="ar-loading-shell"', self.source)
        self.assertIn('aria_busy="true"', self.source)
        self.assertIn("data_ready", self.source)
        self.assertIn("pollDataReady", self.source)
        self.assertIn("window.location.reload()", self.source)

    def test_loading_shell_animates_in_chrome(self):
        dom = chrome_dump(loading_fixture_html(css_from_app()))
        count = int(_attr(dom, "animation-count") or "0")
        self.assertGreater(count, 0)


class TestCollectionsInteractionMarkup(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.source = APP.read_text()

    def test_hero_keeps_help_but_removes_refresh_theme_and_user(self):
        self.assertIn('Button("?", cls="help-btn"', self.source)
        self.assertNotIn('A("Refresh", href="/refresh"', self.source)
        self.assertNotIn('cls="theme-toggle"', self.source)
        self.assertNotIn('cls="ar-hero-user"', self.source)
        self.assertNotIn("function toggleTheme()", self.source)
        self.assertNotIn("localStorage.getItem('ar-theme')", self.source)

    def test_invoice_row_opens_customer_drawer(self):
        self.assertRegex(
            self.source,
            r"data_inv=r\['INVOICE_NUMBER'\],\s+"
            r"onclick=f\"openDrawer\(",
        )
        self.assertIn('tabindex="0"', self.source)
        self.assertIn("event.key === 'Enter'", self.source)

    def test_notes_click_does_not_open_customer_drawer(self):
        self.assertRegex(
            self.source,
            r"cls=\"notes-cell\",\s+onclick=f\"event\.stopPropagation\(\); "
            r"openNotes\(",
        )


class TestLiveAppSmoke(unittest.TestCase):
    """Hits the deployed Cloud Run service. Skip with AR_E2E_LIVE=0."""

    @classmethod
    def setUpClass(cls):
        if os.environ.get("AR_E2E_LIVE", "0").lower() not in ("1", "true", "yes"):
            raise unittest.SkipTest("set AR_E2E_LIVE=1 to hit the deployed app")

    def test_home_renders_invoice_table(self):
        import urllib.request

        req = urllib.request.Request(
            LIVE_URL,
            headers={"User-Agent": "ar-collections-e2e"},
        )
        with urllib.request.urlopen(req, timeout=30) as resp:
            self.assertEqual(resp.status, 200)
            html = resp.read().decode("utf-8", errors="replace")
        self.assertIn('id="ar-table"', html)
        self.assertIn('class="table-wrap"', html)
        self.assertIn("invoices", html.lower())
        iterable_rule = rule_body(html, '[data-theme="iterable"] .table-wrap')
        self.assertNotEqual(
            overflow_value(iterable_rule),
            "hidden",
            "live Iterable theme still clips .table-wrap with overflow:hidden",
        )


if __name__ == "__main__":
    unittest.main()
