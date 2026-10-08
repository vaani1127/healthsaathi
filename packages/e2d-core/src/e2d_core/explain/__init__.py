"""Explain-then-Detect: explanations of accesses from clinic workflow evidence."""

from e2d_core.explain.engine import explain
from e2d_core.explain.model import (
    AccessEvent,
    AppointmentEv,
    Candidate,
    EvidenceBundle,
    EvidenceQuery,
    EvidenceRef,
    Explanation,
    InvoiceEv,
    PatientEv,
    QueueTokenEv,
    ShiftEv,
)
from e2d_core.explain.templates import TemplateConfig, TemplateSpec, default_config

__all__ = [
    "AccessEvent",
    "AppointmentEv",
    "Candidate",
    "EvidenceBundle",
    "EvidenceQuery",
    "EvidenceRef",
    "Explanation",
    "InvoiceEv",
    "PatientEv",
    "QueueTokenEv",
    "ShiftEv",
    "TemplateConfig",
    "TemplateSpec",
    "default_config",
    "explain",
]
