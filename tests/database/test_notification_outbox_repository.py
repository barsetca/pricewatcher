import sqlite3

import pytest

from pricewatch.database import connect, initialize_database
from pricewatch.database.repositories.notification_outbox_repository import NotificationOutboxRepository
from pricewatch.domain.enums import NotificationEventType, NotificationOutboxStatus
from pricewatch.domain.models import NotificationOutbox


NOW = "2026-10-20T10:00:00+00:00"
LATER = "2026-10-20T10:05:00+00:00"


@pytest.fixture
def db(tmp_path):
    path = tmp_path / "outbox.sqlite"
    initialize_database(path)
    connection = connect(path)
    yield connection
    connection.close()


def event(key="price-drop-1", chat_id=100, *, next_attempt_at=NOW, created_at=NOW, offer_id=None, payload_json='{"product":"Example"}'):
    return NotificationOutbox(
        event_key=key,
        event_type=NotificationEventType.PRICE_DROP,
        offer_id=offer_id,
        chat_id=chat_id,
        payload_json=payload_json,
        next_attempt_at=next_attempt_at,
        created_at=created_at,
    )


def test_enqueue_is_idempotent_per_event_key_and_chat_id(db):
    repository = NotificationOutboxRepository(db)
    first = repository.enqueue(event())
    duplicate = repository.enqueue(event(payload_json='{"changed":true}'))
    separate_recipient = repository.enqueue(event(chat_id=101))

    assert first.id == duplicate.id
    assert duplicate.payload_json == '{"product":"Example"}'
    assert separate_recipient.id != first.id
    assert (first.status, first.attempt_count) == (NotificationOutboxStatus.PENDING, 0)


def test_ready_events_are_not_blocked_by_delayed_older_event_and_are_chronological(db):
    repository = NotificationOutboxRepository(db)
    delayed = repository.enqueue(event("delayed", next_attempt_at=LATER, created_at="2026-10-20T09:00:00+00:00"))
    second = repository.enqueue(event("second", next_attempt_at=NOW, created_at="2026-10-20T09:01:00+00:00"))
    third = repository.enqueue(event("third", next_attempt_at=NOW, created_at="2026-10-20T09:02:00+00:00"))

    assert [item.id for item in repository.list_ready(NOW)] == [second.id, third.id]
    assert repository.get_by_id(delayed.id).status is NotificationOutboxStatus.PENDING


def test_claim_is_atomic_and_only_current_lease_can_finish(db):
    repository = NotificationOutboxRepository(db)
    queued = repository.enqueue(event())
    claimed = repository.claim(queued.id, NOW, LATER)

    assert claimed is not None
    assert claimed.status is NotificationOutboxStatus.SENDING
    assert claimed.attempt_count == 1
    assert claimed.lease_token
    assert repository.claim(queued.id, NOW, LATER) is None
    assert not repository.mark_sent(queued.id, "wrong-token", LATER, 10)
    assert repository.mark_sent(queued.id, claimed.lease_token, LATER, 10)
    sent = repository.get_by_id(queued.id)
    assert (sent.status, sent.sent_at, sent.telegram_message_id, sent.lease_until) == (
        NotificationOutboxStatus.SENT, LATER, 10, None,
    )
    assert repository.list_ready("2026-10-20T11:00:00+00:00") == []


def test_lookup_by_telegram_message_accepts_only_sent_record(db):
    repository = NotificationOutboxRepository(db)
    queued = repository.enqueue(event())
    assert repository.get_sent_by_telegram_message(100, 77) is None
    claimed = repository.claim(queued.id, NOW, LATER)
    assert repository.get_sent_by_telegram_message(100, 77) is None
    assert repository.mark_sent(queued.id, claimed.lease_token, LATER, 77)

    found = repository.get_sent_by_telegram_message(100, 77)

    assert found.id == queued.id
    assert repository.get_sent_by_telegram_message(101, 77) is None
    assert repository.get_sent_by_telegram_message(100, 78) is None


