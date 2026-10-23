"""Offline scenarios that pass real ``telegram.Update`` objects through PTB handlers."""
from __future__ import annotations

import asyncio
import json
import logging
from datetime import datetime, timezone
from decimal import Decimal

import pytest
from telegram import Chat, Message, Update, User
from telegram.ext import CallbackQueryHandler, CommandHandler, ExtBot
from telegram.request import BaseRequest

from pricewatch.database import connect, initialize_database
from pricewatch.database.repositories.notification_outbox_repository import NotificationOutboxRepository
from pricewatch.domain.enums import (
    ExtractionMethod, ExtractionStatus, FetchMethod, FetchStatus,
    NotificationOutboxStatus, OfferStatus,
)
from pricewatch.scraping.models import ExtractionResult, FetchResult, PriceCheckResult
from pricewatch.scraping.url_security import UrlSecurity
from pricewatch.services.monitoring_service import MonitoringResult, MonitoringService
from pricewatch.services.product_service import ProductService
from pricewatch.telegram.app import MENU, WELCOME_TEXT, build_application


TOKEN = "123456:" + "offline_test_token_" * 2


def check_result(*, success=True, title="Store title", price="123.45", currency="EUR", fetch_status=FetchStatus.SUCCESS):
    fetch = FetchResult(
        "https://shop.test/item", FetchMethod.REQUESTS, fetch_status,
        "https://shop.test/item", 200 if fetch_status is FetchStatus.SUCCESS else None,
        "<html>", 5, None if fetch_status is FetchStatus.SUCCESS else "network failed",
    )
    extraction = ExtractionResult(
        ExtractionStatus.SUCCESS if success else ExtractionStatus.PARTIAL,
        title=title,
        price=Decimal(price) if success else None,
        currency=currency if success else None,
        method=ExtractionMethod.JSON_LD if success else None,
        confidence_score=0.99 if success else None,
    )
    return PriceCheckResult(fetch, extraction, (fetch,))


class Checker:
    def __init__(self):
        self.result = check_result()
        self.calls = []

    def check(self, url):
        self.calls.append(url)
        return self.result


class Runner:
    def __init__(self, path):
        self.path = path
        self.checker = Checker()
        self.calls = []
        self.monitoring_result = None
        self.security = UrlSecurity(
            lambda host, port: [(None, None, None, None, ("93.184.216.34", 0))]
            if host in {"shop.test", "other.test"} else []
        )

    def __call__(self, path, telegram_id, username, operation):
        self.calls.append(telegram_id)
        connection = connect(self.path)
        try:
            products = ProductService(connection, self.checker, self.security)
            user = products.get_or_create_user(telegram_id, username)
            monitoring = MonitoringService(connection, self.checker)
            if self.monitoring_result is not None:
                fixed = self.monitoring_result
                monitoring.check_for_user = lambda user_id, offer_id: fixed
            return operation(products, monitoring, user.id)
        finally:
            connection.close()

    def direct(self, telegram_id, operation):
        return self(None, telegram_id, "user", operation)


class OfflineTelegramRequest(BaseRequest):
    """Minimal Bot API transport; every outgoing call remains in memory."""
    def __init__(self):
        super().__init__()
        self.calls = []
        self.message_id = 1000

    @property
    def read_timeout(self):
        return 1

    async def initialize(self):
        return None

    async def shutdown(self):
        return None

    async def do_request(self, url, method, request_data=None, **kwargs):
        api_method = url.rsplit("/", 1)[-1]
        params = request_data.parameters if request_data else {}
        self.calls.append((api_method, params))
        if api_method == "getMe":
            result = {"id": 999, "is_bot": True, "first_name": "PriceWatch", "username": "pricewatch_test_bot"}
        elif api_method == "sendMessage":
            self.message_id += 1
            result = {
                "message_id": self.message_id,
                "date": 0,
                "chat": {"id": int(params["chat_id"]), "type": "private"},
                "from": {"id": 999, "is_bot": True, "first_name": "PriceWatch"},
                "text": params["text"],
            }
            if params.get("reply_markup"):
                result["reply_markup"] = params["reply_markup"]
        else:
            result = True
        return 200, json.dumps({"ok": True, "result": result}).encode()

    @property
    def messages(self):
        return [params for method, params in self.calls if method == "sendMessage"]


class OfflineBot(ExtBot):
    """PTB bot object whose send/answer methods never use an HTTP transport."""
    def __init__(self):
        super().__init__(TOKEN)
        self._calls = []
        self._messages = []
        self._message_id = 1000
        self._edit_text_error = None
        self._edit_markup_error = None
        self._on_send_message = None
        self._bot_user = User(999, "PriceWatch", True, username="pricewatch_test_bot")
        self._initialized = True

    async def send_message(self, chat_id, text, reply_markup=None, **kwargs):
        self._message_id += 1
        params = {"chat_id": chat_id, "text": text}
        if reply_markup is not None:
            params["reply_markup"] = reply_markup.to_dict()
        self._calls.append(("sendMessage", params))
        self._messages.append(params)
        if self._on_send_message:
            self._on_send_message(params)
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
        self._calls.append(("answerCallbackQuery", {"callback_query_id": callback_query_id}))
        return True

    async def edit_message_text(self, text, chat_id=None, message_id=None, reply_markup=None, **kwargs):
        if self._edit_text_error:
            raise self._edit_text_error
        params = {
            "chat_id": chat_id, "message_id": message_id, "text": text,
            "reply_markup": reply_markup.to_dict() if reply_markup else None,
        }
        self._calls.append(("editMessageText", params))
        return True

    async def edit_message_reply_markup(self, chat_id=None, message_id=None, reply_markup=None, **kwargs):
        if self._edit_markup_error:
            raise self._edit_markup_error
        self._calls.append(("editMessageReplyMarkup", {
            "chat_id": chat_id, "message_id": message_id,
            "reply_markup": reply_markup.to_dict() if reply_markup else None,
        }))
        return True

    @property
    def calls(self):
        return self._calls

    @property
    def messages(self):
        return self._messages


