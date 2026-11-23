"""sidefx: Outbound effects journal and safety layer for AI agents."""

from sidefx.config import (
    ConfigError,
    PreflightConfig,
    PreflightRule,
    SidefxConfig,
    ToolClassificationConfig,
    UnknownToolPolicy,
    default_config_dict,
    discover_config_path,
    load_config,
)
from sidefx.journal import (
    ActionNotFoundError,
    ActionRecord,
    ActionStatus,
    BeginDecision,
    BeginResult,
    InvalidStateTransitionError,
    Journal,
    JournalError,
    PayloadMismatchError,
    Surface,
    compute_payload_hash,
    derive_marker,
    resolve_db_path,
)

__version__ = "0.1.0"

__all__ = [
    "ActionNotFoundError",
    "ActionRecord",
    "ActionStatus",
    "BeginDecision",
    "BeginResult",
    "ConfigError",
    "InvalidStateTransitionError",
    "Journal",
    "JournalError",
    "PayloadMismatchError",
    "PreflightConfig",
    "PreflightRule",
    "SidefxConfig",
    "Surface",
    "ToolClassificationConfig",
    "UnknownToolPolicy",
    "__version__",
    "compute_payload_hash",
    "default_config_dict",
    "derive_marker",
    "discover_config_path",
    "load_config",
    "resolve_db_path",
]
