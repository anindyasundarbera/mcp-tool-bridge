"""Project-aware local development MCP bridge."""

__version__ = "0.11.0"

from .application import GovernanceApplication, RequestContext  # noqa: F401
from .authorization import AuthorizationManager  # noqa: F401
from .bootstrap import (  # noqa: F401
    BootstrapConflictError,
    BootstrapReport,
    init_conrad_installation,
    rollback_conrad_config,
)
from .claude import ClaudeExecutorAdapter  # noqa: F401
from .cli import CLIExitCode, CLIResponse, run_cli  # noqa: F401
from .containment import (  # noqa: F401
    ContainedProcessRunner,
    ContainmentDoctorReport,
    ContainmentMode,
    ContainmentPolicy,
)
from .doctor import (  # noqa: F401
    DoctorCheckResult,
    DoctorReport,
    DoctorSeverity,
    run_conrad_doctor,
)
from .errors import ConradDomainError, ErrorFamily  # noqa: F401
from .evidence import EvidenceBundleManager  # noqa: F401
from .executor_contract import (  # noqa: F401
    DeterministicFakeExecutor,
    ExecutionRequest,
    ExecutionResult,
    ExecutorCapabilities,
    ExecutorCheckpoint,
    ExecutorHealthStatus,
    ExecutorLifecycleState,
    ExecutorPort,
    ExecutorRegistry,
    ExecutorResourceLimits,
    NetworkMode,
)
from .identity import (  # noqa: F401
    AuthProvenance,
    CallerPrincipal,
    PrincipalRole,
    resolve_caller_principal,
)
from .onboarding import (  # noqa: F401
    OnboardingAcceptanceReport,
    run_onboarding_acceptance,
)
from .openhands_executor import (  # noqa: F401
    PINNED_OPENHANDS_IMAGE,
    OpenHandsAdapterConfig,
    OpenHandsExecutor,
)
from .pr import PullRequestManager  # noqa: F401
from .profiles import (  # noqa: F401
    ProfileName,
    RepositoryTrustMode,
    expand_capability_profile,
    explain_capability_profile,
    list_capability_profiles,
)
from .project_registration import (  # noqa: F401
    register_project,
    show_project,
    validate_projects_config,
)
from .requirement import RequirementManager  # noqa: F401
from .review import (  # noqa: F401
    ApprovalSummaryDTO,
    PromotionAuthorityTrace,
    ReviewDecision,
)
from .schema_compat import (  # noqa: F401
    PUBLIC_TOOL_CATALOG,
    ToolCatalogEntry,
    ToolLifecycleStage,
)
from .service_lifecycle import (  # noqa: F401
    ServiceInstanceRecord,
    ServiceLifecycleManager,
    ServiceState,
    ServiceStatusReport,
)
from .transports import (  # noqa: F401
    LocalHttpTransportAdapter,
    NgrokMtlsTransportAdapter,
    PeerTransportMetadata,
    StdioTransportAdapter,
    TransportCapabilities,
    TransportConfig,
    TransportMode,
)

__all__ = [
    "__version__",
    "ApprovalSummaryDTO",
    "AuthProvenance",
    "AuthorizationManager",
    "BootstrapConflictError",
    "BootstrapReport",
    "CLIExitCode",
    "CLIResponse",
    "CallerPrincipal",
    "ClaudeExecutorAdapter",
    "ConradDomainError",
    "ContainedProcessRunner",
    "ContainmentDoctorReport",
    "ContainmentMode",
    "ContainmentPolicy",
    "DeterministicFakeExecutor",
    "DoctorCheckResult",
    "DoctorReport",
    "DoctorSeverity",
    "ErrorFamily",
    "EvidenceBundleManager",
    "ExecutionRequest",
    "ExecutionResult",
    "ExecutorCapabilities",
    "ExecutorCheckpoint",
    "ExecutorHealthStatus",
    "ExecutorLifecycleState",
    "ExecutorPort",
    "ExecutorRegistry",
    "ExecutorResourceLimits",
    "GovernanceApplication",
    "LocalHttpTransportAdapter",
    "NetworkMode",
    "NgrokMtlsTransportAdapter",
    "OnboardingAcceptanceReport",
    "OpenHandsAdapterConfig",
    "OpenHandsExecutor",
    "PINNED_OPENHANDS_IMAGE",
    "PUBLIC_TOOL_CATALOG",
    "PeerTransportMetadata",
    "PrincipalRole",
    "ProfileName",
    "PromotionAuthorityTrace",
    "PullRequestManager",
    "RequestContext",
    "RepositoryTrustMode",
    "RequirementManager",
    "ReviewDecision",
    "ServiceInstanceRecord",
    "ServiceLifecycleManager",
    "ServiceState",
    "ServiceStatusReport",
    "StdioTransportAdapter",
    "ToolCatalogEntry",
    "ToolLifecycleStage",
    "TransportCapabilities",
    "TransportConfig",
    "TransportMode",
    "expand_capability_profile",
    "explain_capability_profile",
    "init_conrad_installation",
    "list_capability_profiles",
    "register_project",
    "resolve_caller_principal",
    "rollback_conrad_config",
    "run_cli",
    "run_conrad_doctor",
    "run_onboarding_acceptance",
    "show_project",
    "validate_projects_config",
]