class Harness:
    def __init__(self, application, request, runner):
        self.app = application
        self.request = request
        self.runner = runner
        self.update_id = 0

    async def text(self, text, *, user_id=1, chat_type="private", with_user=True):
        self.update_id += 1
        message = {
            "message_id": self.update_id,
            "date": 0,
            "chat": {"id": user_id if chat_type == "private" else -100, "type": chat_type},
            "text": text,
        }
        if with_user:
            message["from"] = {"id": user_id, "is_bot": False, "first_name": "User", "username": f"u{user_id}"}
        if text.startswith("/"):
            command = text.split()[0]
            message["entities"] = [{"type": "bot_command", "offset": 0, "length": len(command)}]
        update = Update.de_json({"update_id": self.update_id, "message": message}, self.app.bot)
        await self.app.process_update(update)
        return update

    async def callback(self, data, *, user_id=1, chat_type="private", message_id=None, message_text="screen"):
        self.update_id += 1
        chat_id = user_id if chat_type == "private" else -100
        source_message_id = message_id if message_id is not None else self.update_id
        update = Update.de_json({
            "update_id": self.update_id,
            "callback_query": {
                "id": f"q{self.update_id}",
                "from": {"id": user_id, "is_bot": False, "first_name": "User", "username": f"u{user_id}"},
                "chat_instance": "offline",
                "data": data,
                "message": {
                    "message_id": source_message_id,
                    "date": 0,
                    "chat": {"id": chat_id, "type": chat_type},
                    "from": {"id": 999, "is_bot": True, "first_name": "PriceWatch"},
                    "text": message_text,
                },
            },
        }, self.app.bot)
        await self.app.process_update(update)
        return update

    @property
    def texts(self):
        return [message["text"] for message in self.request.messages]

    @property
    def last_text(self):
        return self.texts[-1]

    def buttons(self, message_index=-1):
        markup = self.request.messages[message_index].get("reply_markup", {})
        return {
            button["text"]: button["callback_data"]
            for row in markup.get("inline_keyboard", []) for button in row
        }

    def mark_latest_outbox_sent(self, message_id):
        connection = connect(self.runner.path)
        try:
            repository = NotificationOutboxRepository(connection)
            ready = repository.list_ready("9999-12-31T23:59:59+00:00")
            queued = ready[-1]
            claimed = repository.claim(
                queued.id,
                "9999-12-31T23:59:58+00:00",
                "9999-12-31T23:59:59+00:00",
            )
            assert claimed is not None
            assert repository.mark_sent(
                queued.id, claimed.lease_token,
                "9999-12-31T23:59:59+00:00", message_id,
            )
            connection.commit()
            return queued
        finally:
            connection.close()


def _mark_initialized(app):
    """PTB process_update guard without calling getMe in the offline harness."""
    app._initialized = True
    app.bot._initialized = True


@pytest.fixture
def ui(tmp_path):
    path = tmp_path / "telegram.db"
    initialize_database(path)
    runner = Runner(path)
    request = OfflineBot()
    app = build_application(TOKEN, frozenset({1, 2}), str(path), service_runner=runner, bot=request, offload_services=False)
    _mark_initialized(app)
    return Harness(app, request, runner)


@pytest.mark.asyncio
async def test_authorization_private_chat_and_all_update_kinds(ui):
    await ui.text("/start")
    assert ui.last_text == WELCOME_TEXT
    assert set(ui.buttons()) == {title for title, _ in MENU}
    calls = len(ui.runner.calls)
    await ui.text("/start", user_id=99)
    await ui.text("/start", chat_type="group")
    await ui.text("hello", with_user=False)
    await ui.callback("menu:products", user_id=99)
    await ui.text("/cancel", user_id=99)
    assert all("Доступ" in text for text in ui.texts[-5:])
    assert len(ui.runner.calls) == calls
    assert any(method == "answerCallbackQuery" for method, _ in ui.request.calls)


MENU_LABELS = ("Мои товары", "Добавить товар", "Настройки", "Помощь")


@pytest.mark.asyncio
async def test_main_menu_navigation_and_home_clears_dialog(ui):
    await ui.text("/start")
    await ui.text("/start")
    assert ui.last_text == WELCOME_TEXT
    assert set(ui.buttons()) == {title for title, _ in MENU}
    await ui.callback("menu:add")
    assert ui.app.user_data[1]["flow"]["kind"] == "new-url"
    await ui.text("/products")
    assert "flow" not in ui.app.user_data[1] and "Товаров пока нет" in ui.last_text
    await ui.callback("menu:add")
    await ui.callback("menu:home")
    assert "flow" not in ui.app.user_data[1]
    assert "Главное меню" in ui.last_text
    await ui.callback("menu:products")
    assert "Товаров пока нет" in ui.last_text
    await ui.callback("menu:settings")
    assert "Порог пользователя: 10%" in ui.last_text
    await ui.callback("menu:help")
    assert ui.last_text == (
        "PriceWatch отслеживает цены по публичным URL. Используйте кнопки, чтобы "
        "добавлять товары и предложения, запускать проверку, менять пороги и "
        "обрабатывать подозрительные цены, импортировать и экспортировать CSV. "
        "/cancel отменяет текущий диалог."
    )


@pytest.mark.asyncio
async def test_cancel_without_active_flow_reports_it_and_shows_menu(ui):
    await ui.text("/cancel")

    assert "Нет активной операции" in ui.last_text
    assert set(MENU_LABELS) <= set(ui.buttons())
    assert "flow" not in ui.app.user_data[1]


@pytest.mark.asyncio
async def test_cancel_button_and_command_return_to_the_flow_origin(ui):
    product = ui.runner.direct(1, lambda p, m, u: p.create_product(u, "Phone").product)
    offer = ui.runner.direct(
        1,
        lambda p, m, u: p.add_offer(u, product.id, "https://shop.test/cancel-offer").offer,
    )

    await ui.callback("menu:add")
    await ui.callback(ui.buttons()["Отмена"])
    assert "Главное меню" in ui.last_text and "flow" not in ui.app.user_data[1]
    await ui.callback("menu:add")
    await ui.text("/cancel")
    assert "Главное меню" in ui.last_text and "flow" not in ui.app.user_data[1]

    for action in (f"add-offer:{product.id}", f"rename:{product.id}"):
        await ui.callback(action)
        await ui.callback(ui.buttons()["Отмена"])
        assert ui.last_text.startswith("Phone\n")
        assert "flow" not in ui.app.user_data[1]
        await ui.callback(action)
        await ui.text("/cancel")
        assert ui.last_text.startswith("Phone\n")
        assert "flow" not in ui.app.user_data[1]

    await ui.callback(f"product-threshold:{product.id}")
    await ui.callback(ui.buttons()["Отмена"])
    assert ui.last_text.startswith("Phone\n") and "flow" not in ui.app.user_data[1]
    await ui.callback(f"product-threshold:{product.id}")
    await ui.text("/cancel")
    assert ui.last_text.startswith("Phone\n") and "flow" not in ui.app.user_data[1]

    await ui.callback("menu:settings")
    await ui.callback("settings:edit")
    await ui.callback(ui.buttons()["Отмена"])
    assert "Порог пользователя" in ui.last_text and "flow" not in ui.app.user_data[1]
    await ui.callback("settings:edit")
    await ui.text("/cancel")
    assert "Порог пользователя" in ui.last_text and "flow" not in ui.app.user_data[1]

    await ui.callback(f"delete-offer:{offer.id}")
    await ui.callback(ui.buttons()["Отмена"])
    assert ui.last_text.startswith("Phone\n")
    assert ui.runner.direct(1, lambda p, m, u: p.get_offer(u, offer.id)) is not None

    await ui.callback(f"delete-product:{product.id}")
    await ui.text("/cancel")
    assert ui.last_text.startswith("Phone\n")
    assert ui.runner.direct(1, lambda p, m, u: p.get_product_details(u, product.id)) is not None


