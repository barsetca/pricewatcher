from enum import Enum
class _TextEnum(str, Enum): pass
class FetchStatus(_TextEnum):
    SUCCESS="SUCCESS"; BLOCKED="BLOCKED"; ROBOTS_DENIED="ROBOTS_DENIED"; HTTP_ERROR="HTTP_ERROR"; TIMEOUT="TIMEOUT"; NETWORK_ERROR="NETWORK_ERROR"
class ExtractionStatus(_TextEnum):
    SUCCESS="SUCCESS"; PARTIAL="PARTIAL"; AMBIGUOUS="AMBIGUOUS"; PARSE_ERROR="PARSE_ERROR"; NOT_RUN="NOT_RUN"
class OfferStatus(_TextEnum):
    ACTIVE="ACTIVE"; PENDING="PENDING"; ERROR="ERROR"; SUSPICIOUS="SUSPICIOUS"; DISABLED="DISABLED"
class FetchMethod(_TextEnum): REQUESTS="REQUESTS"; PLAYWRIGHT="PLAYWRIGHT"
class ExtractionMethod(_TextEnum): JSON_LD="JSON_LD"; MICRODATA="MICRODATA"; METADATA="METADATA"; DOM_HEURISTIC="DOM_HEURISTIC"; NONE="NONE"
class AnomalyDecision(_TextEnum): CONFIRMED="CONFIRMED"; REJECTED="REJECTED"
class NotificationEventType(_TextEnum): PRICE_DROP="PRICE_DROP"; ANOMALY_DETECTED="ANOMALY_DETECTED"; REPEATED_ERROR="REPEATED_ERROR"
class NotificationOutboxStatus(_TextEnum): PENDING="PENDING"; SENDING="SENDING"; SENT="SENT"; FAILED="FAILED"
class SchedulerRunStatus(_TextEnum): RUNNING="RUNNING"; SUCCESS="SUCCESS"; FAILED="FAILED"; INTERRUPTED="INTERRUPTED"

# Короткие имена оставлены для repository/API очереди.
OutboxEventType = NotificationEventType
OutboxStatus = NotificationOutboxStatus
