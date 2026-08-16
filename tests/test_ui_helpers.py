import streamlit_app as ui


def test_repository_url_validation():
    assert ui._is_github_repo_url("https://github.com/acme/payments")
    assert ui._is_github_repo_url("https://github.com/acme/payments.git")
    assert not ui._is_github_repo_url("https://example.com/acme/payments")
    assert not ui._is_github_repo_url("github.com/acme/payments")


def test_distribution_and_health_components_escape_content():
    distribution = ui._distribution_html(
        {"high<script>": 2, "low": 1}, {"low": "#123456"},
    )
    health = ui._health_html([("Tests<script>", "Passed", "pass")])

    assert "<script>" not in distribution
    assert "High&lt;Script&gt;" in distribution
    assert "Tests&lt;script&gt;" in health
    assert "health pass" in health