@pytest.mark.asyncio
async def test_cancel_product_return_is_owner_checked_and_has_safe_fallback(ui):
    own = ui.runner.direct(1, lambda p, m, u: p.create_product(u, "Own").product)
    foreign = ui.runner.direct(2, lambda p, m, u: p.create_product(u, "Secret").product)

    await ui.callback(f"rename:{own.id}")
    ui.runner.direct(1, lambda p, m, u: p.delete_product(u, own.id))
    await ui.text("/cancel")
    assert "Товаров пока нет" in ui.last_text
    assert "flow" not in ui.app.user_data[1]

    ui.app.user_data[1]["flow"] = {
        "kind": "rename",
        "operation_id": "internal-test",
        "return_to": ("product", foreign.id),
    }
    await ui.text("/cancel")
    assert "Secret" not in ui.last_text
    assert "Товаров пока нет" in ui.last_text
    assert "flow" not in ui.app.user_data[1]


@pytest.mark.asyncio
async def test_full_create_dialog_source_title_no_write_before_confirmation_and_repeat(ui):
    await ui.callback("menu:add")
    await ui.text("https://shop.test/item?utm_source=x")
    assert "Store title" in ui.last_text
    assert ui.runner.direct(1, lambda p, m, u: p.list_products(u)) == ()
    use_title = ui.buttons()["Использовать"]
    await ui.callback(use_title)
    assert "Название: Store title" in ui.last_text
    confirm = ui.buttons()["Подтвердить"]
    assert ui.runner.direct(1, lambda p, m, u: p.list_products(u)) == ()
    await ui.callback(confirm)
    products = ui.runner.direct(1, lambda p, m, u: p.list_products(u))
    assert len(products) == 1 and products[0].name == "Store title"
    assert any(
        method == "editMessageReplyMarkup" and params["reply_markup"] is None
        for method, params in ui.request.calls
    )
    assert len(ui.runner.checker.calls) == 1
    assert "Эффективный порог" in ui.last_text
    await ui.callback(confirm)
    assert "устарело" in ui.last_text
    assert len(ui.runner.direct(1, lambda p, m, u: p.list_products(u))) == 1


@pytest.mark.asyncio
async def test_create_custom_name_pending_cancel_invalid_unsafe_and_duplicate(ui):
    ui.runner.checker.result = check_result(success=False, title=None)
    await ui.callback("menu:add")
    await ui.text("not a url")
    assert "Некорректные" in ui.last_text and ui.app.user_data[1]["flow"]["kind"] == "new-url"
    await ui.text("http://127.0.0.1/private")
    assert "небезопасен" in ui.last_text
    await ui.text("https://shop.test/pending")
    assert "Введите название" in ui.last_text
    await ui.text("My pending item")
    assert "PENDING" in ui.last_text
    confirm = ui.buttons()["Подтвердить"]
    await ui.callback(confirm)
    details = ui.runner.direct(1, lambda p, m, u: p.get_product_details(u, 1))
    assert details.offers[0].status is OfferStatus.PENDING
    await ui.callback("menu:add")
    await ui.text("https://shop.test/pending")
    assert "уже существует" in ui.last_text
    assert len(ui.runner.checker.calls) == 1
    await ui.text("/cancel")
    assert "Действие отменено" in ui.last_text and "flow" not in ui.app.user_data[1]


@pytest.mark.asyncio
async def test_old_creation_button_cannot_confirm_new_operation(ui):
    await ui.callback("menu:add")
    await ui.text("https://shop.test/a")
    old_title = ui.buttons()["Использовать"]
    await ui.callback("menu:add")
    await ui.text("https://shop.test/b")
    await ui.callback(old_title)
    assert "устарело" in ui.last_text
    assert ui.runner.direct(1, lambda p, m, u: p.list_products(u)) == ()


@pytest.mark.asyncio
async def test_cancel_at_name_and_confirmation_steps_never_writes(ui):
    await ui.callback("menu:add")
    await ui.text("https://shop.test/cancel-name")
    await ui.callback(ui.buttons()["Ввести своё"])
    await ui.text("/cancel")
    assert ui.runner.direct(1, lambda p, m, u: p.list_products(u)) == ()
    await ui.callback("menu:add")
    await ui.text("https://shop.test/cancel-confirm")
    await ui.callback(ui.buttons()["Использовать"])
    await ui.callback("menu:home")
    assert ui.runner.direct(1, lambda p, m, u: p.list_products(u)) == ()


@pytest.mark.asyncio
async def test_two_concurrent_create_confirmations_persist_once(ui):
    await ui.callback("menu:add")
    await ui.text("https://shop.test/concurrent")
    await ui.callback(ui.buttons()["Использовать"])
    confirm = ui.buttons()["Подтвердить"]
    await __import__("asyncio").gather(ui.callback(confirm), ui.callback(confirm))
    products = ui.runner.direct(1, lambda p, m, u: p.list_products(u))
    assert len(products) == 1
    assert any("устарело" in text for text in ui.texts)


@pytest.mark.asyncio
async def test_product_card_rename_threshold_inheritance_and_delete_confirmation(ui):
    product = ui.runner.direct(1, lambda p, m, u: p.create_product(u, "Phone").product)
    await ui.callback(f"product:{product.id}:0")
    assert "Порог товара: наследуется" in ui.last_text and "Эффективный порог: 10%" in ui.last_text
    assert {"К товарам", "Главное меню"} <= set(ui.buttons())
    await ui.callback(f"rename:{product.id}")
    await ui.text("Renamed phone")
    assert "Renamed phone" in ui.last_text
    await ui.callback(f"product-threshold:{product.id}")
    await ui.text("12,5")
    assert "Порог товара: 12.5%" in ui.last_text
    await ui.callback(f"product-threshold:{product.id}")
    inherit = ui.buttons()["Наследовать"]
    await ui.callback(inherit)
    assert "Порог товара: наследуется" in ui.last_text
    await ui.callback(f"delete-product:{product.id}")
    confirm = ui.buttons()["Да, удалить"]
    assert ui.runner.direct(1, lambda p, m, u: p.get_product_details(u, product.id)) is not None
    await ui.callback(confirm)
    assert ui.runner.direct(1, lambda p, m, u: p.get_product_details(u, product.id)) is None
    await ui.callback(confirm)
    assert "устарело" in ui.last_text