def test_concurrent_claim_from_two_connections_only_claims_once(tmp_path):
    """SQLite write-lock не даёт двум worker-ам взять одно событие одновременно."""
    path = tmp_path / "claim-race.sqlite"
    initialize_database(path)
    first = connect(path)
    second = sqlite3.connect(path, timeout=0)
    second.row_factory = sqlite3.Row
    second.execute("PRAGMA foreign_keys = ON")
    try:
        first_repository = NotificationOutboxRepository(first)
        second_repository = NotificationOutboxRepository(second)
        queued = first_repository.enqueue(event())
        first.commit()

        first.execute("BEGIN IMMEDIATE")
        claimed = first_repository.claim(queued.id, NOW, LATER)
        assert claimed is not None

        with pytest.raises(sqlite3.OperationalError, match="database is locked"):
            second_repository.claim(queued.id, NOW, LATER)

        first.commit()

        assert second_repository.claim(queued.id, NOW, LATER) is None
        stored = second_repository.get_by_id(queued.id)
        assert (stored.status, stored.attempt_count) == (NotificationOutboxStatus.SENDING, 1)
    finally:
        if first.in_transaction:
            first.rollback()
        if second.in_transaction:
            second.rollback()
        first.close()
        second.close()


def test_reschedule_failed_and_expired_lease_transitions(db):
    repository = NotificationOutboxRepository(db)
    retry = repository.enqueue(event("retry"))
    failed = repository.enqueue(event("failed"))
    stuck = repository.enqueue(event("stuck"))
    retry_claim = repository.claim(retry.id, NOW, LATER)
    failed_claim = repository.claim(failed.id, NOW, LATER)
    stuck_claim = repository.claim(stuck.id, NOW, "2026-10-20T10:01:00+00:00")

    assert repository.reschedule(retry.id, retry_claim.lease_token, "2026-10-20T10:06:00+00:00", "temporary")
    assert repository.mark_failed(failed.id, failed_claim.lease_token, "permanent")
    assert repository.recover_expired("2026-10-20T10:02:00+00:00") == 1
    assert repository.get_by_id(retry.id).status is NotificationOutboxStatus.PENDING
    assert repository.get_by_id(retry.id).last_error == "temporary"
    assert repository.get_by_id(failed.id).status is NotificationOutboxStatus.FAILED
    recovered = repository.get_by_id(stuck.id)
    assert (recovered.status, recovered.attempt_count, recovered.lease_token) == (
        NotificationOutboxStatus.PENDING, 1, None,
    )


def test_old_lease_cannot_overwrite_a_new_claim_after_recovery(db):
    repository = NotificationOutboxRepository(db)
    queued = repository.enqueue(event())
    old_claim = repository.claim(queued.id, NOW, "2026-10-20T10:01:00+00:00")
    assert repository.recover_expired("2026-10-20T10:02:00+00:00") == 1
    new_claim = repository.claim(queued.id, "2026-10-20T10:02:00+00:00", LATER)

    assert new_claim.attempt_count == 2
    assert not repository.mark_sent(queued.id, old_claim.lease_token, LATER)
    assert repository.reschedule(queued.id, new_claim.lease_token, "2026-10-20T10:06:00+00:00", "retry")


def test_enqueue_and_claim_leave_transaction_boundary_to_caller(db):
    repository = NotificationOutboxRepository(db)
    db.execute("BEGIN")
    queued = repository.enqueue(event())
    repository.claim(queued.id, NOW, LATER)
    db.rollback()

    assert repository.get_by_id(queued.id) is None


def test_offer_deletion_nulls_outbox_offer_reference(db):
    db.execute("INSERT INTO users (telegram_user_id, created_at) VALUES (1, ?)", (NOW,))
    db.execute("INSERT INTO products (user_id, name, created_at) VALUES (1, 'item', ?)", (NOW,))
    db.execute("INSERT INTO offers (product_id, url, normalized_url, created_at) VALUES (1, 'https://a', 'https://a', ?)", (NOW,))
    queued = NotificationOutboxRepository(db).enqueue(event(offer_id=1))

    db.execute("DELETE FROM offers WHERE id = 1")
    assert NotificationOutboxRepository(db).get_by_id(queued.id).offer_id is None


def test_schema_checks_status_and_attempt_count(db):
    with pytest.raises(sqlite3.IntegrityError):
        db.execute(
            """INSERT INTO notification_outbox
               (event_key, event_type, chat_id, payload_json, status, attempt_count, next_attempt_at, created_at)
               VALUES ('bad-status', 'PRICE_DROP', 1, '{}', 'UNKNOWN', 0, ?, ?)""",
            (NOW, NOW),
        )
    with pytest.raises(sqlite3.IntegrityError):
        db.execute(
            """INSERT INTO notification_outbox
               (event_key, event_type, chat_id, payload_json, status, attempt_count, next_attempt_at, created_at)
               VALUES ('bad-attempt', 'PRICE_DROP', 1, '{}', 'PENDING', -1, ?, ?)""",
            (NOW, NOW),
        )
