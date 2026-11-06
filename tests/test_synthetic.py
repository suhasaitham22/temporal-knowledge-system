"""Synthetic fixtures only. No crawled credit-union pages."""

import unittest
from datetime import date

from solution import build


class Page:
    def __init__(self, url, title, markdown, fetched_at="2026-01-01T00:00:00Z"):
        self.url = url
        self.title = title
        self.markdown = markdown
        self.fetched_at = fetched_at


class Snap:
    def __init__(self, snapshot_id, taken_on, pages):
        self.snapshot_id = snapshot_id
        self.taken_on = taken_on
        self.pages = pages


def _numbers(text):
    import re
    found = set()
    for match in re.finditer(r"(\$)?\s*(\d{1,3}(?:,\d{3})+|\d+)(?:\.(\d+))?\s*(%|percent)?", text or "", re.I):
        unit = "$" if match.group(1) else ("%" if match.group(4) else "")
        value = float(match.group(2).replace(",", "") + (("." + match.group(3)) if match.group(3) else ""))
        found.add((unit, round(value, 4)))
    return found


def no_new_numbers(answer_text, question):
    return not (_numbers(answer_text) - _numbers(question))


RATES = """
# Rates

Share Savings

| Account Type | Balance Tier | Dividend Rate | Annual Percentage Yield (APY) |
| --- | --- | --- | --- |
| Share Savings | $10,000 - $24,999.99 | 0.15% | 0.15% |
| Share Savings | $25,000+ | 0.20% | 0.20% |

Certificates

| Certificate | Dividend Rate | Annual Percentage Yield (APY) | Min. to Earn |
| --- | --- | --- | --- |
| 17 Month Fixed Jumbo Certificate Special | 4.15%1 | 4.20%1 | $100,000.00 |
| 17 Month Fixed Certificate Special | 4.06% | 4.10% | $1,000.00 |
| 10 Month Fixed Jumbo Certificate Special | 3.86% | 3.90% | $100,000.00 |

Checking Accounts

| Account Type | Balance Tier | Dividend Rate | Annual Percentage Yield (APY) |
| --- | --- | --- | --- |
| 1st Rate Checking | $0.01 - $4,999.99 | 0.00% | 0.00% |
| 1st Rate Checking | $5,000 - $24,999.99 | 2.47% | 2.50% |
| 1st Rate Checking | $25,000 - $99,999.99 | 0.10% | 0.10% |

Auto Loans

| Loan Program | Term | Annual Percentage Rate (As Low As) |
| --- | --- | --- |
| 2024-27 Model Year | 60 Months | 5.39% APR1 |
| 2024-27 Model Year | 72 Months | 5.49% APR |
| 2021-23 Model Year | 60 Months | 5.64% APR |
| 2017-20 Model Year | 60 Months | 6.34% APR |

Home Loans - Purchase

| Program | Rate | Points | APR | Payment Example |
| --- | --- | --- | --- | --- |
| Purchase: 30-Year Fixed Conventional $100,000 or less | 7.13% | 0 | 7.27% | $673.80 |
| Purchase: 30-Year Fixed Conventional $300,000 or less | 7.25% | 0 | 7.32% | $2,046.53 |

Home Loans - Refinance

| Program | Rate | Points | APR |
| --- | --- | --- | --- |
| Refinance: 30-Year Fixed Conventional $100,000 or less | 7.13% | 0 | 7.28% |

Bright Savings

| Account Type | Balance Tier | Dividend Rate | Annual Percentage Yield (APY) |
| --- | --- | --- | --- |
| Bright Savings | $0.01 - $1,000,000 | 2.23% | 2.25% |

Achieve Money Market

| Account Type | Balance Tier | Dividend Rate for 1st 6 Months | Annual Percentage Yield (APY) for 1st 6 Months | Dividend Rate After Promotional Period | APY After Promotional Period |
| --- | --- | --- | --- | --- | --- |
| Achieve Money Market | $25,000-$99,999.99 | 4.00% | 3.66% | 3.20% | 3.25% |

[See details](https://example-cu.test/personal/savings-account)
"""

