from __future__ import annotations

import asyncio
import csv
import io
import logging
import sqlite3
import threading
from datetime import datetime, timezone
from decimal import Decimal

import pytest
from telegram import Chat, Message, Update, User
from telegram.ext import ExtBot

from pricewatch.database import connect, initialize_database
from pricewatch.domain.enums import (
    ExtractionMethod,
    ExtractionStatus,
    FetchMethod,
    FetchStatus,
    OfferStatus,
)
from pricewatch.scraping.models import ExtractionResult, FetchResult, PriceCheckResult
from pricewatch.scraping.url_security import UrlSecurity
from pricewatch.services.monitoring_service import MonitoringService
from pricewatch.services.csv_service import (
    CsvImportCode,
    CsvImportRecordResult,
    CsvImportResult,
)
from pricewatch.services.product_service import ProductService
from pricewatch.telegram.app import (
    WELCOME_TEXT,
    _csv_import_and_report,
    _csv_import_summary,
    build_application,
)


TOKEN = "123456:" + "csv_offline_test_token_" * 2


class Checker:
    def __init__(self) -> None:
        self.calls: list[str] = []
        self.started: threading.Event | None = None
        self.release: threading.Event | None = None

    def check(self, url: str) -> PriceCheckResult:
        self.calls.append(url)
        if self.started is not None:
            self.started.set()
        if self.release is not None:
            assert self.release.wait(timeout=5)
        fetch = FetchResult(
            url,
            FetchMethod.REQUESTS,
            FetchStatus.SUCCESS,
            final_url=url,
            http_status=200,
            html="<html>",
        )
        extraction = ExtractionResult(
            ExtractionStatus.SUCCESS,
            title="Store title",
            price=Decimal("19.99"),
            currency="EUR",
            method=ExtractionMethod.JSON_LD,
        )
        return PriceCheckResult(fetch, extraction, (fetch,))


class Runner:
    def __init__(self, path) -> None:
        self.path = path
        self.checker = Checker()
        self.calls: list[tuple[int, int]] = []
        self.raise_next = False
        self.security = UrlSecurity(
            lambda host, port: [(None, None, None, None, ("93.184.216.34", 0))]
        )

    def __call__(self, path, telegram_id, username, operation):
        self.calls.append((telegram_id, threading.get_ident()))
        if self.raise_next:
            self.raise_next = False
            raise RuntimeError("worker failed")
        connection = connect(self.path)
        try:
            products = ProductService(connection, self.checker, self.security)
            user = products.get_or_create_user(telegram_id, username)
            return operation(products, MonitoringService(connection, self.checker), user.id)
        finally:
            connection.close()

    def direct(self, telegram_id, operation):
        return self(None, telegram_id, f"u{telegram_id}", operation)


class Download:
    def __init__(self, value) -> None:
        self.value = value

    async def download_as_bytearray(self):
        if isinstance(self.value, Exception):
            raise self.value
        return bytearray(self.value)


class CsvBot(ExtBot):
    def __init__(self) -> None:
        super().__init__(TOKEN)
        self._files: dict[str, bytes | Exception] = {}
        self._file_requests: list[str] = []
        self._messages: list[dict] = []
        self._documents: list[dict] = []
        self._fail_document_send = False
        self._fail_message_text: str | None = None
        self._fail_edit_markup = False
        self._edits: list[dict] = []
        self._message_id = 1000
        self._bot_user = User(999, "PriceWatch", True, username="pricewatch_test_bot")
        self._initialized = True

    async def send_message(self, chat_id, text, reply_markup=None, **kwargs):
        if self._fail_message_text and self._fail_message_text in text:
            self._fail_message_text = None
            raise RuntimeError("telegram message send failed")
        self._message_id += 1
        item = {"chat_id": chat_id, "text": text}
        if reply_markup is not None:
            item["reply_markup"] = reply_markup.to_dict()
        self._messages.append(item)
        message = Message(
            self._message_id,
            datetime.fromtimestamp(0, timezone.utc),
            Chat(chat_id, "private"),
            from_user=self._bot_user,
            text=text,
            reply_markup=reply_markup,
        )
        message.set_bot(self)
        return message

    async def answer_callback_query(self, callback_query_id, **kwargs):
        return True

    async def edit_message_reply_markup(
        self, chat_id=None, message_id=None, reply_markup=None, **kwargs
    ):
        if self._fail_edit_markup:
            self._fail_edit_markup = False
            raise RuntimeError("telegram markup edit failed")
        self._edits.append({
            "chat_id": chat_id,
            "message_id": message_id,
            "reply_markup": reply_markup.to_dict() if reply_markup else None,
        })
        return True

    async def get_file(self, file_id, **kwargs):
        self._file_requests.append(file_id)
        return Download(self._files[file_id])

    async def send_document(self, chat_id, document, filename=None, **kwargs):
        if self._fail_document_send:
            self._fail_document_send = False
            raise RuntimeError("telegram document send failed")
        position = document.tell()
        content = document.read()
        document.seek(position)
        self._documents.append(
            {"chat_id": chat_id, "filename": filename, "content": content}
        )
        return True

    @property
    def files(self):
        return self._files

    @property
    def file_requests(self):
        return self._file_requests

    @property
    def messages(self):
        return self._messages

    @property
    def documents(self):
        return self._documents

    @property
    def edits(self):
        return self._edits


