"""Unit tests (no Paperless or Ollama needed). Run: python -m unittest discover -s tests -v"""

import datetime as dt
import sys
import unittest
from pathlib import Path
from unittest import mock

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

import classify as c  # noqa: E402

TYPES = {"Invoice": 1, "Payslip": 2}
CORRS = {"Acme Energy": 10, "City Council": 11}


class TitleTest(unittest.TestCase):
    def test_generated_titles(self):
        for t in [
            "",
            "Fwd: your bill",
            "RE: info",
            "Scan_0012",
            "IMG20260101",
            "scan 3",
            "document",
            "2026-10-01_invoice",
            "UNTITLED DOCUMENT FROM THE SCANNER",
        ]:
            self.assertTrue(c.looks_generated(t), t)

    def test_real_titles_kept(self):
        for t in [
            "Electricity bill October",
            "Payslip September 2026",
            "Letter from the council",
        ]:
            self.assertFalse(c.looks_generated(t), t)


class DecideTest(unittest.TestCase):
    def doc(self, **kw):
        return {
            "id": 1,
            "title": "Scan_0001",
            "tags": [],
            "document_type": None,
            "correspondent": None,
            **kw,
        }

    def test_all_fields(self):
        answer = {
            "type": "Invoice",
            "correspondent": "Acme Energy",
            "title": "Electricity bill",
        }
        patch, changes = c.decide(self.doc(), answer, TYPES, CORRS, "ACME Energy Ltd - invoice 123")
        self.assertEqual(
            patch,
            {"document_type": 1, "correspondent": 10, "title": "Electricity bill"},
        )
        self.assertEqual(changes, ["type=Invoice", "correspondent=Acme Energy", "title"])

    def test_invented_correspondent_rejected(self):
        answer = {"type": "none", "correspondent": "City Council", "title": "x"}
        patch, _ = c.decide(self.doc(), answer, TYPES, CORRS, "A letter from somebody else")
        self.assertNotIn("correspondent", patch)

    def test_unknown_type_and_existing_fields_untouched(self):
        answer = {"type": "Contract", "correspondent": "Acme Energy", "title": "New"}
        patch, _ = c.decide(
            self.doc(document_type=2, correspondent=11, title="Real title here"),
            answer,
            TYPES,
            CORRS,
            "Acme Energy",
        )
        self.assertEqual(patch, {})

    def test_multiline_title_cut(self):
        patch, _ = c.decide(self.doc(), {"title": "Bill\nmore text"}, TYPES, CORRS, "")
        self.assertEqual(patch["title"], "Bill")

    def test_switches(self):
        answer = {"type": "Invoice", "correspondent": "Acme Energy", "title": "Bill"}
        with (
            mock.patch.object(c, "SET_TITLE", False),
            mock.patch.object(c, "SET_TYPE", False),
        ):
            patch, _ = c.decide(self.doc(), answer, TYPES, CORRS, "Acme Energy")
        self.assertEqual(patch, {"correspondent": 10})


class HelpersTest(unittest.TestCase):
    def test_schema_limits_choices(self):
        s = c.schema(list(TYPES), list(CORRS))
        self.assertEqual(s["properties"]["type"]["enum"], ["Invoice", "Payslip", "none"])
        self.assertEqual(
            s["properties"]["correspondent"]["enum"],
            ["Acme Energy", "City Council", "none"],
        )

    def test_prompt_language(self):
        with mock.patch.object(c, "DOC_LANGUAGE", "Italian"):
            self.assertIn("IN ITALIAN", c.prompt(list(TYPES), "t", "x"))

    def test_local_url(self):
        with mock.patch.object(c, "API", "http://paperless:8000/api"):
            self.assertEqual(
                c.local_url("https://docs.example.com/api/tags/?page=2"),
                "http://paperless:8000/api/tags/?page=2",
            )

    def test_seconds_until(self):
        now = dt.datetime(2026, 10, 27, 4, 0)
        self.assertEqual(c.seconds_until("04:15", now), 15 * 60)
        self.assertEqual(c.seconds_until("03:00", now), 23 * 3600)

    def test_stop_time(self):
        with mock.patch.object(c, "STOP_AT", "06:30"):
            self.assertEqual(
                c.stop_time(dt.datetime(2026, 10, 27, 4, 0)),
                dt.datetime(2026, 10, 27, 6, 30),
            )
            self.assertEqual(
                c.stop_time(dt.datetime(2026, 10, 27, 7, 0)),
                dt.datetime(2026, 10, 28, 6, 30),
            )
        with mock.patch.object(c, "STOP_AT", ""):
            self.assertIsNone(c.stop_time(dt.datetime(2026, 10, 27, 4, 0)))


class RunTest(unittest.TestCase):
    """Full run against a fake Paperless API and a fake model."""

    def test_run_patches_and_tags(self):
        docs = {
            5: {
                "id": 5,
                "title": "Scan_5",
                "content": "Acme Energy invoice",
                "tags": [3],
                "document_type": None,
                "correspondent": None,
            }
        }
        patches = {}

        def fake_api(path, method="GET", data=None, timeout=60):
            if path.startswith("/document_types/"):
                return {
                    "results": [{"name": n, "id": i} for n, i in TYPES.items()],
                    "next": None,
                }
            if path.startswith("/correspondents/"):
                return {
                    "results": [{"name": n, "id": i} for n, i in CORRS.items()],
                    "next": None,
                }
            if path.startswith("/tags/"):
                return {
                    "results": [{"id": 90 if "review" in path else 91}],
                    "next": None,
                }
            if path.startswith("/documents/?"):
                return {"results": [{"id": 5}]}
            if method == "PATCH":
                patches[path] = data
                return {}
            return docs[5]

        answer = {
            "type": "Invoice",
            "correspondent": "Acme Energy",
            "title": "Energy bill",
        }
        with (
            mock.patch.object(c, "TOKEN", "t"),
            mock.patch.object(c, "api", fake_api),
            mock.patch.object(c, "ask", return_value=answer),
            mock.patch("builtins.print"),
        ):
            rc = c.run()
        self.assertEqual(rc, 0)
        self.assertEqual(
            patches["/documents/5/"],
            {
                "document_type": 1,
                "correspondent": 10,
                "title": "Energy bill",
                "tags": [3, 90, 91],
            },
        )


if __name__ == "__main__":
    unittest.main()
