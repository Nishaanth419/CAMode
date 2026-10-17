"""Synthetic INDmoney mutual fund responses with the real field layout and made-up schemes and values.

The layout is ``get_mf_by_category`` and ``get_mf_funds_details`` as recorded on 2026-10-16
with ``python -m scripts.inspect_indmoney_shapes --funds --labels``: dates are written
"15 Oct 2026", category text is the slug with spaces ("flexi cap"), risk reads
"Very High Risk", and the details response has no date anywhere. Includes the traps:
a 5-year return of 0 for a scheme with no figure, a scheme younger than 3 years, a
row without a risk label, and a row INDmoney files under a category CA Mode does not map.
"""

from __future__ import annotations

import json

NAV_DATE = "15 Oct 2026"


def listing_row(scheme_id, name, category, *, risk="Very High Risk", expense=0.62, r1=14.2, r3=17.85, r5=21.3,
                nav_date=NAV_DATE, launch_date="", ranking_category="Equity", **extra):
    row = {
        "id": scheme_id, "name": name, "aum": "₹12,450 Cr", "return_1yr": r1, "return_3yr": r3, "return_5yr": r5,
        "expense_ratio": expense, "expense": f"{expense}%" if expense is not None else "", "return": f"{r3}%",
        "risk": risk, "nav": 88.4312, "nav_date": nav_date, "yesterday_nav": 88.1104,
        "yesterday_nav_date": "14 Oct 2026", "ind_category_rank": "1",
        "nfo_start_date": "", "nfo_end_date": "", "launch_date": launch_date, "rating": 5,
        "ind_ranking": {"ranking_sub_category": category, "ranking_category": ranking_category, "rank": "1",
                        "total_score": 81.2, "percentile_rank": 97.5},
        "fund_type": "Equity", "purchase_allowed": True, "sip_allowed": True, "min_sip": 100, "max_sip": 99999999,
        "min_lumpsum": 1000, "max_lumpsum": 99999999, "sip_multiplier": 1, "category": category, "inception": 15.1,
    }
    for key, value in extra.items():
        if value is _DROP:
            row.pop(key, None)
        else:
            row[key] = value
    return row


_DROP = object()
DROP = _DROP


def listing(rows, *, count=None):
    return json.dumps({
        "error": "", "success": True, "filter_count": len(rows), "count": count or len(rows), "next_page": False,
        "empty_response": {}, "data": rows,
    })


def details(benchmarks):
    """``get_mf_funds_details`` for {scheme id: benchmark name or None}."""
    data = []
    for scheme_id, benchmark in benchmarks.items():
        detail = {
            "SipMaxInstallmentAmount": 99999999, "SipMinInstallmentAmount": 100, "aum": 124500000000,
            "bseMaximumPurchaseAmount": 99999999, "bseMinimumPurchaseAmount": 1000, "category": "flexi cap",
            "expense_ratio": 0.62, "fund_app_short_name": "Sample", "image_url": "https://example.invalid/logo.png",
            "ind_ranking": {"cost_rank": 3, "cost_rank_position": "3/40", "cost_score": 0.8, "percentile_rank": 97.5,
                            "perf_rank": 2, "perf_rank_position": "2/40", "performance_score": 0.9, "rank": "1",
                            "ranking_category": "Equity", "ranking_sub_category": "flexi cap", "risk_rank": 5,
                            "risk_rank_position": "5/40", "risk_score": 0.31, "tag": "Top", "total_score": 81.2},
            "name": "Sample", "nav": 88.4312,
            "returns": {"1D": 0.2, "1M": 1.1, "1Y": 14.2, "3M": 3.4, "3Y": 17.85, "5Y": 21.3, "6M": 6.6, "MAX": 15.1,
                        "cumulative_five_year_return": 162.4, "cumulative_three_year_return": 63.7},
            "strategy": "Invests across market caps.",
        }
        if benchmark is not None:
            detail["benchmark_name"] = benchmark
        data.append({"fund_id": scheme_id, "data": {"fund_detail": detail}})
    return json.dumps({"success": True, "data": data})


# Large/flexi equity: two clean schemes, one young scheme with a 0 5Y return, one without risk, one filed elsewhere.
FLEXI_ROWS = [
    listing_row(2101, "Zeta Flexi Cap Fund Direct Growth", "flexi cap"),
    listing_row(2102, "Alpha Large Cap Fund Direct Growth", "large cap", expense=0.48, r1=11.05, r3=15.4, r5=18.92),
    listing_row(2103, "Nova Flexi Cap Fund Direct Growth", "flexi cap", r1=9.6, r3=0, r5=0, launch_date="12 June 2024"),
    listing_row(2104, "Beta Nifty 50 Index Fund Direct Growth", "nifty 50 index funds", risk="", expense=0.2,
                r1=10.4, r3=13.1, r5=0),
    listing_row(2105, "Gamma Aggressive Hybrid Fund Direct Growth", "aggressive allocation"),
]
FLEXI_BENCHMARKS = {2101: "NIFTY 500 TRI", 2102: "NIFTY 100 TRI", 2103: "NIFTY 500 TRI", 2104: None}

LIQUID_ROWS = [
    listing_row(1302, "Omega Liquid Fund Direct Growth", "liquid", risk="Low to Moderate Risk", expense=0.2,
                r1=7.12, r3=6.81, r5=5.44, ranking_category="Debt"),
    listing_row(1303, "Delta Overnight Fund Direct Growth", "overnight", risk="Low Risk", expense=0.08,
                r1=6.55, r3=6.3, r5=4.9, ranking_category="Debt"),
]
LIQUID_BENCHMARKS = {1302: "CRISIL Liquid Debt A-I TR INR", 1303: "CRISIL Liquid Overnight Index"}