class Harness:
    def __init__(self, app, bot, runner) -> None:
        self.app = app
        self.bot = bot
        self.runner = runner
        self.update_id = 0

    async def text(self, text, *, user_id=1):
        self.update_id += 1
        message = {
            "message_id": self.update_id,
            "date": 0,
            "chat": {"id": user_id, "type": "private"},
            "from": {
                "id": user_id,
                "is_bot": False,
                "first_name": "User",
                "username": f"u{user_id}",
            },
            "text": text,
        }
        if text.startswith("/"):
            command = text.split()[0]
            message["entities"] = [
                {"type": "bot_command", "offset": 0, "length": len(command)}
            ]
        update = Update.de_json(
            {"update_id": self.update_id, "message": message}, self.app.bot
        )
        await asyncio.wait_for(self.app.process_update(update), timeout=3)

    async def callback(self, data, *, user_id=1):
        self.update_id += 1
        update = Update.de_json(
            {
                "update_id": self.update_id,
                "callback_query": {
                    "id": f"q{self.update_id}",
                    "from": {
                        "id": user_id,
                        "is_bot": False,
                        "first_name": "User",
                        "username": f"u{user_id}",
                    },
                    "chat_instance": "offline",
                    "data": data,
                    "message": {
                        "message_id": self.update_id,
                        "date": 0,
                        "chat": {"id": user_id, "type": "private"},
                        "from": {
                            "id": 999,
                            "is_bot": True,
                            "first_name": "PriceWatch",
                        },
                        "text": "screen",
                    },
                },
            },
            self.app.bot,
        )
        await asyncio.wait_for(self.app.process_update(update), timeout=3)

    async def document(
        self,
        content,
        *,
        user_id=1,
        file_id=None,
        filename="products.csv",
        declared_size=None,
    ):
        self.update_id += 1
        file_id = file_id or f"file-{self.update_id}"
        self.bot.files[file_id] = content
        payload = {
            "file_id": file_id,
            "file_unique_id": f"unique-{file_id}",
            "file_name": filename,
            "mime_type": "text/csv",
        }
        if declared_size is not None:
            payload["file_size"] = declared_size
        update = Update.de_json(
            {
                "update_id": self.update_id,
                "message": {
                    "message_id": self.update_id,
                    "date": 0,
                    "chat": {"id": user_id, "type": "private"},
                    "from": {
                        "id": user_id,
                        "is_bot": False,
                        "first_name": "User",
                        "username": f"u{user_id}",
                    },
                    "document": payload,
                },
            },
            self.app.bot,
        )
        await asyncio.wait_for(self.app.process_update(update), timeout=3)

    @property
    def texts(self):
        return [item["text"] for item in self.bot.messages]

    @property
    def last_text(self):
        return self.texts[-1]

    def buttons(self, message_index=-1):
        markup = self.bot.messages[message_index].get("reply_markup", {})
        return {
            button["text"]: button["callback_data"]
            for row in markup.get("inline_keyboard", [])
            for button in row
        }


def build_harness(path, *, offload_services=False):
    runner = Runner(path)
    bot = CsvBot()
    app = build_application(
        TOKEN,
        frozenset({1, 2}),
        str(path),
        service_runner=runner,
        bot=bot,
        offload_services=offload_services,
    )
    app._initialized = True
    app.bot._initialized = True
    return Harness(app, bot, runner)


@pytest.fixture
def ui(tmp_path):
    path = tmp_path / "telegram-csv.db"
    initialize_database(path)
    return build_harness(path)


