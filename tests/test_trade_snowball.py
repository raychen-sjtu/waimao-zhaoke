import unittest

import trade_snowball


class TradeSnowballTests(unittest.TestCase):
    def test_product_match_rejects_cross_industry_collision(self):
        filters = {
            "include_keywords": ["picture frame", "frame"],
            "exclude_keywords": ["vehicle frame", "steel frame"],
        }
        match = trade_snowball.product_match(
            "PHOTO FRAME MOULDING FOR HOME DECOR", filters
        )
        collision = trade_snowball.product_match(
            "STEEL VEHICLE FRAME AND BRACKET", filters
        )
        self.assertTrue(match["matched"])
        self.assertFalse(collision["matched"])
        self.assertEqual(collision["reason"], "excluded_keyword")

    def test_merge_keeps_multiple_seed_sources(self):
        first = {
            "company_id": "buyer-1",
            "company_name": "Example Buyer LLC",
            "source_seeds": ["Seed A"],
            "trade_count": 4,
        }
        second = {
            "company_id": "buyer-1",
            "company_name": "Example Buyer LLC",
            "source_seeds": ["Seed B"],
            "trade_count": 7,
        }
        merged = trade_snowball.merge_leads([first, second])
        self.assertEqual(len(merged), 1)
        self.assertEqual(merged[0]["source_seeds"], ["Seed A", "Seed B"])
        self.assertEqual(merged[0]["trade_count"], 7)

    def test_classification_uses_evidence_thresholds(self):
        config = {
            "filters": {
                "include_keywords": ["picture frame"],
                "exclude_keywords": ["vehicle frame"],
                "min_trade_count": 2,
            },
            "pipeline": {
                "qualified_threshold": 65,
                "review_threshold": 35,
            },
        }
        lead = {
            "company_name": "Example Buyer LLC",
            "country": "US",
            "scope": "picture frame distributor",
            "product_desc": "PICTURE FRAME",
            "trade_count": 8,
            "latest_trade_date": "2026-06-01",
            "email_count": 1,
            "website_count": 1,
            "source_seeds": ["Seed A", "Seed B"],
        }
        scored = trade_snowball.score_lead(lead, config)
        self.assertEqual(scored["status"], "qualified")
        self.assertIn("product_match", scored["reason_codes"])
        self.assertIn("cross_seed_validation", scored["reason_codes"])


if __name__ == "__main__":
    unittest.main()

