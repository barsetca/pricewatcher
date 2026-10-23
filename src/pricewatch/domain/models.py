from __future__ import annotations
import sqlite3
from dataclasses import dataclass
from typing import Optional
from pricewatch.domain.enums import AnomalyDecision, ExtractionMethod, ExtractionStatus, FetchMethod, FetchStatus, NotificationEventType, NotificationOutboxStatus, OfferStatus, SchedulerRunStatus
@dataclass(frozen=True)
class User:
    telegram_user_id:int; created_at:str; username:Optional[str]=None; alert_threshold:float=10.0; id:Optional[int]=None
    @classmethod
    def from_row(cls,row:sqlite3.Row): return cls(**dict(row))
@dataclass(frozen=True)
class Product:
    user_id:int; name:str; created_at:str; alert_threshold:Optional[float]=None; is_active:bool=True; id:Optional[int]=None
    @classmethod
    def from_row(cls,row):
        data=dict(row); data["is_active"]=bool(data["is_active"]); return cls(**data)
@dataclass(frozen=True)
class Offer:
    product_id:int; url:str; normalized_url:str; created_at:str; domain:Optional[str]=None; store_name:Optional[str]=None; source_title:Optional[str]=None; current_price_minor:Optional[int]=None; currency:Optional[str]=None; preferred_fetch_method:Optional[FetchMethod]=None; status:OfferStatus=OfferStatus.PENDING; consecutive_errors:int=0; error_notified:bool=False; pending_price_minor:Optional[int]=None; pending_price_detected_at:Optional[str]=None; pending_check_attempt_id:Optional[int]=None; last_checked_at:Optional[str]=None; last_success_at:Optional[str]=None; is_active:bool=True; state_version:int=0; id:Optional[int]=None
    @classmethod
    def from_row(cls,row):
        data=dict(row); data["status"]=OfferStatus(data["status"]); data["is_active"]=bool(data["is_active"]); data["error_notified"]=bool(data["error_notified"])
        if data["preferred_fetch_method"]: data["preferred_fetch_method"]=FetchMethod(data["preferred_fetch_method"])
        return cls(**data)
@dataclass(frozen=True)
class PriceRecord:
    offer_id:int; price_minor:int; currency:str; checked_at:str; id:Optional[int]=None
    @classmethod
    def from_row(cls,row): return cls(**dict(row))
@dataclass(frozen=True)
class CheckAttempt:
    offer_id:int; checked_at:str; fetch_status:FetchStatus; extraction_status:ExtractionStatus; fetch_method:Optional[FetchMethod]=None; http_status:Optional[int]=None; extraction_method:Optional[ExtractionMethod]=None; confidence_score:Optional[float]=None; detected_price_minor:Optional[int]=None; currency:Optional[str]=None; elapsed_time_ms:Optional[int]=None; error_message:Optional[str]=None; is_suspicious:bool=False; anomaly_decision:Optional[AnomalyDecision]=None; anomaly_decided_at:Optional[str]=None; id:Optional[int]=None
    @classmethod
    def from_row(cls,row):
        data=dict(row); data["fetch_status"]=FetchStatus(data["fetch_status"]); data["extraction_status"]=ExtractionStatus(data["extraction_status"]); data["is_suspicious"]=bool(data["is_suspicious"])
        for key,enum in (("fetch_method",FetchMethod),("extraction_method",ExtractionMethod),("anomaly_decision",AnomalyDecision)):
            if data[key]: data[key]=enum(data[key])
        return cls(**data)
@dataclass(frozen=True)
class NotificationOutbox:
    event_key:str; event_type:NotificationEventType; chat_id:int; payload_json:str; next_attempt_at:str; created_at:str
    offer_id:Optional[int]=None; status:NotificationOutboxStatus=NotificationOutboxStatus.PENDING; attempt_count:int=0
    lease_until:Optional[str]=None; lease_token:Optional[str]=None; sent_at:Optional[str]=None
    last_error:Optional[str]=None; telegram_message_id:Optional[int]=None; id:Optional[int]=None
    @classmethod
    def from_row(cls,row):
        data=dict(row); data["event_type"]=NotificationEventType(data["event_type"]); data["status"]=NotificationOutboxStatus(data["status"])
        return cls(**data)
@dataclass(frozen=True)
class SchedulerRun:
    job_name:str; scheduled_for:str; started_at:str; status:SchedulerRunStatus
    attempt_count:int=1; finished_at:Optional[str]=None; error_message:Optional[str]=None; id:Optional[int]=None
    @classmethod
    def from_row(cls,row):
        data=dict(row); data["status"]=SchedulerRunStatus(data["status"]); return cls(**data)
