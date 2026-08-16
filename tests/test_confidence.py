from models import ValidationResult
from utils.confidence_scorer import ConfidenceScorer


def test_unavailable_security_scanner_does_not_earn_clean_signal():
    validation = ValidationResult(
        gate_1_ast_valid=True,
        gate_2_tests_passed=True,
        gate_4_security_clean=True,
        tests_available=True,
        test_runner_available=True,
        security_scanner_available=False,
        passed=True,
    )

    score = ConfidenceScorer().compute(validation, cache_hit=False, is_first_fix=True)

    assert score.tests_signal == 0.40
    assert score.ast_valid_signal == 0.10
    assert score.security_clean_signal == 0.0
    assert score.total_score == 0.60
    assert not score.security_scanner_available


def test_no_test_suite_remains_capped():
    validation = ValidationResult(
        gate_1_ast_valid=True,
        gate_4_security_clean=True,
        tests_available=False,
        test_runner_available=True,
        security_scanner_available=True,
        passed=True,
    )

    score = ConfidenceScorer().compute(validation, cache_hit=True, is_first_fix=True)

    assert score.tests_signal == 0.0
    assert score.total_score <= 0.60
