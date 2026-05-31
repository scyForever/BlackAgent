"""Domain-specific error types."""


class BlackAgentDomainError(RuntimeError):
    """Base error for domain boundary failures."""


class ContractValidationError(BlackAgentDomainError):
    """Raised when a cross-layer payload cannot satisfy a domain contract."""


__all__ = ["BlackAgentDomainError", "ContractValidationError"]