@pytest.mark.asyncio
async def test_import_menu_preview_confirmation_summary_report_and_repeat(ui, caplog):
    caplog.set_level(logging.INFO, logger="pricewatch.telegram.app")
    await ui.text("/start")
    assert {"Импорт CSV", "Экспорт CSV"} <= set(ui.buttons())
    await ui.callback("menu:csv-import")
    assert "product_name,url" in ui.last_text and "5 МиБ" in ui.last_text

    content = (
        "\ufeffproduct_name,url\n"
        "Телефон,https://shop.test/phone\n"
        ",https://shop.test/invalid\n"
    ).encode()
    await ui.document(content, filename="вход.csv", declared_size=len(content))

    assert "Всего записей: 2" in ui.last_text
    assert "Корректных: 1" in ui.last_text and "INVALID: 1" in ui.last_text
    assert "Строка 3" in ui.last_text
    assert ui.runner.checker.calls == []
    connection = connect(ui.runner.path)
    try:
        assert connection.execute("SELECT COUNT(*) FROM products").fetchone()[0] == 0
    finally:
        connection.close()

    confirm = ui.buttons()["Импортировать"]
    await ui.callback(confirm)

    assert any(text == "Импорт выполняется…" for text in ui.texts)
    assert ui.bot.edits and ui.bot.edits[-1]["reply_markup"] is None
    summary = next(text for text in ui.texts if "Импортировано: 1 (ACTIVE: 1, PENDING: 0)" in text)
    assert "Некорректных: 1" in summary
    assert {"Мои товары", "Импортировать еще", "Главное меню"} <= set(ui.buttons())
    import_again = ui.buttons()["Импортировать еще"]
    assert "flow" not in ui.app.user_data[1]
    assert len(ui.runner.checker.calls) == 1
    assert len(ui.bot.documents) == 1
    csv_logs = [
        record.getMessage() for record in caplog.records
        if "event=csv_import_" in record.getMessage()
    ]
    assert len([message for message in csv_logs if "csv_import_started" in message]) == 1
    completed = next(message for message in csv_logs if "csv_import_completed" in message)
    assert "outcome=partial" in completed
    assert (
        "total=2 imported=1 pending=0 invalid=1 duplicate=0 blocked=0 "
        "ambiguous=0 errors=0 unprocessed=0"
    ) in completed
    joined = "\n".join(csv_logs)
    assert "event=csv_import_interrupted" not in joined
    assert "https://" not in joined
    assert "Телефон" not in joined and "вход.csv" not in joined
    assert "chat_id" not in joined and "user_id" not in joined
    report = ui.bot.documents[0]
    assert report["filename"] == "import_report.csv"
    rows = list(csv.reader(io.StringIO(report["content"].decode())))
    assert [row[3] for row in rows[1:]] == ["IMPORTED", "INVALID"]

    await ui.callback(ui.buttons()["Мои товары"])
    assert "Ваши товары" in ui.last_text
    await ui.callback("menu:home")
    assert "Главное меню" in ui.last_text
    await ui.callback(import_again)
    await ui.document(b"product_name,url\nB,https://shop.test/b\n")
    assert ui.app.user_data[1]["flow"]["kind"] == "csv-preview-ready"

    await ui.callback(confirm)
    assert "устарело" in ui.last_text
    assert {"Импорт CSV", "Главное меню"} <= set(ui.buttons())
    assert len(ui.runner.checker.calls) == 1
    assert len(ui.bot.documents) == 1


@pytest.mark.asyncio
async def test_rejected_files_metadata_actual_size_download_error_and_outside_flow(
    ui, caplog,
):
    caplog.set_level(logging.INFO, logger="pricewatch.telegram.app")
    await ui.document(b"product_name,url\nA,https://shop.test/a\n")
    assert "не ожидается" in ui.last_text
    assert ui.bot.file_requests == []

    await ui.callback("menu:csv-import")
    await ui.document(
        b"small",
        file_id="metadata-large",
        declared_size=5 * 1024 * 1024 + 1,
    )
    assert "больше 5 МиБ" in ui.last_text
    assert "metadata-large" not in ui.bot.file_requests

    await ui.document(b"x" * (5 * 1024 * 1024 + 1), file_id="actual-large")
    assert "структурно отклонён" in ui.last_text
    assert "Импортировать" not in ui.buttons()

    await ui.document(
        RuntimeError("download unavailable"),
        file_id="download-error",
    )
    assert "Не удалось загрузить" in ui.last_text
    assert ui.app.user_data[1]["flow"]["kind"] == "csv-waiting-file"

    await ui.document(b"product_name,url\n,https://shop.test/a\n")
    assert "Корректных: 0" in ui.last_text
    assert "Импортировать" not in ui.buttons()
    messages = [record.getMessage() for record in caplog.records]
    assert any(
        "event=csv_import_completed outcome=invalid" in message
        for message in messages
    )
    download_failed = next(
        message for message in messages
        if "event=csv_import_failed action=download" in message
    )
    assert "exception_class=RuntimeError" in download_failed
    assert "download unavailable" not in download_failed
    assert "download-error" not in "\n".join(messages)