@pytest.mark.asyncio
async def test_foreign_product_and_offer_are_never_exposed_or_changed(ui):
    product = ui.runner.direct(2, lambda p, m, u: p.create_product(u, "Secret").product)
    offer = ui.runner.direct(2, lambda p, m, u: p.add_offer(u, product.id, "https://shop.test/secret").offer)
    calls = len(ui.runner.checker.calls)
    for data in (f"product:{product.id}:0", f"add-offer:{product.id}", f"rename:{product.id}", f"offer:{offer.id}", f"check:{offer.id}", f"delete-offer:{offer.id}"):
        await ui.callback(data)
        assert "не найден" in ui.last_text.lower()
    assert len(ui.runner.checker.calls) == calls
    unchanged = ui.runner.direct(2, lambda p, m, u: (p.get_product_details(u, product.id), p.get_offer(u, offer.id)))
    assert unchanged[0].product.name == "Secret" and unchanged[1] is not None


@pytest.mark.asyncio
async def test_add_offer_card_history_manual_check_and_delete(ui):
    product = ui.runner.direct(1, lambda p, m, u: p.create_product(u, "Phone").product)
    await ui.callback(f"add-offer:{product.id}")
    await ui.text("https://shop.test/offer")
    assert "Подтверждённая цена: 123.45 EUR" in ui.last_text
    assert "202" in ui.last_text and "Последняя проверка" in ui.last_text
    assert {"Назад к товару", "Главное меню"} <= set(ui.buttons())
    offer = ui.runner.direct(1, lambda p, m, u: p.get_product_details(u, product.id).offers[0])
    ui.runner.checker.result = check_result(success=False, title=None)
    await ui.callback(f"check:{offer.id}")
    assert any("цена пока не получена" in text for text in ui.texts[-2:])
    await ui.callback(f"delete-offer:{offer.id}")
    confirm = ui.buttons()["Да, удалить"]
    await ui.callback(confirm)
    assert ui.runner.direct(1, lambda p, m, u: p.get_offer(u, offer.id)) is None
    await ui.callback(confirm)
    assert "устарело" in ui.last_text


@pytest.mark.asyncio
async def test_manual_check_clears_rename_flow_before_following_text(ui):
    product = ui.runner.direct(1, lambda p, m, u: p.create_product(u, "Phone").product)
    offer = ui.runner.direct(
        1,
        lambda p, m, u: p.add_offer(u, product.id, "https://shop.test/check-rename").offer,
    )
    await ui.callback(f"rename:{product.id}")

    await ui.text(f"/check {offer.id}")

    assert "flow" not in ui.app.user_data[1]
    await ui.text("Unexpected rename")
    assert "Выберите действие" in ui.last_text
    stored = ui.runner.direct(1, lambda p, m, u: p.get_product_details(u, product.id))
    assert stored.product.name == "Phone"


@pytest.mark.asyncio
async def test_stale_check_button_clears_new_url_flow_before_following_text(ui):
    product = ui.runner.direct(1, lambda p, m, u: p.create_product(u, "Phone").product)
    offer = ui.runner.direct(
        1,
        lambda p, m, u: p.add_offer(u, product.id, "https://shop.test/stale-check").offer,
    )
    await ui.callback("menu:add")
    calls_before = len(ui.runner.checker.calls)

    await ui.callback(f"check:{offer.id}")

    assert "flow" not in ui.app.user_data[1]
    assert len(ui.runner.checker.calls) == calls_before + 1
    await ui.text("https://shop.test/must-not-be-added")
    assert "Выберите действие" in ui.last_text
    assert len(ui.runner.checker.calls) == calls_before + 1
    assert len(ui.runner.direct(1, lambda p, m, u: p.list_products(u))) == 1


@pytest.mark.asyncio
async def test_manual_check_clears_threshold_flow_and_does_not_restore_on_not_found(ui):
    product = ui.runner.direct(1, lambda p, m, u: p.create_product(u, "Phone").product)
    offer = ui.runner.direct(
        1,
        lambda p, m, u: p.add_offer(u, product.id, "https://shop.test/check-threshold").offer,
    )
    await ui.callback(f"product-threshold:{product.id}")
    await ui.text(f"/check {offer.id}")
    assert "flow" not in ui.app.user_data[1]
    await ui.text("25")
    assert "Выберите действие" in ui.last_text
    details = ui.runner.direct(1, lambda p, m, u: p.get_product_details(u, product.id))
    assert details.product.alert_threshold is None

    foreign_product = ui.runner.direct(2, lambda p, m, u: p.create_product(u, "Secret").product)
    foreign_offer = ui.runner.direct(
        2,
        lambda p, m, u: p.add_offer(u, foreign_product.id, "https://shop.test/foreign-check").offer,
    )
    await ui.callback(f"rename:{product.id}")
    await ui.text(f"/check {foreign_offer.id}")
    assert "flow" not in ui.app.user_data[1]
    assert "не найден" in ui.last_text.lower()
    await ui.text("Must not rename")
    assert "Выберите действие" in ui.last_text
    details = ui.runner.direct(1, lambda p, m, u: p.get_product_details(u, product.id))
    assert details.product.name == "Phone"


@pytest.mark.asyncio
@pytest.mark.parametrize("code,expected", [
    ("SUCCESS", "цена подтверждена"),
    ("PENDING", "пока не получена"),
    ("ERROR", "с ошибкой"),
    ("SUSPICIOUS", "подозрительная цена"),
    ("STALE", "Данные изменились"),
    ("SKIPPED", "пропущена"),
    ("NOT_FOUND", "не найдено"),
    ("INTERNAL_ERROR", "внутренней ошибки"),
])
async def test_manual_check_handler_reports_every_service_result(ui, code, expected):
    product = ui.runner.direct(1, lambda p, m, u: p.create_product(u, f"Result {code}").product)
    offer = ui.runner.direct(1, lambda p, m, u: p.add_offer(u, product.id, f"https://shop.test/{code.lower()}").offer)
    ui.runner.monitoring_result = MonitoringResult(code, offer.id)
    await ui.callback(f"check:{offer.id}")
    assert any(expected in text for text in ui.texts[-2:])
    if code == "NOT_FOUND":
        assert "Главное меню" in ui.buttons()