CALC = """
# Vehicle calculator

| Loan Program | Term | Annual Percentage Rate (As Low As) |
| --- | --- | --- |
| 2023-26 Model Year | 60 Months | 5.34% APR |
| 2023-26 Model Year | 72 Months | 5.44% APR |
| 2021-22 Model Year | 60 Months | 5.59% APR |
| 2017-20 Model Year | 60 Months | 6.29% APR |

Boat loans

| Loan Type | Term | APR (As Low As) |
| --- | --- | --- |
| $40,000+ | 96 Months | 8.34% APR |
"""

GUIDE = """
# Homebuying guidance

| Term | Rate | Points | APR |
| --- | --- | --- | --- |
| Mortgage Rate Relief Program for qualified Borrowers | 5.125% | 0.00% | 5.196% |
| Purchase: 30-Year Fixed Conventional $100,000 or less | 5.750% | 0.00% | 5.884% |

Rates are effective as of 04/22/2026.
"""

JUNE_CERT = """
# Rates
Special Certificates
17-month jumbo certificate special APY: 3.95%
Rates are effective as of 06/09/2026.
"""

AUG_CERT = """
# Rates
Special Certificates
17-month jumbo certificate special APY: 4.05%
Rates are effective as of 07/14/2026.
"""


class TemporalTests(unittest.TestCase):
    def test_as_of_uses_latest_on_or_before_and_abstains_before_first(self):
        ks = build()
        ks.ingest(Snap("june", "2026-06-09", [Page("https://example-cu.test/rates", "Rates", JUNE_CERT)]))
        ks.ingest(Snap("aug", "2026-08-27", [Page("https://example-cu.test/rates", "Rates", AUG_CERT)]))
        early = ks.answer("What APY did the seventeen-month jumbo certificate special show?", as_of="2026-07-20")
        self.assertIn("3.95%", early.text)
        self.assertNotIn("4.05%", early.text)
        self.assertTrue(early.confident)
        before = ks.answer("What APY did the seventeen-month jumbo certificate special show?", as_of="2026-05-01")
        self.assertFalse(before.confident)
        self.assertTrue(no_new_numbers(before.text, "What APY did the seventeen-month jumbo certificate special show?"))

    def test_sparse_supplement_keeps_unfetched_pages(self):
        """Interior-only snapshots overlay URLs they fetched and keep the rest."""
        ks = build()
        ks.ingest(Snap("old", "2026-01-05", [
            Page("https://example-cu.test/rates/certificates", "Certificates", "6-month certificate APY: 2.95%\n"),
            Page("https://example-cu.test/fees", "Fees", "Lost debit card replacement fee: $2.50\n"),
        ]))
        ks.ingest(Snap("new", "2026-03-01", [
            Page("https://example-cu.test/rates/certificates", "Certificates", "6-month certificate APY: 3.50%\n"),
            Page("https://example-cu.test/promo", "Promo", "Summer special: 6-month certificate APY 2.95% (offer ended 2025-09-30)\n"),
        ]))
        current = ks.answer("What is the 6-month certificate APY?")
        self.assertIn("3.50%", current.text)
        self.assertNotIn("2.95%", current.text)
        para = ks.answer("Current APY on a six month share certificate?")
        self.assertIn("3.50%", para.text)
        historical = ks.answer("What is the 6-month certificate APY?", as_of="2026-01-31")
        self.assertIn("2.95%", historical.text)
        self.assertNotIn("3.50%", historical.text)
        fee_now = ks.answer("How much is a lost debit card replacement?")
        self.assertTrue(fee_now.confident)
        self.assertIn("2.50", fee_now.text)
        fee_then = ks.answer("How much is a lost debit card replacement?", as_of="2026-01-31")
        self.assertIn("2.50", fee_then.text)
        missing = ks.answer("What is the 60-month jumbo certificate rate?")
        self.assertFalse(missing.confident)
        self.assertTrue(no_new_numbers(missing.text, "What is the 60-month jumbo certificate rate?"))
        conflicts = ks.conflicts()
        self.assertTrue(any("3.50%" in " ".join(item.values) and "2.95%" in " ".join(item.values) for item in conflicts))
        self.assertTrue(any(item.resolution and "3.50%" in item.resolution for item in conflicts))

    def test_full_crawl_drops_pages_absent_from_later_snapshot(self):
        """A later snapshot that includes the site root is the whole site."""
        home = Page("https://example-cu.test/", "Home", "# Home\n\nWelcome.\n")
        ks = build()
        ks.ingest(Snap("old", "2026-01-05", [
            home,
            Page("https://example-cu.test/rates/certificates", "Certificates", "6-month certificate APY: 2.95%\n"),
            Page("https://example-cu.test/fees", "Fees", "Lost debit card replacement fee: $2.50\n"),
        ]))
        ks.ingest(Snap("new", "2026-03-01", [
            home,
            Page("https://example-cu.test/rates/certificates", "Certificates", "6-month certificate APY: 3.50%\n"),
            Page("https://example-cu.test/promo", "Promo", "Summer special: 6-month certificate APY 2.95% (offer ended 2025-09-30)\n"),
        ]))
        current = ks.answer("What is the 6-month certificate APY?")
        self.assertIn("3.50%", current.text)
        self.assertNotIn("2.95%", current.text)
        fee_now = ks.answer("How much is a lost debit card replacement?")
        self.assertFalse(fee_now.confident)
        self.assertNotIn("2.50", fee_now.text)
        fee_then = ks.answer("How much is a lost debit card replacement?", as_of="2026-01-31")
        self.assertIn("2.50", fee_then.text)

    def test_history_is_not_a_current_conflict_after_it_resolves(self):
        ks = build()
        ks.ingest(Snap("may", "2026-05-12", [Page(
            "https://example-cu.test/rates", "Rates",
            """
| Program | Rate | APR |
| --- | --- | --- |
| Refinance: 30-Year Fixed Conventional $100,000 or less | 5.875% | 6.022% |
| Refinance: 30-Year Fixed Conventional $100,000 or less | 6.000% | 6.148% |
""",
        )]))
        ks.ingest(Snap("sep", "2026-09-28", [Page(
            "https://example-cu.test/rates", "Rates",
            """
| Program | Rate | APR |
| --- | --- | --- |
| Refinance: 30-Year Fixed Conventional $100,000 or less | 7.13% | 7.28% |
| Refinance: 30-Year Fixed Conventional $100,000 or less | 7.13% | 7.28% |
""",
        )]))
        topics = " ".join(item.topic + " " + " ".join(item.values) for item in ks.conflicts())
        self.assertNotIn("5.875%", topics)
        self.assertNotIn("6.148%", topics)

    def test_conflict_listing_keeps_only_direct_competitors(self):
        ks = build()
        ks.ingest(Snap("old", "2026-01-05", [Page(
            "https://example-cu.test/rates",
            "Rates",
            """
| Loan Program | Term | Annual Percentage Rate (As Low As) |
| --- | --- | --- |
| 2024-27 Model Year | 60 Months | 4.00% APR |
""",
        )]))
        ks.ingest(Snap("now", "2026-09-28", [
            Page(
                "https://example-cu.test/rates",
                "Rates",
                """
Auto Loans

| Loan Program | Term | Annual Percentage Rate (As Low As) |
| --- | --- | --- |
| 2024-27 Model Year | 60 Months | 5.39% APR |
| 2017-20 Model Year | 60 Months | 6.34% APR |
""",
            ),
            Page(
                "https://example-cu.test/calculators/vehicle-affordability",
                "Calculator",
                """
| Loan Program | Term | Annual Percentage Rate (As Low As) |
| --- | --- | --- |
| 2024-26 Model Year | 60 Months | 5.34% APR |
| 2017-20 Model Year | 60 Months | 6.29% APR |
""",
            ),
            Page(
                "https://example-cu.test/rates/certificates",
                "Certificates",
                """
# Certificates

| Certificate | APY |
| --- | --- |
| 6-month certificate | 3.50% |

Rates are effective as of 09/01/2026.
""",
            ),
            Page(
                "https://example-cu.test/personal/savings-account",
                "Older certificate table",
                """
# Certificates

| Certificate | APY |
| --- | --- |
| 6-month certificate | 2.95% |

Rates are effective as of 01/05/2026.
""",
            ),
        ]))
        current = ks.answer("What is the 6-month certificate APY?")
        self.assertIn("3.50%", current.text)
        self.assertNotIn("2.95%", current.text)
        self.assertTrue(current.confident)
        blob = " ".join(" ".join(item.values) for item in ks.conflicts())
        # The January page is gone, so 4.00% is not a current competitor.
        self.assertNotIn("4.00%", blob)
        # The older effective date is not a second current certificate claim.
        self.assertNotIn("2.95%", blob)
        newer = older = None
        for item in ks.conflicts():
            vals = set(item.values)
            if "5.34%" in vals:
                newer = vals
            if "6.29%" in vals:
                older = vals
        self.assertIsNotNone(newer)
        self.assertIsNotNone(older)
        self.assertIn("5.39%", newer)
        self.assertNotIn("6.34%", newer)
        self.assertNotIn("6.29%", newer)
        self.assertIn("6.34%", older)
        self.assertNotIn("5.39%", older)
        self.assertNotIn("5.34%", older)
        # Same qualifier and the same effective date is a real current disagreement.
        same_day = build()
        table = """
# Certificates

| Certificate | APY |
| --- | --- |
| 6-month certificate | {value} |

Rates are effective as of 09/01/2026.
"""
        same_day.ingest(Snap("now", "2026-09-28", [
            Page("https://example-cu.test/rates/certificates", "Certificates", table.format(value="3.50%")),
            Page("https://example-cu.test/personal/savings-account", "Certificates", table.format(value="2.95%")),
        ]))
        listed = " ".join(" ".join(item.values) for item in same_day.conflicts())
        self.assertIn("3.50%", listed)
        self.assertIn("2.95%", listed)