@pytest.mark.asyncio
async def test_unexpected_parse_error_keeps_working_waiting_state(
    ui, monkeypatch, caplog,
):
    caplog.set_level(logging.INFO, logger="pricewatch.telegram.app")
    await ui.callback("menu:csv-import")

    def fail_parse(service, content, filename):
        raise RuntimeError("unexpected parser failure")

    with monkeypatch.context() as patch:
        patch.setattr("pricewatch.telegram.app.CsvService.parse", fail_parse)
        await ui.document(b"product_name,url\nA,https://shop.test/a\n")

    assert "Не удалось проверить CSV-файл" in ui.last_text
    assert "Отправьте другой CSV-документ" in ui.last_text
    assert {"Отмена", "Главное меню"} <= set(ui.buttons())
    assert ui.app.user_data[1]["flow"]["kind"] == "csv-waiting-file"
    await ui.document(b"product_name,url\nB,https://shop.test/b\n")
    assert ui.app.user_data[1]["flow"]["kind"] == "csv-preview-ready"
    failed = next(
        record.getMessage() for record in caplog.records
        if "event=csv_import_failed action=preview" in record.getMessage()
    )
    assert "exception_class=RuntimeError" in failed
    assert "unexpected parser failure" not in failed
    assert "https://" not in failed


@pytest.mark.asyncio
async def test_csv_import_success_log_has_aggregate_safe_fields(ui, caplog):
    caplog.set_level(logging.INFO, logger="pricewatch.telegram.app")
    await ui.callback("menu:csv-import")
    await ui.document(
        b"product_name,url\nSensitive Name,https://shop.test/private?token=sample\n",
        filename="private-name.csv",
    )

    await ui.callback(ui.buttons()["Импортировать"])

    messages = [
        record.getMessage() for record in caplog.records
        if "event=csv_import_" in record.getMessage()
    ]
    completed = next(message for message in messages if "csv_import_completed" in message)
    assert "outcome=success" in completed
    assert (
        "total=1 imported=1 pending=0 invalid=0 duplicate=0 blocked=0 "
        "ambiguous=0 errors=0 unprocessed=0"
    ) in completed
    joined = "\n".join(messages)
    assert "Sensitive Name" not in joined
    assert "https://" not in joined and "private-name.csv" not in joined


@pytest.mark.asyncio
async def test_import_without_report_still_gets_terminal_navigation(ui, monkeypatch):
    await ui.callback("menu:csv-import")
    await ui.document(b"product_name,url\nA,https://shop.test/a\n")
    confirm = ui.buttons()["Импортировать"]
    empty_result = CsvImportResult(
        "source.csv",
        0,
        records=(),
        started=True,
    )
    monkeypatch.setattr(
        "pricewatch.telegram.app._csv_import_and_report",
        lambda products, user_id, preview, operation_id: (empty_result, None),
    )

    await ui.callback(confirm)

    assert any("Всего строк: 0" in text for text in ui.texts)
    assert ui.bot.documents == []
    assert {"Мои товары", "Импортировать еще", "Главное меню"} <= set(ui.buttons())
    assert "flow" not in ui.app.user_data[1]


@pytest.mark.asyncio
async def test_import_with_zero_imported_duplicate_rows_has_report_and_navigation(ui):
    product = ui.runner.direct(1, lambda p, m, u: p.create_product(u, "Existing").product)
    ui.runner.direct(
        1,
        lambda p, m, u: p.add_offer(u, product.id, "https://shop.test/duplicate"),
    )
    await ui.callback("menu:csv-import")
    await ui.document(b"product_name,url\nExisting,https://shop.test/duplicate\n")

    await ui.callback(ui.buttons()["Импортировать"])

    summary = next(text for text in ui.texts if "Дубликатов: 1" in text)
    assert "Импортировано: 0" in summary
    assert ui.bot.documents[-1]["filename"] == "import_report.csv"
    assert {"Мои товары", "Импортировать еще", "Главное меню"} <= set(ui.buttons())
    assert "flow" not in ui.app.user_data[1]


