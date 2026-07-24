from __future__ import annotations

import re
import unittest
from pathlib import Path


REPO_ROOT = Path(__file__).resolve().parents[1]
WEB_ROOT = REPO_ROOT / "src" / "cloudagent_platform" / "web"


class ConsoleDesignContractTest(unittest.TestCase):
    @classmethod
    def setUpClass(cls) -> None:
        cls.css = (WEB_ROOT / "console.css").read_text(encoding="utf-8")
        cls.js = (WEB_ROOT / "console.js").read_text(encoding="utf-8")
        cls.html = (WEB_ROOT / "index.html").read_text(encoding="utf-8")

    def test_calm_precision_semantic_tokens_and_restraint(self) -> None:
        for contract in (
            "--canvas: #ffffff",
            "--surface: #f7f7f8",
            "--text: #1f1f1f",
            "--primary: #171717",
            "--accent: #0d7a5f",
            "--focus: #0067c0",
            '"Geist Sans"',
            '"Geist Mono"',
        ):
            self.assertIn(contract, self.css)
        self.assertNotIn("gradient(", self.css)
        self.assertNotIn("transition: all", self.css)

    def test_focus_touch_input_and_motion_contracts(self) -> None:
        self.assertRegex(
            self.css,
            re.compile(
                r":focus-visible\s*\{[^}]*outline:\s*2px solid var\(--focus\)",
                re.DOTALL,
            ),
        )
        mobile = self.css.split("@media (max-width: 40rem)", maxsplit=1)[1]
        self.assertIn("min-height: 2.75rem", mobile)
        self.assertIn("font-size: 1rem", mobile)
        self.assertIn("@media (prefers-reduced-motion: reduce)", self.css)

    def test_navigation_session_workspace_and_draft_contracts(self) -> None:
        for contract in (
            "URLSearchParams(window.location.search)",
            'window.addEventListener("popstate"',
            'history[replace ? "replaceState" : "pushState"]',
            "/api/v1/admin/sessions/",
            "/workspace",
            "DRAFT_KEY_PREFIX",
            "messageForm.requestSubmit()",
            "Open workspace",
        ):
            self.assertIn(contract, self.js)
        self.assertNotIn(
            "const [session, events, artifacts, usage, pending, audit]",
            self.js,
        )

    def test_document_keeps_accessible_shell_contract(self) -> None:
        self.assertIn('<html lang="en">', self.html)
        self.assertIn('class="skip-link" href="#main"', self.html)
        self.assertIn('aria-live="polite"', self.html)
        self.assertIn('data-testid="console-app"', self.html)


if __name__ == "__main__":
    unittest.main()