class QualifierTests(unittest.TestCase):
    def setUp(self):
        self.ks = build()
        self.ks.ingest(Snap("now", "2026-09-28", [
            Page("https://example-cu.test/rates", "Rates", RATES),
            Page("https://example-cu.test/calculators/vehicle-affordability", "Calculator", CALC),
            Page("https://example-cu.test/homebuying-guidance", "Guidance", GUIDE),
            Page(
                "https://example-cu.test/newsroom/relief-ended",
                "Relief ended",
                "August 28, 2025\n\nWhile the special Mortgage Rate Relief program has ended, other loans remain.\n",
            ),
            Page(
                "https://example-cu.test/learn/blog/arm",
                "ARM",
                "A 30-year 7/1 adjustable-rate mortgage can reset after the first fixed period. No rate is quoted here.\n",
            ),
        ]))

    def test_footnote_does_not_create_extra_digits(self):
        answer = self.ks.answer("With the jumbo minimum, what is the seventeen-month special APY and required deposit?")
        self.assertIn("4.20%", answer.text)
        self.assertIn("100,000", answer.text)
        self.assertNotIn("4.201", answer.text)
        self.assertNotIn("1,000", answer.text.replace("100,000", "").replace("$100,000", ""))

    def test_dividend_is_not_apy(self):
        answer = self.ks.answer("What dividend rate, rather than APY, does the ordinary seventeen-month special pay?")
        self.assertIn("4.06%", answer.text)
        self.assertNotIn("4.10%", answer.text)
        self.assertNotIn("4.20%", answer.text)

    def test_model_year_beats_stale_calculator(self):
        newer = self.ks.answer("What's the APR for a 60-month auto loan on a 2025 model-year vehicle?")
        self.assertIn("5.39%", newer.text)
        self.assertNotIn("5.34%", newer.text)
        older = self.ks.answer("I have a 2023 car and want a sixty-month loan; what APR is listed?")
        self.assertIn("5.64%", older.text)
        self.assertNotIn("5.34%", older.text)
        self.assertNotIn("5.59%", older.text)
        six = self.ks.answer("For a 2025 car financed over six years, what APR is advertised?")
        self.assertIn("5.49%", six.text)
        self.assertNotIn("5.44%", six.text)

    def test_unsupported_term_does_not_borrow_another_product(self):
        answer = self.ks.answer("What APR is listed for a 96-month loan on my 2025 automobile?")
        self.assertFalse(answer.confident)
        self.assertNotIn("8.34%", answer.text)
        self.assertTrue(no_new_numbers(answer.text, "What APR is listed for a 96-month loan on my 2025 automobile?"))

    def test_amount_tier_and_purpose(self):
        both = self.ks.answer(
            "For a $100,000 purchase mortgage fixed for thirty years, give the interest rate and APR."
        )
        self.assertIn("7.13%", both.text)
        self.assertIn("7.27%", both.text)
        self.assertNotIn("7.25%", both.text)
        self.assertNotIn("7.32%", both.text)
        self.assertNotIn("5.750%", both.text)
        compare = self.ks.answer(
            "Compare current APRs for purchasing versus refinancing a $100,000 home loan fixed for thirty years."
        )
        self.assertIn("7.27%", compare.text)
        self.assertIn("7.28%", compare.text)
        edges = self.ks.answer("For 1st Rate Checking compare balances $4,999.99, $5,000 and $25,000.")
        self.assertIn("0.00%", edges.text)
        self.assertIn("2.50%", edges.text)
        self.assertIn("0.10%", edges.text)
        same = self.ks.answer("Does Share Savings yield the same at $24,999.99 and $25,000?")
        self.assertIn("0.15%", same.text)
        self.assertIn("0.20%", same.text)
        self.assertTrue(same.text.lower().startswith("no"))

    def test_promo_phases_stay_distinct(self):
        answer = self.ks.answer(
            "For $30,000 of qualifying money in Achieve Money Market, distinguish the introductory dividend rate, APY, and post-promo figures."
        )
        for token in ("4.00%", "3.66%", "3.20%", "3.25%"):
            self.assertIn(token, answer.text)

    def test_bright_measures(self):
        answer = self.ks.answer("Why does Bright Savings show two percentages? Give both the dividend rate and yield.")
        self.assertIn("2.23%", answer.text)
        self.assertIn("2.25%", answer.text)

    def test_arm_refusal_has_no_new_numbers(self):
        question = "Can you quote the current seven-one ARM interest rate?"
        answer = self.ks.answer(question)
        self.assertFalse(answer.confident)
        self.assertNotIn("7.13%", answer.text)
        self.assertNotIn("not offered", answer.text.lower())
        self.assertTrue(no_new_numbers(answer.text, question))

    def test_conflicts_are_intra_snapshot_and_scoped(self):
        conflicts = self.ks.conflicts()
        blob = "\n".join(
            f"{item.topic} | {item.values} | {item.resolution}" for item in conflicts
        )
        self.assertIn("5.39%", blob)
        self.assertIn("5.34%", blob)
        self.assertIn("5.750%", blob)
        self.assertIn("7.13%", blob)
        self.assertTrue(any(item.resolution and "ended" in item.resolution.lower() for item in conflicts))
        # Dividend versus APY on the same certificate is not a conflict.
        for item in conflicts:
            if "certificate" in item.topic and "4.06%" in item.values and "4.10%" in item.values:
                self.fail(item)
        self.assertLess(len(conflicts), 25)


if __name__ == "__main__":
    unittest.main()
