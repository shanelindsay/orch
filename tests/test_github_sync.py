import unittest

import github_sync


SAMPLE_BODY = """
## Goal
Make foo faster by reducing redundant database reads.

## Acceptance checklist
- [ ] profile current behaviour
- [x] implement caching layer
- [ ] document configuration flag

## Scope notes
- src/foo/
- docs/

## Validation
Run `pytest tests/foo` and share the summary.
"""


class ParseIssueBodyTests(unittest.TestCase):
    def test_parse_issue_body_sections(self) -> None:
        charter = github_sync.parse_issue_body(SAMPLE_BODY)
        self.assertEqual(
            charter.goal,
            "Make foo faster by reducing redundant database reads.",
        )
        self.assertEqual(
            charter.acceptance,
            [
                "profile current behaviour",
                "implement caching layer",
                "document configuration flag",
            ],
        )
        self.assertEqual(charter.scope_notes, ["src/foo/", "docs/"])
        self.assertEqual(
            charter.validation,
            "Run `pytest tests/foo` and share the summary.",
        )

    def test_format_issue_prompt(self) -> None:
        charter = github_sync.parse_issue_body(SAMPLE_BODY)
        issue = github_sync.IssueDetails(
            number=128,
            title="Speed up foo",
            state="open",
            url="https://example.test/issue/128",
            labels=["orchestrate", "P1"],
            body=SAMPLE_BODY,
        )
        prompt = github_sync.format_issue_prompt(issue, charter)
        self.assertIn("Work on Issue #128: Speed up foo", prompt)
        self.assertIn("Goal: Make foo faster", prompt)
        self.assertIn("1. profile current behaviour", prompt)
        self.assertIn("Scope: src/foo/; docs/", prompt)
        self.assertIn("Labels: P1, orchestrate", prompt)

    def test_missing_sections_graceful(self) -> None:
        body = "## Goal\nShip v1.0\n"  # no other sections provided
        charter = github_sync.parse_issue_body(body)
        self.assertEqual(charter.acceptance, [])
        self.assertEqual(charter.scope_notes, [])
        self.assertEqual(charter.validation, "")


if __name__ == "__main__":
    unittest.main()
