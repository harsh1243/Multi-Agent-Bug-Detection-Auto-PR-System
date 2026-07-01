"""Application configuration using Pydantic BaseSettings."""

import os
from pathlib import Path
from pydantic_settings import BaseSettings
from pydantic import Field


class Settings(BaseSettings):
    """Application settings loaded from environment variables."""

    # API Keys
    anthropic_api_key: str = Field(..., description="Anthropic API key for Claude")
    anthropic_base_url: str | None = Field(
        None,
        description="Anthropic-compatible base URL (e.g. a third-party credits proxy like "
                    "Lightning AI). Leave unset to use the official api.anthropic.com.",
    )
    github_token: str | None = Field(None, description="GitHub personal access token")

    # Server
    host: str = "0.0.0.0"
    port: int = 8000
    debug: bool = False

    # LLM Configuration (Anthropic SDK model IDs — bare form, no provider prefix)
    claude_model_primary: str = "claude-sonnet-4-6"          # reasoning, code-gen, confirmation
    claude_model_triage: str = "claude-haiku-4-5-20251001"   # cheap triage / structured tasks
    llm_max_retries: int = 4
    llm_retry_base_delay: float = 1.0
    llm_timeout: int = 90
    llm_token_budget_code_gen: int = 8192
    llm_token_budget_investigation: int = 4096
    llm_token_budget_routing: int = 1024
    llm_token_budget_hunt: int = 4096        # per-file bug-hunt output budget
    # Newer models (e.g. claude-opus-4-8 via Lightning) deprecate `temperature`
    # and reject requests that include it. Keep this False for those models.
    llm_use_temperature: bool = False

    # ── LLM Bug Hunter (real discovery; scanners often unavailable on Windows) ──
    bug_hunter_enabled: bool = True
    bug_hunter_max_files: int = 0            # 0 = no cap (scan every source file)
    bug_hunter_max_file_bytes: int = 60_000  # skip files larger than this (chunk-free safety)
    bug_hunter_languages: list[str] = ["python", "javascript", "typescript", "jsx", "tsx"]
    bug_hunter_delay_seconds: float = 1.0    # pause between files to respect rate limits

    # ── Fix gating: which findings become PRs vs report-only ──
    auto_fix_issue_classes: list[str] = [
        "security_vulnerability", "functional_bug", "performance",
    ]
    report_only_issue_classes: list[str] = ["code_quality"]
    fix_code_quality: bool = False           # if True, code-quality findings are also fixed
    min_severity_to_fix: str = "low"         # one of: info, low, medium, high, critical
    max_files_to_fix: int = 0                # 0 = no cap on number of PRs per run

    # Paths
    repo_clone_dir: Path = Path("/tmp/repos")
    chroma_db_path: str = "./chroma_db"
    knowledge_graph_dir: Path = Path("./kg_cache")

    # ChromaDB
    chroma_collection_name: str = "repo_fixes"
    chroma_similarity_threshold: float = 0.85

    # Confidence Scoring
    confidence_threshold_auto_merge: float = 0.70
    confidence_threshold_high: float = 0.95
    confidence_cap_no_tests: float = 0.60

    # Validation
    validation_max_retries: int = 3
    validation_sandbox_timeout: int = 120
    blast_radius_default_hops: int = 2

    # Critical Path Detection
    critical_path_patterns: list[str] = [
        ".*auth.*\\.py", ".*jwt.*", ".*crypto.*",
        ".*password.*", ".*login.*", ".*session.*", ".*oauth.*"
    ]
    critical_function_patterns: list[str] = [
        ".*authenticate.*", ".*authorize.*", ".*hash.*",
        ".*encrypt.*", ".*sign.*"
    ]
    approval_timeout_hours: int = 24

    # Frontend URL for CORS
    frontend_url: str = "http://localhost:5173"

    class Config:
        env_file = ".env"
        env_file_encoding = "utf-8"


# Global settings instance
settings = Settings()

# Ensure directories exist
settings.repo_clone_dir.mkdir(parents=True, exist_ok=True)
settings.knowledge_graph_dir.mkdir(parents=True, exist_ok=True)