@pytest.mark.asyncio
async def test_global_error_handler_recovers_unhandled_command_without_retry(
    ui, caplog,
):
    calls = []

    async def fail_command(update, context):
        calls.append("once")
        raise RuntimeError(
            "https://private.test/path?token=raw-command-secret user=private-user"
        )

    ui.app.add_handler(CommandHandler("explode", fail_command), group=-1)
    caplog.set_level(logging.INFO, logger="pricewatch.telegram.app")

    await ui.text("/explode")

    assert calls == ["once"]
    assert "Повтор автоматически не выполнялся" in ui.last_text
    messages = "\n".join(record.getMessage() for record in caplog.records)
    assert (
        "event=telegram_unhandled_error update_type=message outcome=error "
        "exception_class=RuntimeError"
    ) in messages
    for forbidden in (
        "raw-command-secret", "private-user", "https://", "Update(",
        "chat_id", "message_id", "user_id",
    ):
        assert forbidden not in messages


@pytest.mark.asyncio
async def test_global_error_handler_recovers_unhandled_callback_without_mutation(
    ui, caplog,
):
    callback_data = "unsafe:token=raw-callback-secret"
    calls = []

    async def fail_callback(update, context):
        calls.append("once")
        raise ValueError("callback exception secret")

    ui.app.add_handler(
        CallbackQueryHandler(fail_callback, pattern="^unsafe:"), group=-1
    )
    caplog.set_level(logging.INFO, logger="pricewatch.telegram.app")
    service_calls = len(ui.runner.calls)

    await ui.callback(callback_data)

    assert calls == ["once"]
    assert len(ui.runner.calls) == service_calls
    assert "Повтор автоматически не выполнялся" in ui.last_text
    messages = "\n".join(record.getMessage() for record in caplog.records)
    assert (
        "event=telegram_unhandled_error update_type=callback outcome=error "
        "exception_class=ValueError"
    ) in messages
    assert callback_data not in messages
    assert "callback exception secret" not in messages
    assert "callback_data" not in messages


@pytest.mark.asyncio
async def test_global_error_handler_contains_recovery_failure_and_no_target(
    ui, caplog,
):
    assert ui.app.error_handlers
    caplog.set_level(logging.INFO, logger="pricewatch.telegram.app")

    def fail_send(params):
        raise RuntimeError("recovery transport secret")

    ui.request._on_send_message = fail_send
    update = Update.de_json(
        {
            "update_id": 9001,
            "message": {
                "message_id": 8001,
                "date": 0,
                "chat": {"id": 1, "type": "private"},
                "from": {
                    "id": 1,
                    "is_bot": False,
                    "first_name": "User",
                    "username": "private_username",
                },
                "text": "/secret-command",
                "entities": [
                    {"type": "bot_command", "offset": 0, "length": 15}
                ],
            },
        },
        ui.app.bot,
    )
    await ui.app.process_error(
        update=update, error=RuntimeError("unhandled token secret")
    )
    message_count = len(ui.request.messages)
    await ui.app.process_error(
        update=Update(update_id=9002),
        error=LookupError("targetless secret"),
    )

    assert len(ui.request.messages) == message_count
    messages = "\n".join(record.getMessage() for record in caplog.records)
    assert (
        "event=telegram_recovery_failed action=recovery_screen_delivery "
        "outcome=error exception_class=RuntimeError"
    ) in messages
    assert (
        "event=telegram_unhandled_error update_type=unsupported outcome=error "
        "exception_class=LookupError"
    ) in messages
    for forbidden in (
        "recovery transport secret", "unhandled token secret", "targetless secret",
        "private_username", "/secret-command", "8001", "9001",
    ):
        assert forbidden not in messages


@pytest.mark.asyncio
async def test_confirmation_keyboards_are_removed_on_cancel_and_delete(ui):
    product = ui.runner.direct(1, lambda p, m, u: p.create_product(u, "Phone").product)
    offer = ui.runner.direct(
        1,
        lambda p, m, u: p.add_offer(u, product.id, "https://shop.test/remove-buttons").offer,
    )

    await ui.callback(f"delete-offer:{offer.id}")
    cancel = ui.buttons()["Отмена"]
    edits_before = len([call for call in ui.request.calls if call[0] == "editMessageReplyMarkup"])
    await ui.callback(cancel)
    edits_after = [call for call in ui.request.calls if call[0] == "editMessageReplyMarkup"]
    assert len(edits_after) == edits_before + 1
    assert edits_after[-1][1]["reply_markup"] is None
    assert ui.runner.direct(1, lambda p, m, u: p.get_offer(u, offer.id)) is not None

    await ui.callback(f"delete-offer:{offer.id}")
    confirm = ui.buttons()["Да, удалить"]
    await ui.callback(confirm)
    assert [call for call in ui.request.calls if call[0] == "editMessageReplyMarkup"][-1][1]["reply_markup"] is None
    assert ui.last_text.startswith("Phone\n")
    assert "Главное меню" in ui.buttons()
    await ui.callback(confirm)
    assert "устарело" in ui.last_text
    assert ui.runner.direct(1, lambda p, m, u: p.get_offer(u, offer.id)) is None


@pytest.mark.asyncio
async def test_confirmation_edit_failure_does_not_repeat_create(ui):
    await ui.callback("menu:add")
    await ui.text("https://shop.test/edit-create")
    await ui.callback(ui.buttons()["Использовать"])
    confirm = ui.buttons()["Подтвердить"]
    ui.request._edit_markup_error = RuntimeError("Telegram edit failed")

    await ui.callback(confirm)
    await ui.callback(confirm)

    products = ui.runner.direct(1, lambda p, m, u: p.list_products(u))
    assert len(products) == 1
    assert "устарело" in ui.last_text


@pytest.mark.asyncio
async def test_delete_offer_falls_back_when_parent_disappears(ui, monkeypatch):
    product = ui.runner.direct(1, lambda p, m, u: p.create_product(u, "Transient").product)
    offer = ui.runner.direct(
        1,
        lambda p, m, u: p.add_offer(u, product.id, "https://shop.test/transient-parent").offer,
    )
    original_delete_offer = ProductService.delete_offer

    def delete_offer_and_parent(service, user_id, offer_id):
        stored = service.get_offer(user_id, offer_id)
        result = original_delete_offer(service, user_id, offer_id)
        if result.code == "DELETED" and stored is not None:
            service.delete_product(user_id, stored.product_id)
        return result

    monkeypatch.setattr(ProductService, "delete_offer", delete_offer_and_parent)
    await ui.callback(f"delete-offer:{offer.id}")
    await ui.callback(ui.buttons()["Да, удалить"])

    assert "Товаров пока нет" in ui.last_text
    assert "Главное меню" in ui.buttons()


