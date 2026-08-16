"""Critical path detection for security-sensitive files and functions."""

from __future__ import annotations

import re
from typing import Optional

from config import settings


class CriticalPathDetector:
    """Detect if a fix touches security-critical code paths."""

    def __init__(self):
        self.file_patterns = [re.compile(p, re.IGNORECASE) for p in settings.critical_path_patterns]
        self.function_patterns = [re.compile(p, re.IGNORECASE) for p in settings.critical_function_patterns]

    def is_critical_file(self, file_path: str) -> bool:
        """Check if a file path matches critical patterns."""
        return any(p.search(file_path) for p in self.file_patterns)

    def is_critical_function(self, func_name: str) -> bool:
        """Check if a function name matches critical patterns."""
        return any(p.search(func_name) for p in self.function_patterns)

    def analyze_fix(
        self,
        file_path: str,
        finding_severity: str,
        modified_functions: list[str],
        crosses_service_boundary: bool = False,
        is_auth_endpoint: bool = False,
    ) -> bool:
        """Determine if a fix is on the critical path."""
        if self.is_critical_file(file_path):
            return True
        if any(self.is_critical_function(f) for f in modified_functions):
            return True
        if finding_severity in ("critical", "high"):
            return True
        if crosses_service_boundary:
            return True
        if is_auth_endpoint:
            return True
        return False

    def get_reason(self, file_path: str, finding_severity: str, modified_functions: list[str]) -> str:
        """Get human-readable reason for critical path flag."""
        reasons = []
        if self.is_critical_file(file_path):
            reasons.append("touches security-related file")
        if any(self.is_critical_function(f) for f in modified_functions):
            reasons.append("modifies security-critical function")
        if finding_severity in ("critical", "high"):
            reasons.append(f"{finding_severity} severity finding")
        if not reasons:
            reasons.append("crosses service boundary or modifies auth endpoint")
        return ", ".join(reasons)