@pytest.mark.asyncio
async def test_new_file_invalidates_old_callback_cancel_and_restart(ui):
    await ui.callback("menu:csv-import")
    await ui.document(b"product_name,url\nA,https://shop.test/a\n")
    old_confirm = ui.buttons()["Импортировать"]
    await ui.document(b"product_name,url\nB,https://shop.test/b\n")
    new_confirm = ui.buttons()["Импортировать"]
    cancel = ui.buttons()["Отмена"]

    await ui.callback(old_confirm)
    assert "устарело" in ui.last_text and ui.runner.checker.calls == []
    assert {"Импорт CSV", "Главное меню"} <= set(ui.buttons())
    await ui.callback(cancel)
    assert "Импорт CSV отменён" in ui.last_text
    assert "flow" not in ui.app.user_data[1]
    await ui.callback(new_confirm)
    assert "устарело" in ui.last_text and ui.runner.checker.calls == []
    restart_import = ui.buttons()["Импорт CSV"]
    await ui.callback(restart_import)
    assert ui.app.user_data[1]["flow"]["kind"] == "csv-waiting-file"
    await ui.document(b"product_name,url\nFresh,https://shop.test/fresh\n")
    assert ui.app.user_data[1]["flow"]["kind"] == "csv-preview-ready"

    await ui.callback("menu:csv-import")
    await ui.document(b"product_name,url\nC,https://shop.test/c\n")
    section_confirm = ui.buttons()["Импортировать"]
    await ui.callback("menu:settings")
    await ui.callback(section_confirm)
    assert "устарело" in ui.last_text and ui.runner.checker.calls == []

    await ui.callback("menu:csv-import")
    await ui.text("/cancel")
    assert "Главное меню" in ui.last_text
    assert "flow" not in ui.app.user_data[1]

    restarted = build_harness(ui.runner.path)
    await restarted.callback(old_confirm)
    assert "Начните новый импорт CSV" in restarted.last_text
    assert {"Импорт CSV", "Главное меню"} <= set(restarted.buttons())
    await restarted.callback(restarted.buttons()["Импорт CSV"])
    await restarted.document(b"product_name,url\nRestarted,https://shop.test/restarted\n")
    assert restarted.app.user_data[1]["flow"]["kind"] == "csv-preview-ready"


@pytest.mark.asyncio
async def test_document_during_rename_keeps_flow_and_offers_working_actions(ui):
    product = ui.runner.direct(1, lambda p, m, u: p.create_product(u, "Phone").product)
    await ui.callback(f"rename:{product.id}")
    flow = dict(ui.app.user_data[1]["flow"])
    file_requests = len(ui.bot.file_requests)

    await ui.document(b"not used", filename="unexpected.pdf")

    assert "ожидается текстовый ввод" in ui.last_text
    assert {"Продолжить ввод", "Отмена", "Главное меню"} <= set(ui.buttons())
    assert ui.app.user_data[1]["flow"] == flow
    assert len(ui.bot.file_requests) == file_requests
    assert ui.runner.direct(1, lambda p, m, u: p.get_product_details(u, product.id)).product.name == "Phone"

    await ui.callback(ui.buttons()["Продолжить ввод"])
    assert "Отправьте новое название" in ui.last_text
    assert ui.app.user_data[1]["flow"] == flow
    await ui.callback(ui.buttons()["Отмена"])
    assert ui.last_text.startswith("Phone\n")
    assert "flow" not in ui.app.user_data[1]


@pytest.mark.asyncio
async def test_document_during_new_url_can_go_home_without_starting_csv(ui):
    await ui.callback("menu:add")
    flow = dict(ui.app.user_data[1]["flow"])
    file_requests = len(ui.bot.file_requests)
    checker_calls = len(ui.runner.checker.calls)

    await ui.document(b"product_name,url\nA,https://shop.test/a\n")

    assert "Отправьте URL товара" in ui.last_text
    assert ui.app.user_data[1]["flow"] == flow
    assert len(ui.bot.file_requests) == file_requests
    assert len(ui.runner.checker.calls) == checker_calls
    await ui.callback(ui.buttons()["Главное меню"])
    assert "Главное меню" in ui.last_text
    assert "flow" not in ui.app.user_data[1]


@pytest.mark.asyncio
async def test_document_during_threshold_keeps_value_and_cancel_returns_product(ui):
    product = ui.runner.direct(1, lambda p, m, u: p.create_product(u, "Phone").product)
    await ui.callback(f"product-threshold:{product.id}")
    flow = dict(ui.app.user_data[1]["flow"])

    await ui.document(b"12.5", filename="threshold.txt")

    assert "Введите порог" in ui.last_text
    assert ui.app.user_data[1]["flow"] == flow
    details = ui.runner.direct(1, lambda p, m, u: p.get_product_details(u, product.id))
    assert details.product.alert_threshold is None
    await ui.callback(ui.buttons()["Отмена"])
    assert ui.last_text.startswith("Phone\n")
    assert "flow" not in ui.app.user_data[1]


