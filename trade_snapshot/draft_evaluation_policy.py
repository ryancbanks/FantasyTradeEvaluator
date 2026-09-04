"""Compatibility identity for Draft Lab training evaluation semantics."""


EVALUATION_POLICY_VERSION = 2
LEGACY_EVALUATION_POLICY_NOTICE = (
    "This artifact predates full snake-draft seat sweeps and cannot be used. "
    "Retrain it so every brain is evaluated from every draft position."
)


def is_current_evaluation_policy(value: object) -> bool:
    return type(value) is int and value == EVALUATION_POLICY_VERSION


__all__ = (
    "EVALUATION_POLICY_VERSION",
    "LEGACY_EVALUATION_POLICY_NOTICE",
    "is_current_evaluation_policy",
)