@pytest.mark.asyncio
async def test_add_offer_url_errors_keep_flow_and_cancel_returns_product(ui):
    product = ui.runner.direct(1, lambda p, m, u: p.create_product(u, "Target").product)
    other = ui.runner.direct(1, lambda p, m, u: p.create_product(u, "Other").product)
    existing = "https://shop.test/existing-offer"
    ui.runner.direct(1, lambda p, m, u: p.add_offer(u, other.id, existing))
    await ui.callback(f"add-offer:{product.id}")
    operation_id = ui.app.user_data[1]["flow"]["operation_id"]

    await ui.text("not-a-url")
    assert ui.app.user_data[1]["flow"]["kind"] == "offer-url"
    assert ui.app.user_data[1]["flow"]["operation_id"] == operation_id
    assert {"Отмена", "Главное меню"} <= set(ui.buttons())

    await ui.text(existing)
    assert "уже существует" in ui.last_text
    assert ui.app.user_data[1]["flow"]["kind"] == "offer-url"
    await ui.callback(ui.buttons()["Отмена"])
    assert ui.last_text.startswith("Target\n")
    assert "flow" not in ui.app.user_data[1]


@pytest.mark.asyncio
async def test_stale_malformed_and_unknown_command_have_recovery_navigation(ui):
    await ui.callback("flow:create:missing-operation")
    assert "устарело" in ui.last_text and "Главное меню" in ui.buttons()
    await ui.callback("broken:callback:shape")
    assert "Неизвестное" in ui.last_text and "Главное меню" in ui.buttons()
    await ui.text("/does-not-exist")
    assert "Неизвестная команда" in ui.last_text
    assert set(MENU_LABELS) <= set(ui.buttons())


@pytest.mark.asyncio
async def test_delete_failure_is_not_reported_as_success(
    ui, monkeypatch, caplog,
):
    product = ui.runner.direct(1, lambda p, m, u: p.create_product(u, "Failure").product)

    def fail_delete(service, user_id, product_id):
        raise RuntimeError("database temporarily unavailable")

    monkeypatch.setattr(ProductService, "delete_product", fail_delete)
    caplog.set_level(logging.INFO, logger="pricewatch.telegram.app")
    await ui.callback(f"delete-product:{product.id}")
    confirm = ui.buttons()["Да, удалить"]
    await ui.callback(confirm)

    assert "Удалено." not in ui.texts[-1]
    assert "временной ошибки" in ui.last_text
    event_log = next(
        record.getMessage() for record in caplog.records
        if "action=service_action" in record.getMessage()
    )
    assert event_log == (
        "event=telegram_callback action=service_action outcome=error "
        "exception_class=RuntimeError"
    )
    assert "database temporarily unavailable" not in event_log
    assert "callback_data" not in event_log
    assert "chat_id" not in event_log and "message_id" not in event_log
    assert "Главное меню" in ui.buttons()
    await ui.callback(confirm)
    assert "устарело" in ui.last_text


@pytest.mark.asyncio
@pytest.mark.parametrize("bad_value", ["abc", "-1", "101", "NaN"])
async def test_settings_reject_invalid_values_without_losing_dialog(ui, bad_value):
    await ui.callback("menu:settings")
    await ui.callback("settings:edit")
    await ui.text(bad_value)
    assert "Введите число" in ui.last_text
    assert ui.app.user_data[1]["flow"]["kind"] == "user-threshold"
    assert ui.runner.direct(1, lambda p, m, u: p.get_user(u).alert_threshold) == 10


@pytest.mark.asyncio
async def test_settings_accept_decimal_comma_and_persist(ui):
    await ui.callback("menu:settings")
    await ui.callback("settings:edit")
    await ui.text("7,25")
    assert "Порог пользователя: 7.25%" in ui.last_text
    assert ui.runner.direct(1, lambda p, m, u: p.get_user(u).alert_threshold) == 7.25


@pytest.mark.asyncio
async def test_products_and_offers_pagination_clamps_after_deletion(ui):
    for number in range(23):
        ui.runner.direct(1, lambda p, m, u, n=number: p.create_product(u, f"Product {n:02}").product)
    await ui.callback("menu:products")
    assert "1–10 из 23" in ui.last_text and "Следующая →" in ui.buttons()
    await ui.callback("products-page:2")
    assert "21–23 из 23" in ui.last_text and "Следующая →" not in ui.buttons()
    for product_id in range(12, 24):
        ui.runner.direct(1, lambda p, m, u, pid=product_id: p.delete_product(u, pid))
    await ui.callback("products-page:2")
    assert "1–10 из 11" in ui.last_text or "11–11 из 11" in ui.last_text
    product = ui.runner.direct(1, lambda p, m, u: p.list_products(u)[0])
    for number in range(12):
        ui.runner.direct(1, lambda p, m, u, n=number: p.add_offer(u, product.id, f"https://shop.test/page-{n}"))
    await ui.callback(f"product:{product.id}:0")
    assert "Следующая →" in ui.buttons()
    await ui.callback(f"product:{product.id}:1")
    assert "← Предыдущая" in ui.buttons() and "Следующая →" not in ui.buttons()
    offers = ui.runner.direct(1, lambda p, m, u: p.get_product_details(u, product.id).offers)
    for offer in offers[8:]:
        ui.runner.direct(1, lambda p, m, u, oid=offer.id: p.delete_offer(u, oid))
    await ui.callback(f"product:{product.id}:1")
    assert "Предложений: 8" in ui.last_text and "← Предыдущая" not in ui.buttons()


@pytest.mark.asyncio
async def test_anomaly_callbacks_confirm_reject_repeat_and_stale(ui):
    product = ui.runner.direct(1, lambda p, m, u: p.create_product(u, "Anomaly").product)
    offer = ui.runner.direct(1, lambda p, m, u: p.add_offer(u, product.id, "https://shop.test/anomaly").offer)
    ui.runner.checker.result = check_result(price="40.00")
    suspicious = ui.runner.direct(1, lambda p, m, u: m.check_for_user(u, offer.id))
    pending_a = ui.runner.direct(1, lambda p, m, u: p.get_offer(u, offer.id).pending_check_attempt_id)
    assert suspicious.code == "SUSPICIOUS"
    ui.mark_latest_outbox_sent(701)
    ui.runner.checker.result = check_result(price="30.00")
    ui.runner.direct(1, lambda p, m, u: m.check_for_user(u, offer.id))
    pending_b = ui.runner.direct(1, lambda p, m, u: p.get_offer(u, offer.id).pending_check_attempt_id)
    ui.mark_latest_outbox_sent(702)
    await ui.callback(f"anomaly:confirm:{offer.id}:{pending_a}", message_id=701)
    assert "устарела" in ui.last_text
    stale_edit = [
        params for method, params in ui.request.calls
        if method == "editMessageReplyMarkup"
    ][-1]
    stale_labels = {
        button["text"]
        for row in stale_edit["reply_markup"]["inline_keyboard"]
        for button in row
    }
    assert stale_labels == {"Открыть предложение", "Главное меню"}
    assert ui.runner.direct(1, lambda p, m, u: p.get_offer(u, offer.id).pending_check_attempt_id) == pending_b
    await ui.callback(
        f"anomaly:reject:{offer.id}:{pending_b}", message_id=702,
        message_text="⚠️ Обнаружено необычное изменение цены",
    )
    edit = [params for method, params in ui.request.calls if method == "editMessageText"][-1]
    edit_labels = {
        button["text"]
        for row in edit["reply_markup"]["inline_keyboard"]
        for button in row
    }
    assert "Цена отклонена" in edit["text"]
    assert edit_labels == {"Открыть предложение", "Главное меню"}
    await ui.callback(f"anomaly:reject:{offer.id}:{pending_b}", message_id=702)
    assert "уже обработана" in ui.last_text