@pytest.mark.asyncio
async def test_running_import_is_atomic_nonblocking_and_cannot_be_cancelled(
    tmp_path, monkeypatch
):
    path = tmp_path / "telegram-running.db"
    initialize_database(path)
    ui = build_harness(path, offload_services=True)
    service_started = asyncio.Event()
    release_service = asyncio.Event()
    offloaded = []
    import_blocked = False

    async def controlled_thread(function, *args):
        nonlocal import_blocked
        offloaded.append(function)
        if function is ui.runner and args[1] == 1 and not import_blocked:
            import_blocked = True
            service_started.set()
            await release_service.wait()
        return function(*args)

    monkeypatch.setattr("pricewatch.telegram.app.run_in_daemon_thread", controlled_thread)
    await ui.callback("menu:csv-import")
    await ui.document(b"product_name,url\nA,https://shop.test/slow\n")
    confirm = ui.buttons()["Импортировать"]
    cancel = ui.buttons()["Отмена"]

    task = asyncio.create_task(ui.callback(confirm))
    try:
        await asyncio.wait_for(service_started.wait(), timeout=1)
        assert "Импорт выполняется…" in ui.texts

        await ui.callback(confirm)
        await ui.callback(cancel)
        await ui.callback("menu:home")
        await ui.text("/cancel")
        await ui.text("/start")
        await ui.text("/check 999")
        await ui.callback("check:999")
        await ui.callback("offer:999")
        await ui.document(b"product_name,url\nB,https://shop.test/new\n")
        await ui.text("/start", user_id=2)
        assert sum("уже выполняется" in text for text in ui.texts) >= 9
        assert WELCOME_TEXT in ui.texts
    finally:
        release_service.set()
        await task
    assert len(ui.runner.checker.calls) == 1
    assert ui.runner in offloaded
    assert any(function is not ui.runner for function in offloaded)
    assert "flow" not in ui.app.user_data[1]


@pytest.mark.asyncio
async def test_cancelled_import_wait_logs_uncertain_and_cleans_running_guard(
    tmp_path, monkeypatch, caplog,
):
    path = tmp_path / "telegram-cancelled-import.db"
    initialize_database(path)
    ui = build_harness(path, offload_services=True)
    worker_started = threading.Event()
    worker_release = threading.Event()
    worker_finished = threading.Event()
    calls = []

    def blocked_import(products, user_id, preview, operation_id):
        calls.append(operation_id)
        worker_started.set()
        worker_release.wait(timeout=5)
        try:
            return _csv_import_and_report(
                products, user_id, preview, operation_id
            )
        finally:
            worker_finished.set()

    monkeypatch.setattr(
        "pricewatch.telegram.app._csv_import_and_report", blocked_import
    )
    caplog.set_level(logging.INFO, logger="pricewatch.telegram.app")
    await ui.callback("menu:csv-import")
    await ui.document(
        b"product_name,url\nSensitive,https://shop.test/private-token\n",
        filename="private-name.csv",
    )
    confirm = ui.buttons()["Импортировать"]

    task = asyncio.create_task(ui.callback(confirm))
    while not worker_started.is_set():
        await asyncio.sleep(0.001)
    task.cancel()
    with pytest.raises(asyncio.CancelledError):
        await task

    assert not worker_finished.is_set()
    assert "flow" not in ui.app.user_data[1]
    assert len(calls) == 1
    await ui.callback(confirm)
    assert "устарело" in ui.last_text
    assert len(calls) == 1
    messages = [record.getMessage() for record in caplog.records]
    interrupted = [
        message for message in messages
        if "event=csv_import_interrupted" in message
    ]
    assert len(interrupted) == 1
    assert (
        "outcome=uncertain" in interrupted[0]
        and "reason=task_cancelled" in interrupted[0]
        and "operation_id=" in interrupted[0]
        and "duration_ms=" in interrupted[0]
    )
    joined = "\n".join(messages)
    for forbidden in (
        "Sensitive", "private-token", "private-name.csv", "shop.test",
        "chat_id", "user_id",
    ):
        assert forbidden not in joined

    worker_release.set()
    while not worker_finished.is_set():
        await asyncio.sleep(0.001)
    assert len(ui.runner.checker.calls) == 1
    assert len(calls) == 1


