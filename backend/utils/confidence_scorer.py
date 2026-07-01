"""Confidence scoring with 5-signal weighted composite."""

from __future__ import annotations

from models import ConfidenceScore, ValidationResult
from config import settings


class ConfidenceScorer:
    """Compute confidence scores from validation signals."""

    # Signal weights (must sum to 1.0)
    WEIGHTS = {
        "tests": 0.40,
        "security_clean": 0.25,
        "ast_valid": 0.10,
        "cache_hit": 0.15,
        "fix_order": 0.10,
    }

    def compute(
        self,
        validation: ValidationResult,
        cache_hit: bool = False,
        is_first_fix: bool = False,
    ) -> ConfidenceScore:
        """Compute composite confidence score.

        When the repository has no runnable test suite, the strongest signal
        (tests, +40%) cannot be earned and the total is capped at 60% — matching
        the documented "static-analysis-only" fallback, which always requires
        human approval.
        """
        score = ConfidenceScore()

        # Gate 2: tests passed
        score.tests_signal = self.WEIGHTS["tests"] if validation.gate_2_tests_passed else 0.0

        # Gate 4: security clean
        score.security_clean_signal = self.WEIGHTS["security_clean"] if validation.gate_4_security_clean else 0.0

        # Gate 1: AST valid
        score.ast_valid_signal = self.WEIGHTS["ast_valid"] if validation.gate_1_ast_valid else 0.0

        # ChromaDB cache hit
        score.cache_hit_signal = self.WEIGHTS["cache_hit"] if cache_hit else 0.0

        # Fix ordering (first = lowest dependency risk)
        score.fix_order_signal = self.WEIGHTS["fix_order"] if is_first_fix else 0.0

        score.total_score = (
            score.tests_signal
            + score.security_clean_signal
            + score.ast_valid_signal
            + score.cache_hit_signal
            + score.fix_order_signal
        )

        # No test suite => cap at the documented static-only ceiling.
        if not validation.tests_available:
            score.total_score = min(score.total_score, settings.confidence_cap_no_tests)

        return score

    def is_auto_merge_eligible(self, score: ConfidenceScore) -> bool:
        """Check if score qualifies for auto-merge (>= 95%)."""
        return score.total_score >= settings.confidence_threshold_high

    def requires_approval(self, score: ConfidenceScore, is_critical_path: bool) -> bool:
        """Determine if human approval is required."""
        if is_critical_path:
            return True
        return score.total_score < settings.confidence_threshold_auto_merge