@pytest.mark.asyncio
async def test_anomaly_confirm_callback_updates_card_and_repeat_is_safe(ui):
    product = ui.runner.direct(1, lambda p, m, u: p.create_product(u, "Confirm anomaly").product)
    offer = ui.runner.direct(1, lambda p, m, u: p.add_offer(u, product.id, "https://shop.test/confirm-anomaly").offer)
    ui.runner.checker.result = check_result(price="40.00")
    ui.runner.direct(1, lambda p, m, u: m.check_for_user(u, offer.id))
    attempt = ui.runner.direct(1, lambda p, m, u: p.get_offer(u, offer.id).pending_check_attempt_id)
    ui.mark_latest_outbox_sent(703)
    await ui.callback(
        f"anomaly:confirm:{offer.id}:{attempt}", message_id=703,
        message_text="⚠️ Уведомление\nЦена требует подтверждения.",
    )
    edit = [params for method, params in ui.request.calls if method == "editMessageText"][-1]
    assert "Цена подтверждена" in edit["text"] and "Цена требует подтверждения" in edit["text"]
    callbacks = {
        button["text"]: button["callback_data"]
        for row in edit["reply_markup"]["inline_keyboard"]
        for button in row
    }
    assert callbacks == {
        "Открыть предложение": f"offer:{offer.id}",
        "Главное меню": "menu:home",
    }
    confirmed = ui.runner.direct(1, lambda p, m, u: p.get_offer(u, offer.id))
    history = ui.runner.direct(1, lambda p, m, u: p.list_offer_history(u, offer.id, 10))
    assert confirmed.current_price_minor == 4000 and confirmed.pending_price_minor is None
    assert [record.price_minor for record in history].count(4000) == 1
    await ui.callback(f"anomaly:confirm:{offer.id}:{attempt}", message_id=703)
    history = ui.runner.direct(1, lambda p, m, u: p.list_offer_history(u, offer.id, 10))
    assert "уже обработана" in ui.last_text and [record.price_minor for record in history].count(4000) == 1


@pytest.mark.asyncio
async def test_anomaly_callback_rejects_forged_message_before_business_action(ui):
    product = ui.runner.direct(1, lambda p, m, u: p.create_product(u, "Forged anomaly").product)
    offer = ui.runner.direct(1, lambda p, m, u: p.add_offer(u, product.id, "https://shop.test/forged").offer)
    ui.runner.checker.result = check_result(price="40.00")
    ui.runner.direct(1, lambda p, m, u: m.check_for_user(u, offer.id))
    attempt = ui.runner.direct(1, lambda p, m, u: p.get_offer(u, offer.id).pending_check_attempt_id)
    ui.mark_latest_outbox_sent(704)
    await ui.callback(f"offer:{offer.id}")
    assert "Подтвердить аномалию" not in ui.buttons()

    await ui.callback(f"anomaly:confirm:{offer.id}:{attempt}", message_id=999)

    unchanged = ui.runner.direct(1, lambda p, m, u: p.get_offer(u, offer.id))
    assert "не связана" in ui.last_text
    assert unchanged.pending_check_attempt_id == attempt
    assert not any(method == "editMessageText" for method, _ in ui.request.calls)

    await ui.callback(f"anomaly:confirm:{offer.id}:{attempt}", user_id=2, message_id=704)
    unchanged = ui.runner.direct(1, lambda p, m, u: p.get_offer(u, offer.id))
    assert "не связана" in ui.last_text
    assert unchanged.pending_check_attempt_id == attempt


@pytest.mark.asyncio
async def test_anomaly_edit_failure_does_not_repeat_business_decision(ui, caplog):
    product = ui.runner.direct(1, lambda p, m, u: p.create_product(u, "Edit failure").product)
    offer = ui.runner.direct(1, lambda p, m, u: p.add_offer(u, product.id, "https://shop.test/edit-failure").offer)
    ui.runner.checker.result = check_result(price="40.00")
    ui.runner.direct(1, lambda p, m, u: m.check_for_user(u, offer.id))
    attempt = ui.runner.direct(1, lambda p, m, u: p.get_offer(u, offer.id).pending_check_attempt_id)
    ui.request._edit_text_error = RuntimeError("Telegram edit failed")

    telegram_message_id = 987654321
    ui.mark_latest_outbox_sent(telegram_message_id)
    with caplog.at_level(logging.ERROR, logger="pricewatch.telegram.app"):
        await ui.callback(
            f"anomaly:confirm:{offer.id}:{attempt}", message_id=telegram_message_id
        )

    stored = ui.runner.direct(1, lambda p, m, u: p.get_offer(u, offer.id))
    history = ui.runner.direct(1, lambda p, m, u: p.list_offer_history(u, offer.id, 10))
    assert stored.current_price_minor == 4000 and stored.pending_price_minor is None
    assert [record.price_minor for record in history].count(4000) == 1
    assert "изменить не удалось" in ui.last_text
    assert {"Открыть предложение", "Главное меню"} <= set(ui.buttons())
    event_logs = [
        record.getMessage()
        for record in caplog.records
        if "action=anomaly_message_edit" in record.getMessage()
    ]
    assert event_logs == [
        "event=telegram_callback action=anomaly_message_edit "
        f"outcome=error offer_id={offer.id} exception_class=RuntimeError"
    ]
    assert "chat_id" not in event_logs[0]
    assert "message_id" not in event_logs[0]
    assert str(telegram_message_id) not in event_logs[0]
    assert "Telegram edit failed" not in event_logs[0]

    await ui.callback(
        f"anomaly:confirm:{offer.id}:{attempt}", message_id=telegram_message_id
    )

    history = ui.runner.direct(1, lambda p, m, u: p.list_offer_history(u, offer.id, 10))
    assert "уже обработана" in ui.last_text
    assert [record.price_minor for record in history].count(4000) == 1