@pytest.mark.asyncio
async def test_report_send_failure_does_not_repeat_import(ui):
    await ui.callback("menu:csv-import")
    await ui.document(b"product_name,url\nA,https://shop.test/a\n")
    confirm = ui.buttons()["Импортировать"]
    ui.bot._fail_document_send = True

    await ui.callback(confirm)

    assert any("Повторный импорт автоматически не запускается" in text for text in ui.texts)
    assert {"Мои товары", "Импортировать еще", "Главное меню"} <= set(ui.buttons())
    assert len(ui.runner.checker.calls) == 1
    assert "flow" not in ui.app.user_data[1]
    await ui.callback(confirm)
    assert "устарело" in ui.last_text
    assert len(ui.runner.checker.calls) == 1


@pytest.mark.asyncio
async def test_csv_confirmation_edit_failure_does_not_repeat_import(ui, caplog):
    await ui.callback("menu:csv-import")
    await ui.document(b"product_name,url\nA,https://shop.test/edit-failure\n")
    confirm = ui.buttons()["Импортировать"]
    ui.bot._fail_edit_markup = True
    caplog.set_level(logging.INFO, logger="pricewatch.telegram.app")

    await ui.callback(confirm)
    await ui.callback(confirm)

    assert len(ui.runner.checker.calls) == 1
    assert "устарело" in ui.last_text
    assert "flow" not in ui.app.user_data[1]
    callback_log = next(
        record.getMessage() for record in caplog.records
        if "action=confirmation_keyboard_cleanup" in record.getMessage()
    )
    assert callback_log == (
        "event=telegram_callback action=confirmation_keyboard_cleanup "
        "outcome=error exception_class=RuntimeError"
    )
    assert "telegram markup edit failed" not in callback_log
    assert "callback_data" not in callback_log
    assert "chat_id" not in callback_log and "message_id" not in callback_log


@pytest.mark.asyncio
async def test_csv_cancel_removes_confirmation_keyboard(ui):
    await ui.callback("menu:csv-import")
    await ui.document(b"product_name,url\nA,https://shop.test/cancel-buttons\n")
    cancel = ui.buttons()["Отмена"]

    await ui.callback(cancel)

    assert ui.bot.edits and ui.bot.edits[-1]["reply_markup"] is None
    assert "flow" not in ui.app.user_data[1]
    assert "Главное меню" in ui.last_text


@pytest.mark.asyncio
async def test_summary_send_failure_keeps_navigation_without_repeating_import(ui):
    await ui.callback("menu:csv-import")
    await ui.document(b"product_name,url\nA,https://shop.test/summary-failure\n")
    confirm = ui.buttons()["Импортировать"]
    ui.bot._fail_message_text = "Импорт CSV завершён.\nВсего строк"

    await ui.callback(confirm)

    assert any("Повторный импорт автоматически не запускается" in text for text in ui.texts)
    assert {"Мои товары", "Импортировать еще", "Главное меню"} <= set(ui.buttons())
    assert len(ui.runner.checker.calls) == 1
    assert ui.bot.documents == []
    assert "flow" not in ui.app.user_data[1]
    await ui.callback(confirm)
    assert "устарело" in ui.last_text
    assert len(ui.runner.checker.calls) == 1


@pytest.mark.asyncio
async def test_worker_failure_releases_state_without_automatic_retry(ui, caplog):
    await ui.callback("menu:csv-import")
    await ui.document(b"product_name,url\nA,https://shop.test/a\n")
    confirm = ui.buttons()["Импортировать"]
    ui.runner.raise_next = True
    caplog.set_level(logging.INFO, logger="pricewatch.telegram.app")

    await ui.callback(confirm)

    assert "Не удалось подтвердить, как завершился импорт" in ui.last_text
    assert "Автоматический повтор не выполнялся" in ui.last_text
    assert {"Мои товары", "Импортировать еще", "Главное меню"} <= set(ui.buttons())
    assert "flow" not in ui.app.user_data[1]
    assert ui.runner.checker.calls == []
    await ui.callback(confirm)
    assert "устарело" in ui.last_text
    assert ui.runner.checker.calls == []
    failed = next(
        record.getMessage() for record in caplog.records
        if "event=csv_import_failed action=process" in record.getMessage()
    )
    assert "exception_class=RuntimeError" in failed
    assert "worker failed" not in failed


