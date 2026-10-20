"""Supplementary tests for the extended redaction labels, wording and quota advice.
Run from the repository root: python -m unittest discover -s tests -v
"""
import os
import sys
import tempfile
import unittest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
import auraroute_core as core

INVOICE = (
    "Name: Jane Mary Doe\n"
    "Passport No: X1234567\n"
    "Phone: +44 20 7946 0000\n"
    "Email: jane.doe@example.com\n"
    "Government ID: 110101199001011234\n"
    "Address: 1 Example Road, London\n"
    "Student ID: S-99812\n"
    "Date of Birth: 2001-02-03\n"
    "Institution: Example University\n"
    "Total Due: \u00a312,000.50\n"
    "Currency: GBP\n"
    "Due Date: 2026-12-15\n"
)


class ExtendedRedactionTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.mkdtemp()
        core.AUDIT_PATH = os.path.join(self.tmp, "audit.log")

    def test_all_labeled_identifiers_removed(self):
        _, text = core.parse_and_sanitize_invoice(INVOICE)
        for secret in ("Jane Mary Doe", "X1234567", "+44 20 7946 0000", "jane.doe@example.com",
                       "110101199001011234", "1 Example Road", "S-99812", "2001-02-03"):
            self.assertNotIn(secret, text)

    def test_institution_line_survives_and_fields_parse(self):
        schema, _ = core.parse_and_sanitize_invoice(INVOICE)
        self.assertEqual(schema["institution"], "Example University")
        self.assertEqual(schema["amount"], 12000.50)      # cents preserved
        self.assertEqual(schema["due_date"], "2026-12-15")

    def test_over_quota_advice_uses_bank_vocabulary_and_never_splits(self):
        payload = {"amount": 12000.0, "currency": "GBP", "due_date": "2026-12-15",
                   "human_verified": True}
        res = core.execute_compliance_routing_check(
            payload, {"home_country": "CN", "annual_quota_used": 42000.0},
            balances_fn=lambda: [{"amount": 16500.0, "currency": "USD"}])
        notes = " ".join(res["advisory_notes"])
        self.assertFalse(res["within_standard_quota"])
        self.assertIn("admission letter and the tuition certificate", notes)
        self.assertIn("Do not split the payment", notes)
        self.assertIn("illustrative framework baseline", notes)
        self.assertTrue(res["visa_balance_warning"])
        self.assertEqual(res["latest_initiation_date"], "2026-12-10")

    def test_unverified_payload_blocked(self):
        with self.assertRaises(ValueError):
            core.execute_compliance_routing_check(
                {"amount": 1.0, "currency": "USD", "due_date": "2026-12-15",
                 "human_verified": False}, {"home_country": "CN"})


if __name__ == "__main__":
    unittest.main()