@pytest.mark.asyncio
async def test_notification_offer_navigation_is_owner_scoped_and_clears_flow(ui):
    own_product = ui.runner.direct(1, lambda p, m, u: p.create_product(u, "Own").product)
    own_offer = ui.runner.direct(
        1,
        lambda p, m, u: p.add_offer(u, own_product.id, "https://shop.test/notification-own").offer,
    )
    foreign_product = ui.runner.direct(2, lambda p, m, u: p.create_product(u, "Secret").product)
    foreign_offer = ui.runner.direct(
        2,
        lambda p, m, u: p.add_offer(u, foreign_product.id, "https://shop.test/notification-secret").offer,
    )
    await ui.callback(f"rename:{own_product.id}")
    checks_before = len(ui.runner.checker.calls)

    await ui.callback(f"offer:{own_offer.id}")

    assert "flow" not in ui.app.user_data[1]
    assert "notification-own" in ui.last_text
    assert "Главное меню" in ui.buttons()
    assert len(ui.runner.checker.calls) == checks_before

    await ui.callback(f"offer:{foreign_offer.id}")

    assert "Secret" not in ui.last_text
    assert "не найден" in ui.last_text.lower()
    assert "Главное меню" in ui.buttons()


@pytest.mark.asyncio
async def test_deleted_offer_from_old_notification_has_safe_recovery(ui):
    product = ui.runner.direct(1, lambda p, m, u: p.create_product(u, "Old notice").product)
    offer = ui.runner.direct(
        1,
        lambda p, m, u: p.add_offer(u, product.id, "https://shop.test/deleted-notice").offer,
    )
    ui.runner.direct(1, lambda p, m, u: p.delete_offer(u, offer.id))

    await ui.callback(f"offer:{offer.id}")

    assert "не найден" in ui.last_text.lower()
    assert "Главное меню" in ui.buttons()


@pytest.mark.asyncio
async def test_manual_check_wakes_delivery_only_when_event_created(tmp_path):
    path = tmp_path / "wakeup.db"
    initialize_database(path)
    runner = Runner(path)
    bot = OfflineBot()
    wakeups = []

    async def wakeup():
        wakeups.append(True)

    app = build_application(
        TOKEN, frozenset({1}), str(path), service_runner=runner, bot=bot,
        offload_services=False, notification_wakeup=wakeup,
    )
    _mark_initialized(app)
    harness = Harness(app, bot, runner)
    product = runner.direct(1, lambda p, m, u: p.create_product(u, "Wakeup").product)
    offer = runner.direct(1, lambda p, m, u: p.add_offer(u, product.id, "https://shop.test/wakeup").offer)
    await harness.callback(f"check:{offer.id}")
    assert wakeups == []
    runner.checker.result = check_result(price="40.00")

    await harness.callback(f"check:{offer.id}")

    assert wakeups == [True]
    assert "уведомление поставлено" in harness.texts[-2].lower()


@pytest.mark.asyncio
async def test_manual_check_sends_summary_before_wakeup_signal(tmp_path):
    path = tmp_path / "wakeup-order.db"
    initialize_database(path)
    runner = Runner(path)
    bot = OfflineBot()
    events = []

    def record_summary(params):
        if "Уведомление поставлено в очередь" in params["text"]:
            events.append("summary_sent")

    async def wakeup():
        events.append("wakeup_called")

    bot._on_send_message = record_summary
    app = build_application(
        TOKEN, frozenset({1}), str(path), service_runner=runner, bot=bot,
        offload_services=False, notification_wakeup=wakeup,
    )
    _mark_initialized(app)
    harness = Harness(app, bot, runner)
    product = runner.direct(1, lambda p, m, u: p.create_product(u, "Wakeup order").product)
    offer = runner.direct(1, lambda p, m, u: p.add_offer(u, product.id, "https://shop.test/wakeup-order").offer)
    runner.checker.result = check_result(price="40.00")

    await harness.callback(f"check:{offer.id}")

    assert events == ["summary_sent", "wakeup_called"]


@pytest.mark.asyncio
async def test_manual_check_wakeup_failure_keeps_summary_and_outbox_event(tmp_path):
    path = tmp_path / "wakeup-failure.db"
    initialize_database(path)
    runner = Runner(path)
    bot = OfflineBot()

    async def failing_wakeup():
        raise RuntimeError("signal unavailable")

    app = build_application(
        TOKEN, frozenset({1}), str(path), service_runner=runner, bot=bot,
        offload_services=False, notification_wakeup=failing_wakeup,
    )
    _mark_initialized(app)
    harness = Harness(app, bot, runner)
    product = runner.direct(1, lambda p, m, u: p.create_product(u, "Wakeup failure").product)
    offer = runner.direct(1, lambda p, m, u: p.add_offer(u, product.id, "https://shop.test/wakeup-failure").offer)
    runner.checker.result = check_result(price="40.00")

    await harness.callback(f"check:{offer.id}")

    assert any("Уведомление поставлено в очередь" in text for text in harness.texts)
    connection = connect(path)
    try:
        ready = NotificationOutboxRepository(connection).list_ready("9999-12-31T23:59:59+00:00")
    finally:
        connection.close()
    assert len(ready) == 1
    assert ready[0].status is NotificationOutboxStatus.PENDING


@pytest.mark.asyncio
async def test_production_path_offloads_service_work(monkeypatch, tmp_path):
    path = tmp_path / "offload.db"
    initialize_database(path)
    runner = Runner(path)
    bot = OfflineBot()
    calls = []

    async def inline_thread(function, *args):
        calls.append(function)
        return function(*args)

    monkeypatch.setattr("pricewatch.telegram.app.run_in_daemon_thread", inline_thread)
    app = build_application(TOKEN, frozenset({1}), str(path), service_runner=runner, bot=bot)
    _mark_initialized(app)
    harness = Harness(app, bot, runner)
    await harness.text("/start")
    assert calls and harness.last_text == WELCOME_TEXT


@pytest.mark.asyncio
async def test_malformed_callback_and_restart_state_are_safe(ui, tmp_path):
    await ui.callback("nonsense:abc")
    assert "Неизвестное" in ui.last_text
    await ui.callback("products-page:not-a-number")
    assert "Неизвестное" in ui.last_text
    await ui.callback("menu:add")
    assert "flow" in ui.app.user_data[1]
    request = OfflineBot()
    restarted = build_application(TOKEN, frozenset({1, 2}), str(ui.runner.path), service_runner=ui.runner, bot=request, offload_services=False)
    _mark_initialized(restarted)
    fresh = Harness(restarted, request, ui.runner)
    await fresh.text("orphan message")
    assert "Выберите действие" in fresh.last_text
    assert "flow" not in restarted.user_data[1]
