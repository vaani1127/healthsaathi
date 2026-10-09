from enum import StrEnum


class Role(StrEnum):
    PLATFORM_ADMIN = "platform_admin"
    CLINIC_ADMIN = "clinic_admin"
    RECEPTION = "reception"
    NURSE = "nurse"
    DOCTOR = "doctor"
    LAB_TECH = "lab_tech"
    PATIENT = "patient"


STAFF_ROLES = frozenset(
    {
        Role.PLATFORM_ADMIN,
        Role.CLINIC_ADMIN,
        Role.RECEPTION,
        Role.NURSE,
        Role.DOCTOR,
        Role.LAB_TECH,
    }
)


class Sex(StrEnum):
    FEMALE = "female"
    MALE = "male"
    OTHER = "other"
    UNKNOWN = "unknown"


class RecordStatus(StrEnum):
    ACTIVE = "active"
    INACTIVE = "inactive"


class ShiftStatus(StrEnum):
    SCHEDULED = "scheduled"
    CANCELLED = "cancelled"


class AppointmentKind(StrEnum):
    NEW = "new"
    FOLLOWUP = "followup"
    WALKIN = "walkin"


class AppointmentStatus(StrEnum):
    BOOKED = "booked"
    CHECKED_IN = "checked_in"
    IN_CONSULT = "in_consult"
    COMPLETED = "completed"
    CANCELLED = "cancelled"
    NO_SHOW = "no_show"


class AppointmentSource(StrEnum):
    RECEPTION = "reception"
    PATIENT = "patient"
    DOCTOR = "doctor"


class QueueStatus(StrEnum):
    WAITING = "waiting"
    WITH_NURSE = "with_nurse"
    WITH_DOCTOR = "with_doctor"
    DONE = "done"
    SKIPPED = "skipped"


class EncounterStatus(StrEnum):
    OPEN = "open"
    CLOSED = "closed"


class ReferralStatus(StrEnum):
    ACTIVE = "active"
    COMPLETED = "completed"
    CANCELLED = "cancelled"


class AllergySeverity(StrEnum):
    MILD = "mild"
    MODERATE = "moderate"
    SEVERE = "severe"


class ConditionStatus(StrEnum):
    ACTIVE = "active"
    RESOLVED = "resolved"
    INACTIVE = "inactive"


class LabOrderStatus(StrEnum):
    ORDERED = "ordered"
    COLLECTED = "collected"
    RESULTED = "resulted"
    CANCELLED = "cancelled"


class InvoiceStatus(StrEnum):
    DRAFT = "draft"
    ISSUED = "issued"
    PAID = "paid"
    VOID = "void"


class PaymentMethod(StrEnum):
    CASH = "cash"
    UPI = "upi"
    CARD_OFFLINE = "card_offline"


class ConsentChannel(StrEnum):
    RECEPTION = "reception"
    PATIENT_PORTAL = "patient_portal"


class ConsentEventKind(StrEnum):
    GRANTED = "granted"
    WITHDRAWN = "withdrawn"


class ResourceType(StrEnum):
    DEMOGRAPHICS = "demographics"
    VITALS = "vitals"
    ALLERGIES = "allergies"
    NOTES = "notes"
    PRESCRIPTIONS = "prescriptions"
    LAB = "lab"
    BILLING = "billing"
    DOCUMENTS = "documents"
    CONSENT = "consent"


class AccessAction(StrEnum):
    VIEW = "view"
    CREATE = "create"
    EDIT = "edit"
    EXPORT = "export"
    PRINT = "print"


class AccessDecision(StrEnum):
    ALLOW = "allow"
    DENY = "deny"


class AlertStatus(StrEnum):
    OPEN = "open"
    BENIGN = "benign"
    MISUSE = "misuse"
    UNSURE = "unsure"


class ReviewOutcome(StrEnum):
    BENIGN = "benign"
    MISUSE = "misuse"
    UNSURE = "unsure"


class QueryStatus(StrEnum):
    OPEN = "open"
    ANSWERED = "answered"
    CLOSED = "closed"


class AnchorBackend(StrEnum):
    AMOY = "amoy"
    OTS = "ots"
    GITHUB = "github"
    BESU = "besu"


class AnchorStatus(StrEnum):
    PENDING = "pending"
    CONFIRMED = "confirmed"
    FAILED = "failed"


class StatusEntity(StrEnum):
    """Records whose status history is kept in status_events."""

    APPOINTMENT = "appointment"
    LAB_ORDER = "lab_order"
    REFERRAL = "referral"
    INVOICE = "invoice"
