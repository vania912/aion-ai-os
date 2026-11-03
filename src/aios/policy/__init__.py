"""Policy engine: what may be read, and which operations may run at all.

Every interface calls into this package; individual tools do not implement their own checks.
"""

from aios.policy.files import FileAccessPolicy, PathGuard, looks_binary
from aios.policy.operations import Decision, OperationPolicy, PermissionLevel, Verdict

__all__ = [
    "Decision",
    "FileAccessPolicy",
    "OperationPolicy",
    "PathGuard",
    "PermissionLevel",
    "Verdict",
    "looks_binary",
]