@pytest.mark.asyncio
async def test_start_notification_failure_clears_state_without_import(ui):
    await ui.callback("menu:csv-import")
    await ui.document(b"product_name,url\nA,https://shop.test/a\n")
    confirm = ui.buttons()["Импортировать"]
    ui.bot._fail_message_text = "Импорт выполняется…"

    await ui.callback(confirm)

    assert ui.runner.checker.calls == []
    assert "flow" not in ui.app.user_data[1]
    assert "Импорт не был запущен" in ui.last_text
    assert {"Импорт CSV", "Главное меню"} <= set(ui.buttons())

    await ui.callback("menu:csv-import")
    assert ui.app.user_data[1]["flow"]["kind"] == "csv-waiting-file"
    await ui.callback(confirm)
    assert "устарело" in ui.last_text
    assert ui.runner.checker.calls == []


@pytest.mark.asyncio
async def test_export_document_empty_result_send_failure_and_authorization(ui, caplog):
    caplog.set_level(logging.INFO, logger="pricewatch.telegram.app")
    product = ui.runner.direct(1, lambda p, m, u: p.create_product(u, "Phone").product)
    ui.runner.direct(
        1,
        lambda p, m, u: p.add_offer(u, product.id, "https://shop.test/export"),
    )
    calls_before = len(ui.runner.checker.calls)

    await ui.callback("menu:csv-export")
    assert ui.bot.documents[-1]["filename"] == "products.csv"
    assert ui.bot.documents[-1]["content"].startswith(b"product_name,store,url")
    assert "Экспорт CSV завершён" in ui.last_text
    assert {"Мои товары", "Главное меню"} <= set(ui.buttons())
    assert len(ui.runner.checker.calls) == calls_before

    documents_before = len(ui.bot.documents)
    await ui.callback("menu:csv-export", user_id=2)
    assert ui.last_text == "Нет товаров для экспорта"
    assert {"Мои товары", "Главное меню"} <= set(ui.buttons())
    assert len(ui.bot.documents) == documents_before

    ui.bot._fail_document_send = True
    await ui.callback("menu:csv-export")
    assert "Не удалось отправить CSV-файл" in ui.last_text
    assert {"Мои товары", "Главное меню"} <= set(ui.buttons())
    assert len(ui.runner.checker.calls) == calls_before

    file_requests = len(ui.bot.file_requests)
    await ui.document(
        b"product_name,url\nX,https://shop.test/x\n",
        user_id=99,
    )
    assert "Доступ" in ui.last_text
    assert len(ui.bot.file_requests) == file_requests
    messages = [
        record.getMessage() for record in caplog.records
        if "event=csv_export_" in record.getMessage()
    ]
    assert sum("event=csv_export_started" in message for message in messages) == 3
    assert any(
        "event=csv_export_completed outcome=success" in message
        and "exported=1" in message
        for message in messages
    )
    assert any(
        "event=csv_export_completed outcome=empty" in message
        and "exported=0" in message
        for message in messages
    )
    failed = next(
        message for message in messages
        if "event=csv_export_failed action=delivery" in message
    )
    assert "exception_class=RuntimeError" in failed
    joined = "\n".join(messages)
    assert "telegram document send failed" not in joined
    assert "Phone" not in joined and "https://" not in joined
    assert "products.csv" not in joined
    assert "chat_id" not in joined and "user_id" not in joined


def test_import_summary_distinguishes_every_result_and_offer_status():
    result = CsvImportResult(
        "source.csv",
        8,
        records=(
            CsvImportRecordResult(2, "A", "u1", CsvImportCode.IMPORTED, offer_status=OfferStatus.ACTIVE),
            CsvImportRecordResult(3, "B", "u2", CsvImportCode.IMPORTED, offer_status=OfferStatus.PENDING),
            CsvImportRecordResult(4, "C", "u3", CsvImportCode.DUPLICATE),
            CsvImportRecordResult(5, "D", "u4", CsvImportCode.INVALID),
            CsvImportRecordResult(6, "E", "u5", CsvImportCode.BLOCKED),
            CsvImportRecordResult(7, "F", "u6", CsvImportCode.AMBIGUOUS),
            CsvImportRecordResult(8, "G", "u7", CsvImportCode.ERROR),
            CsvImportRecordResult(9, "H", "u8", CsvImportCode.NOT_PROCESSED),
        ),
        started=True,
        interrupted=True,
    )

    text = _csv_import_summary(result)

    for expected in (
        "Всего строк: 8",
        "Импортировано: 2 (ACTIVE: 1, PENDING: 1)",
        "Дубликатов: 1",
        "Некорректных: 1",
        "Заблокированных: 1",
        "Неоднозначных: 1",
        "Ошибок: 1",
        "Не обработано: 1",
        "Статус: прерван",
    ):
        assert expected in text
