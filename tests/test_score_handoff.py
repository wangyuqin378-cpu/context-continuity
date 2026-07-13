from __future__ import annotations

import sys
import unittest
from pathlib import Path


EVALS = Path(__file__).parents[1] / "evals"
if str(EVALS) not in sys.path:
    sys.path.insert(0, str(EVALS))

from score_handoff import has_fact_anchor, token_normalize  # noqa: E402


class ScoreHandoffNormalizationTests(unittest.TestCase):
    def test_fact_anchor_normalizes_punctuation_without_changing_tokens(self) -> None:
        text = "O001 Out of scope: Admin-dashboard and billing-flow redesign."
        folded = text.casefold()
        normalized = token_normalize(text)

        self.assertTrue(
            has_fact_anchor(folded, normalized, "admin dashboard")
        )
        self.assertTrue(has_fact_anchor(folded, normalized, "billing"))
        self.assertFalse(
            has_fact_anchor(folded, normalized, "admin reporting")
        )


if __name__ == "__main__":
    unittest.main()
