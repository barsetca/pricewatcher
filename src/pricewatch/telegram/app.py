"""Telegram presentation layer for the local PriceWatch bot.

Every callback is authorized before it reaches an application service. Blocking
SQLite, DNS and HTTP work is dispatched to a worker thread with a fresh SQLite
connection. Dialog state is kept only in PTB ``user_data`` and is therefore reset
on process restart.
"""
from __future__ import annotations

import asyncio
import io
import inspect
import logging
import secrets
import time
from decimal import Decimal, InvalidOperation

from pricewatch.async_utils import run_in_daemon_thread
from pricewatch.database.connection import connect
from pricewatch.scraping.price_checker import PriceChecker
from pricewatch.scraping.requests_fetcher import RequestsFetcher
from pricewatch.scraping.robots_policy import RobotsPolicy
from pricewatch.scraping.universal_extractor import UniversalExtractor
from pricewatch.services.monitoring_service import EventType, MonitoringService
from pricewatch.services.csv_service import (
    MAX_CSV_SIZE_BYTES,
    CsvErrorCode,
    CsvExportService,
    CsvImportCode,
    CsvImportService,
    CsvService,
)
from pricewatch.services.product_service import ProductService
from pricewatch.telegram.auth import access_error

LOG = logging.getLogger(__name__)
PAGE_SIZE = 10
FAILED = object()
CSV_RUNNING_KIND = "csv-import-running"
RETURN_HOME = ("home",)
RETURN_PRODUCTS = ("products",)
RETURN_SETTINGS = ("settings",)
TEXT_FLOW_PROMPTS = {
    "new-url": "Отправьте URL товара или /cancel.",
    "new-name": "Введите название товара или /cancel.",
    "offer-url": "Отправьте URL предложения или /cancel.",
    "rename": "Отправьте новое название или /cancel.",
    "user-threshold": "Введите новый порог от 0 до 100 или /cancel.",
    "product-threshold": (
        "Введите порог от 0 до 100 или используйте кнопку «Наследовать» "
        "на предыдущем сообщении."
    ),
}
MENU = (
    ("Мои товары", "menu:products"),
    ("Добавить товар", "menu:add"),
    ("Импорт CSV", "menu:csv-import"),
    ("Экспорт CSV", "menu:csv-export"),
    ("Настройки", "menu:settings"),
    ("Помощь", "menu:help"),
)
CSV_IMPORT_NAVIGATION = (
    ("Мои товары", "menu:products"),
    ("Импортировать еще", "menu:csv-import"),
    ("Главное меню", "menu:home"),
)
CSV_EXPORT_NAVIGATION = (
    ("Мои товары", "menu:products"),
    ("Главное меню", "menu:home"),
)
CSV_RESTART_NAVIGATION = (
    ("Импорт CSV", "menu:csv-import"),
    ("Главное меню", "menu:home"),
)
HOME_NAVIGATION = (("Главное меню", "menu:home"),)
WELCOME_TEXT = """👋 Добро пожаловать в PriceWatch!

🔎 PriceWatch — бот для автоматического отслеживания цен в интернет-магазинах.

Что умеет бот:

📦 Мониторинг товаров — добавляйте товары и ссылки на предложения разных магазинов.

💰 История цен — смотрите, как менялась стоимость товара.

🔔 Уведомления — получайте сообщения о снижении цены и проблемах при проверке.

⚙️ Управление — настраивайте пороги снижения цены, запускайте проверку вручную и подтверждайте подозрительные изменения.

📄 CSV — импортируйте и экспортируйте список товаров и предложений.

Как начать?

Нажмите «Добавить товар», укажите ссылку на страницу товара в интернет-магазине и следуйте подсказкам бота.

После добавления PriceWatch будет автоматически проверять цену. Вы также можете запустить проверку вручную.

👇 Выберите действие в меню ниже.

ℹ️ Команда /cancel отменяет текущий диалог.
""".strip()


def build_application(
    token: str,
    allowed_user_ids: frozenset[int],
    database_path: str,
    *,
    service_runner=None,
    request=None,
    bot=None,
    offload_services=True,
    notification_wakeup=None,
):
    """Build but never start a PTB application.

    ``service_runner`` and ``request`` are explicit offline-test seams. Production
    callers use the defaults and do not need Telegram credentials during import.
    """
    from telegram.ext import (
        Application,
        ApplicationHandlerStop,
        CallbackQueryHandler,
        CommandHandler,
        MessageHandler,
        filters,
    )

    builder = Application.builder()
    if bot is not None:
        builder = builder.bot(bot)
    else:
        builder = builder.token(token)
    if request is not None and bot is None:
        builder = builder.request(request)
    application = builder.build()
    call_services = service_runner or _run_service

    def keyboard(rows=MENU):
        from telegram import InlineKeyboardButton, InlineKeyboardMarkup
        return InlineKeyboardMarkup(
            [[InlineKeyboardButton(title, callback_data=data)] for title, data in rows]
        )

    def notification_navigation(offer_id, *, include_offer=True):
        rows = []
        if include_offer:
            rows.append(("Открыть предложение", f"offer:{int(offer_id)}"))
        rows.append(("Главное меню", "menu:home"))
        return tuple(rows)

    async def reply(update, text, rows=None):
        message = update.effective_message
        if message is not None:
            return await message.reply_text(
                text,
                reply_markup=keyboard(rows) if rows is not None else None,
                disable_web_page_preview=True,
            )
        return None

    async def reply_document(update, content, filename):
        message = update.effective_message
        if message is None:
            return None
        document = io.BytesIO(content)
        document.name = filename
        return await message.reply_document(document=document, filename=filename)

    async def recovery_screen(update, text, rows=HOME_NAVIGATION):
        """Show a safe next step without exposing internal failure details."""
        try:
            await reply(update, text, rows)
        except Exception as error:
            LOG.error(
                "event=telegram_recovery_failed action=recovery_screen_delivery "
                "outcome=error exception_class=%s",
                type(error).__name__,
            )

    async def global_error_handler(update, context):
        """Last-resort diagnostics and recovery for uncaught PTB handler errors."""
        error = context.error
        LOG.error(
            "event=telegram_unhandled_error update_type=%s outcome=error "
            "exception_class=%s",
            _telegram_update_type(update),
            type(error).__name__,
        )
        if update is None or getattr(update, "effective_message", None) is None:
            raise ApplicationHandlerStop
        try:
            await recovery_screen(
                update,
                "Не удалось обработать действие из-за временной ошибки. "
                "Повтор автоматически не выполнялся.",
            )
        finally:
            raise ApplicationHandlerStop

    async def remove_confirmation_keyboard(update):
        """Best-effort cleanup after the callback state transition is accepted."""
        query = update.callback_query
        if query is None or query.message is None:
            return
        try:
            await query.edit_message_reply_markup(reply_markup=None)
        except Exception as error:
            # The operation state has already moved forward. Never retry the
            # business action just because Telegram could not edit its message.
            LOG.error(
                "event=telegram_callback action=confirmation_keyboard_cleanup "
                "outcome=error exception_class=%s",
                type(error).__name__,
            )

    async def authorized(update, *, answer_callback=False):
        if answer_callback and update.callback_query:
            await update.callback_query.answer()
        error = access_error(update, allowed_user_ids)
        if error:
            await reply(update, error)
            return False
        return True

    async def run(update, operation, *, report_error=True, on_error=None):
        """Authorize and invoke application services in a worker thread."""
        if not await authorized(update):
            return FAILED
        try:
            user = update.effective_user
            if offload_services:
                return await run_in_daemon_thread(
                    call_services, database_path, user.id, user.username, operation
                )
            return call_services(database_path, user.id, user.username, operation)
        except Exception as error:
            if on_error is None:
                if update.callback_query is not None:
                    LOG.error(
                        "event=telegram_callback action=service_action "
                        "outcome=error exception_class=%s",
                        type(error).__name__,
                    )
                else:
                    LOG.exception("Telegram service action failed")
            else:
                on_error(error)
            if report_error:
                await recovery_screen(
                    update,
                    "Не удалось подтвердить результат действия из-за временной ошибки. "
                    "Повтор автоматически не выполнялся.",
                )
            return FAILED

    async def wake_notifications():
        """Опциональная точка M9.5 для немедленного запуска Outbox worker."""
        if notification_wakeup is None:
            return
        try:
            outcome = notification_wakeup()
            if inspect.isawaitable(outcome):
                await outcome
        except Exception:
            # Monitoring transaction уже зафиксирована, событие остаётся в
            # Outbox и будет доступно следующему штатному циклу доставки.
            LOG.exception("Immediate notification wakeup failed")

    def clear_flow(context):
        context.user_data.pop("flow", None)

    def clear_flow_if_current(context, operation_id):
        flow = context.user_data.get("flow")
        if flow and flow.get("operation_id") == operation_id:
            clear_flow(context)

    def new_flow(context, kind, *, return_to=RETURN_HOME, **values):
        flow = {
            "kind": kind,
            "operation_id": _operation_id(),
            "return_to": return_to,
            **values,
        }
        context.user_data["flow"] = flow
        return flow

    def product_return(product_id):
        return ("product", int(product_id))

    async def show_menu(update, context, text="Главное меню:"):
        clear_flow(context)
        await reply(update, text, MENU)

    async def navigate_to_return(update, context, return_to, *, home_text="Главное меню:"):
        """Navigate only to an explicitly supported internal destination."""
        clear_flow(context)
        if return_to == RETURN_SETTINGS:
            await settings_screen(update, context)
            return
        if return_to == RETURN_PRODUCTS:
            await products_screen(update, context)
            return
        if (
            isinstance(return_to, tuple)
            and len(return_to) == 2
            and return_to[0] == "product"
            and isinstance(return_to[1], int)
            and return_to[1] > 0
        ):
            if not await product_screen(update, context, return_to[1]):
                await products_screen(update, context)
            return
        await show_menu(update, context, home_text)

    async def cancel_flow(update, context, *, home_text="Действие отменено.\n\nГлавное меню:"):
        flow = context.user_data.get("flow")
        return_to = flow.get("return_to", RETURN_HOME) if flow else RETURN_HOME
        await navigate_to_return(update, context, return_to, home_text=home_text)

    async def start(update, context):
        if context.user_data.get("flow", {}).get("kind") == CSV_RUNNING_KIND:
            if await authorized(update):
                await reply(update, "Импорт уже выполняется. Дождитесь завершения.")
            return
        clear_flow(context)
        user = await run(update, lambda products, monitoring, user_id: products.get_user(user_id))
        if user is not FAILED:
            await reply(update, WELCOME_TEXT, MENU)

    async def help_screen(update, context):
        if not await authorized(update):
            return
        if context.user_data.get("flow", {}).get("kind") == CSV_RUNNING_KIND:
            await reply(update, "Импорт уже выполняется. Дождитесь завершения.")
            return
        clear_flow(context)
        await reply(
            update,
            "PriceWatch отслеживает цены по публичным URL. Используйте кнопки, чтобы "
            "добавлять товары и предложения, запускать проверку, менять пороги и "
            "обрабатывать подозрительные цены, импортировать и экспортировать CSV. "
            "/cancel отменяет текущий диалог.",
            (("Главное меню", "menu:home"),),
        )

    async def products_screen(update, context, page=0):
        if context.user_data.get("flow", {}).get("kind") == CSV_RUNNING_KIND:
            if await authorized(update):
                await reply(update, "Импорт уже выполняется. Дождитесь завершения.")
            return
        clear_flow(context)
        result = await run(update, lambda products, monitoring, user_id: products.list_products(user_id))
        if result is FAILED:
            return
        if not result:
            await reply(
                update,
                "Товаров пока нет.",
                (("Добавить товар", "menu:add"), ("Главное меню", "menu:home")),
            )
            return
        last_page = (len(result) - 1) // PAGE_SIZE
        page = min(max(page, 0), last_page)
        offset = page * PAGE_SIZE
        visible = result[offset:offset + PAGE_SIZE]
        rows = [(product.name[:48], f"product:{product.id}:{page}") for product in visible]
        if page:
            rows.append(("← Предыдущая", f"products-page:{page - 1}"))
        if page < last_page:
            rows.append(("Следующая →", f"products-page:{page + 1}"))
        rows.append(("Главное меню", "menu:home"))
        await reply(
            update,
            f"Ваши товары ({offset + 1}–{offset + len(visible)} из {len(result)}):",
            rows,
        )

    async def product_screen(update, context, product_id, page=0, *, report_missing=True):
        result = await run(
            update,
            lambda products, monitoring, user_id: (
                products.get_product_details(user_id, product_id),
                products.get_user(user_id),
            ),
        )
        if result is FAILED:
            return False
        details, user = result
        if not details or not user:
            if report_missing:
                await recovery_screen(
                    update,
                    "Товар не найден или больше недоступен.",
                    (("К товарам", "menu:products"), ("Главное меню", "menu:home")),
                )
            return False
        offers = details.offers
        last_page = max(0, (len(offers) - 1) // PAGE_SIZE)
        page = min(max(page, 0), last_page)
        visible = offers[page * PAGE_SIZE:(page + 1) * PAGE_SIZE]
        rows = []
        for offer in visible:
            label = offer.source_title or offer.domain or f"Предложение #{offer.id}"
            rows.append((label[:40], f"offer:{offer.id}"))
        if page:
            rows.append(("← Предыдущая", f"product:{product_id}:{page - 1}"))
        if page < last_page:
            rows.append(("Следующая →", f"product:{product_id}:{page + 1}"))
        rows.extend((
            ("Добавить предложение", f"add-offer:{product_id}"),
            ("Переименовать", f"rename:{product_id}"),
            ("Изменить порог", f"product-threshold:{product_id}"),
            ("Удалить товар", f"delete-product:{product_id}"),
            ("К товарам", "menu:products"),
            ("Главное меню", "menu:home"),
        ))
        own = details.product.alert_threshold
        effective = own if own is not None else user.alert_threshold
        own_text = f"{_percent(own)}%" if own is not None else "наследуется"
        await reply(
            update,
            f"{details.product.name}\n"
            f"Предложений: {len(offers)}\n"
            f"Порог пользователя: {_percent(user.alert_threshold)}%\n"
            f"Порог товара: {own_text}\n"
            f"Эффективный порог: {_percent(effective)}%",
            rows,
        )
        return True

    async def offer_screen(update, context, offer_id):
        result = await run(
            update,
            lambda products, monitoring, user_id: (
                products.get_offer(user_id, offer_id),
                products.list_offer_history(user_id, offer_id, 10),
            ),
        )
        if result is FAILED:
            return
        offer, history = result
        if not offer:
            await recovery_screen(
                update,
                "Предложение не найдено или больше недоступно.",
                (("К товарам", "menu:products"), ("Главное меню", "menu:home")),
            )
            return
        history_text = "\n".join(
            f"• {item.checked_at}: {_price(item.price_minor, item.currency)}" for item in history
        ) or "нет подтверждённых наблюдений"
        pending = (
            f"\nОжидает решения: {_price(offer.pending_price_minor, offer.currency)}"
            if offer.pending_price_minor is not None else ""
        )
        rows = [("Проверить сейчас", f"check:{offer.id}")]
        rows.extend((
            ("Удалить предложение", f"delete-offer:{offer.id}"),
            ("Назад к товару", f"product:{offer.product_id}:0"),
            ("Главное меню", "menu:home"),
        ))
        await reply(
            update,
            f"{offer.source_title or offer.domain or 'Предложение'}\n"
            f"URL: {offer.url}\n"
            f"Статус: {offer.status.value} — {_status_explanation(offer.status.value)}\n"
            f"Подтверждённая цена: "
            f"{_price(offer.current_price_minor, offer.currency) if offer.current_price_minor is not None else 'нет'}"
            f"{pending}\n"
            f"Последняя проверка: {offer.last_checked_at or 'нет'}\n"
            f"История:\n{history_text}",
            rows,
        )

    async def settings_screen(update, context):
        user = await run(update, lambda products, monitoring, user_id: products.get_user(user_id))
        if user is FAILED:
            return
        if not user:
            await recovery_screen(update, "Пользователь не найден или больше недоступен.")
            return
        clear_flow(context)
        await reply(
            update,
            f"Порог пользователя: {_percent(user.alert_threshold)}%.\n"
            "Событие снижения возникает только при снижении строго больше порога.",
            (("Изменить порог", "settings:edit"), ("Главное меню", "menu:home")),
        )

    async def begin_csv_import(update, context):
        flow = new_flow(context, "csv-waiting-file", return_to=RETURN_HOME)
        await reply(
            update,
            "Отправьте CSV-документ в UTF-8 с колонками product_name,url.\n"
            "Ограничения: не более 100 записей и 5 МиБ. Импорт начнётся только "
            "после предварительного просмотра и подтверждения.",
            (("Отмена", f"csv:cancel:{flow['operation_id']}"),),
        )

    async def csv_document(update, context):
        if not await authorized(update):
            return
        flow = context.user_data.get("flow")
        if flow and flow.get("kind") == CSV_RUNNING_KIND:
            await reply(update, "Импорт уже выполняется. Дождитесь завершения.")
            return
        if flow and flow.get("kind") in TEXT_FLOW_PROMPTS:
            operation_id = flow.get("operation_id")
            await reply(
                update,
                "Документ не обработан: сейчас ожидается текстовый ввод.\n"
                + TEXT_FLOW_PROMPTS[flow["kind"]],
                (
                    ("Продолжить ввод", f"flow:continue:{operation_id}"),
                    ("Отмена", f"flow:cancel:{operation_id}"),
                    ("Главное меню", "menu:home"),
                ),
            )
            return
        if not flow or flow.get("kind") not in {
            "csv-waiting-file",
            "csv-preview-ready",
        }:
            await reply(
                update,
                "CSV-документ не ожидается. Выберите «Импорт CSV» в главном меню.",
                MENU,
            )
            return

        document = update.effective_message.document
        filename = document.file_name or "import.csv"
        download_flow = new_flow(context, "csv-downloading", filename=filename)
        operation_id = download_flow["operation_id"]
        csv_started = time.monotonic()
        if document.file_size is not None and document.file_size > MAX_CSV_SIZE_BYTES:
            waiting = new_flow(context, "csv-waiting-file")
            await reply(
                update,
                "Файл больше 5 МиБ. Отправьте другой CSV-документ или отмените импорт.",
                (("Отмена", f"csv:cancel:{waiting['operation_id']}"),),
            )
            _log_csv_import_invalid(operation_id, csv_started, total=0, invalid=1)
            return

        try:
            telegram_file = await document.get_file()
            content = bytes(await telegram_file.download_as_bytearray())
        except Exception as error:
            _log_csv_failed(
                "csv_import_failed", "download", operation_id,
                csv_started, error,
            )
            current = context.user_data.get("flow")
            if not current or current.get("operation_id") != operation_id:
                return
            waiting = new_flow(context, "csv-waiting-file")
            await reply(
                update,
                "Не удалось загрузить файл из Telegram. Попробуйте отправить его ещё раз.",
                (("Отмена", f"csv:cancel:{waiting['operation_id']}"),),
            )
            return

        current = context.user_data.get("flow")
        if not current or current.get("operation_id") != operation_id:
            return
        try:
            if offload_services:
                preview = await run_in_daemon_thread(CsvService().parse, content, filename)
            else:
                preview = CsvService().parse(content, filename)
        except Exception as error:
            _log_csv_failed(
                "csv_import_failed", "preview", operation_id,
                csv_started, error,
            )
            current = context.user_data.get("flow")
            if not current or current.get("operation_id") != operation_id:
                return
            waiting = new_flow(context, "csv-waiting-file")
            await reply(
                update,
                "Не удалось проверить CSV-файл. Отправьте другой CSV-документ "
                "или завершите импорт.",
                (("Отмена", f"csv:cancel:{waiting['operation_id']}"),
                 ("Главное меню", "menu:home")),
            )
            return

        current = context.user_data.get("flow")
        if not current or current.get("operation_id") != operation_id:
            return
        current["preview"] = preview
        if preview.can_confirm:
            current["kind"] = "csv-preview-ready"
            rows = (
                ("Импортировать", f"csv:confirm:{operation_id}"),
                ("Отмена", f"csv:cancel:{operation_id}"),
            )
        else:
            current["kind"] = "csv-waiting-file"
            rows = (("Отмена", f"csv:cancel:{operation_id}"),)
        await reply(update, _csv_preview_text(preview), rows)
        if not preview.can_confirm:
            _log_csv_import_invalid(
                operation_id,
                csv_started,
                total=preview.total_records,
                invalid=len(preview.invalid_records) + len(preview.errors),
            )

    async def csv_export(update, context):
        operation_id = _operation_id()
        started = time.monotonic()
        LOG.info(
            "event=csv_export_started outcome=started operation_id=%s",
            operation_id,
        )
        result = await run(
            update,
            lambda products, monitoring, user_id: CsvExportService(products).export_products(user_id),
            on_error=lambda error: _log_csv_failed(
                "csv_export_failed", "build", operation_id, started, error
            ),
        )
        if result is FAILED:
            return
        if not result.has_data:
            await reply(
                update,
                result.message or "Нет товаров для экспорта",
                CSV_EXPORT_NAVIGATION,
            )
            LOG.info(
                "event=csv_export_completed outcome=empty operation_id=%s "
                "duration_ms=%s exported=0",
                operation_id, _elapsed_ms(started),
            )
            return
        try:
            await reply_document(update, result.content, result.suggested_filename)
        except Exception as error:
            _log_csv_failed(
                "csv_export_failed", "delivery", operation_id, started, error
            )
            await reply(
                update,
                "Не удалось отправить CSV-файл. Попробуйте экспорт ещё раз.",
                CSV_EXPORT_NAVIGATION,
            )
            return
        await reply(
            update,
            "Экспорт CSV завершён.",
            CSV_EXPORT_NAVIGATION,
        )
        LOG.info(
            "event=csv_export_completed outcome=success operation_id=%s "
            "duration_ms=%s exported=%s",
            operation_id, _elapsed_ms(started), result.count,
        )

    async def csv_flow_callback(update, context, parts):
        if len(parts) != 3 or parts[1] not in {"confirm", "cancel"}:
            await recovery_screen(
                update,
                "Неизвестное или устаревшее действие. Начните новый импорт CSV.",
                CSV_RESTART_NAVIGATION,
            )
            return
        operation_id = parts[2]
        flow = context.user_data.get("flow")
        if not flow or flow.get("operation_id") != operation_id:
            await reply(
                update,
                "Действие устарело. Начните новый импорт CSV или вернитесь в главное меню.",
                CSV_RESTART_NAVIGATION,
            )
            return
        if flow.get("kind") == CSV_RUNNING_KIND:
            await reply(update, "Импорт уже выполняется. Дождитесь завершения.")
            return
        if parts[1] == "cancel":
            return_to = flow.get("return_to", RETURN_HOME)
            clear_flow(context)
            await remove_confirmation_keyboard(update)
            await navigate_to_return(
                update, context, return_to,
                home_text="Импорт CSV отменён.\n\nГлавное меню:",
            )
            return
        if flow.get("kind") != "csv-preview-ready" or not flow.get("preview"):
            await reply(
                update,
                "Действие устарело. Начните новый импорт CSV или вернитесь в главное меню.",
                CSV_RESTART_NAVIGATION,
            )
            return

        # Проверка и переход выполняются без await: конкурирующий callback уже
        # увидит IMPORT_RUNNING и не сможет запустить второй worker.
        flow["kind"] = CSV_RUNNING_KIND
        preview = flow["preview"]
        terminal_state = "start-failed"
        import_worker_started = False
        import_started = time.monotonic()

        def import_and_report(products, monitoring, user_id):
            nonlocal import_worker_started
            import_worker_started = True
            return _csv_import_and_report(
                products, user_id, preview, operation_id
            )

        try:
            await remove_confirmation_keyboard(update)
            try:
                await reply(update, "Импорт выполняется…")
            except Exception as error:
                _log_csv_failed(
                    "csv_import_failed", "start_notification", operation_id,
                    import_started, error,
                )
            else:
                terminal_state = "worker-failed"
                import_started = time.monotonic()
                LOG.info(
                    "event=csv_import_started outcome=started operation_id=%s",
                    operation_id,
                )
                outcome = await run(
                    update,
                    import_and_report,
                    report_error=False,
                    on_error=lambda error: _log_csv_failed(
                        "csv_import_failed", "process", operation_id,
                        import_started, error,
                    ),
                )
                if outcome is not FAILED:
                    terminal_state = "completed"
                    result, report = outcome
                    _log_csv_import_completed(result, operation_id, import_started)
                    try:
                        await reply(update, _csv_import_summary(result))
                        if result.records:
                            await reply_document(
                                update,
                                report.content,
                                report.suggested_filename,
                            )
                    except Exception as error:
                        _log_csv_failed(
                            "csv_import_failed", "result_delivery", operation_id,
                            import_started, error,
                        )
                        try:
                            await reply(
                                update,
                                "Импорт завершён, но отправить полный результат не удалось. "
                                "Повторный импорт автоматически не запускается.",
                            )
                        except Exception as error:
                            _log_csv_failed(
                                "csv_import_failed", "fallback_delivery",
                                operation_id, import_started, error,
                            )
        except asyncio.CancelledError:
            if terminal_state == "worker-failed" and import_worker_started:
                _log_csv_import_interrupted(
                    operation_id, import_started, "task_cancelled"
                )
            raise
        finally:
            clear_flow_if_current(context, operation_id)
        if terminal_state == "completed":
            await reply(
                update,
                "Импорт CSV завершён. Выберите следующее действие:",
                CSV_IMPORT_NAVIGATION,
            )
        elif terminal_state == "worker-failed":
            await reply(
                update,
                "Не удалось подтвердить, как завершился импорт. Автоматический повтор "
                "не выполнялся. Проверьте «Мои товары» или начните новый импорт явно.",
                CSV_IMPORT_NAVIGATION,
            )
        else:
            await reply(
                update,
                "Импорт не был запущен: не удалось отправить сообщение о начале. "
                "Начните новый импорт, когда будете готовы.",
                CSV_RESTART_NAVIGATION,
            )

    async def begin_add_product(update, context):
        if context.user_data.get("flow", {}).get("kind") == CSV_RUNNING_KIND:
            await reply(update, "Импорт уже выполняется. Дождитесь завершения.")
            return
        flow = new_flow(context, "new-url", return_to=RETURN_HOME)
        await reply(
            update,
            "Отправьте URL товара или /cancel.",
            (("Отмена", f"flow:cancel:{flow['operation_id']}"),),
        )

    async def command_add(update, context):
        if not await authorized(update):
            return
        await begin_add_product(update, context)

    async def command_check(update, context):
        if not await authorized(update):
            return
        if context.user_data.get("flow", {}).get("kind") == CSV_RUNNING_KIND:
            await reply(update, "Импорт уже выполняется. Дождитесь завершения.")
            return
        clear_flow(context)
        try:
            offer_id = int(context.args[0])
        except (IndexError, ValueError):
            await recovery_screen(
                update,
                "Не удалось распознать предложение. Используйте формат /check <ID предложения>.",
            )
            return
        await check_offer(update, context, offer_id)

    async def check_offer(update, context, offer_id):
        result = await run(update, lambda products, monitoring, user_id: monitoring.check_for_user(user_id, offer_id))
        if result is FAILED:
            return
        if result.code == "NOT_FOUND":
            await recovery_screen(update, _monitoring_text(result))
            return
        await reply(update, _monitoring_text(result))
        if result.events:
            await wake_notifications()
        await offer_screen(update, context, offer_id)

    async def flow_message(update, context):
        if not await authorized(update):
            return
        flow = context.user_data.get("flow")
        if not flow:
            await reply(update, "Выберите действие в меню.", MENU)
            return
        kind = flow.get("kind")
        if kind == CSV_RUNNING_KIND:
            await reply(update, "Импорт уже выполняется. Дождитесь завершения.")
            return
        if kind in {"csv-waiting-file", "csv-preview-ready", "csv-downloading"}:
            await reply(update, "Ожидается CSV-документ. Отправьте файл или используйте /cancel.")
            return
        text = (update.effective_message.text or "").strip()
        if not text:
            await reply(
                update,
                "Пустое значение не принимается. Повторите ввод или отмените действие.",
                (("Отмена", f"flow:cancel:{flow['operation_id']}"),
                 ("Главное меню", "menu:home")),
            )
            return
        if kind == "new-url":
            flow["kind"] = "processing-preview"
            preview = await run(update, lambda products, monitoring, user_id: products.preview_offer(user_id, text))
            if preview is FAILED:
                clear_flow(context); return
            if preview.code != "READY":
                flow["kind"] = "new-url"
                await reply(
                    update,
                    _operation_text(preview) + " Отправьте другой URL или отмените добавление.",
                    (("Отмена", f"flow:cancel:{flow['operation_id']}"),
                     ("Главное меню", "menu:home")),
                )
                return
            flow.update(kind="new-name-choice", preview=preview)
            if preview.source_title:
                await reply(
                    update,
                    f"Найдено название: «{preview.source_title}». Использовать его?",
                    (("Использовать", f"flow:use-title:{flow['operation_id']}"),
                     ("Ввести своё", f"flow:custom-title:{flow['operation_id']}"),
                     ("Отмена", f"flow:cancel:{flow['operation_id']}")),
                )
            else:
                flow["kind"] = "new-name"
                await reply(
                    update,
                    "Введите название товара или /cancel.",
                    (("Отмена", f"flow:cancel:{flow['operation_id']}"),),
                )
            return
        if kind == "new-name":
            flow.update(kind="new-confirm", name=text)
            await create_summary(update, flow)
            return
        if kind == "offer-url":
            operation_id = flow["operation_id"]
            flow["kind"] = "processing"
            result = await run(
                update,
                lambda products, monitoring, user_id: products.add_offer(user_id, flow["product_id"], text),
            )
            if result is FAILED:
                clear_flow_if_current(context, operation_id)
                return
            if result.code == "CREATED" and result.offer:
                clear_flow_if_current(context, operation_id)
                await reply(update, "Предложение добавлено.")
                await offer_screen(update, context, result.offer.id)
                return
            current = context.user_data.get("flow")
            if result.code in {"INVALID", "BLOCKED", "DUPLICATE", "STALE"} and (
                current
                and current.get("operation_id") == operation_id
                and current.get("kind") == "processing"
            ):
                current["kind"] = "offer-url"
                await reply(
                    update,
                    _operation_text(result) + " Отправьте другой URL предложения.",
                    (("Отмена", f"flow:cancel:{operation_id}"),
                     ("Главное меню", "menu:home")),
                )
                return
            clear_flow_if_current(context, operation_id)
            await recovery_screen(update, _operation_text(result))
            return
        if kind == "rename":
            result = await run(update, lambda products, monitoring, user_id: products.rename_product(user_id, flow["product_id"], text))
            if result is FAILED:
                clear_flow(context); return
            if result.code != "UPDATED":
                if result.code == "NOT_FOUND":
                    clear_flow(context)
                    await recovery_screen(update, _operation_text(result))
                    return
                await reply(
                    update,
                    _operation_text(result) + " Повторите ввод или отмените действие.",
                    (("Отмена", f"flow:cancel:{flow['operation_id']}"),
                     ("Главное меню", "menu:home")),
                )
                return
            product_id = flow["product_id"]
            clear_flow(context)
            await reply(update, "Название обновлено.")
            await product_screen(update, context, product_id)
            return
        if kind in {"user-threshold", "product-threshold"}:
            value = _threshold_arg(text)
            if value is None:
                await reply(
                    update,
                    "Введите число от 0 до 100 (допустимы точка или запятая) или отмените действие.",
                    (("Отмена", f"flow:cancel:{flow['operation_id']}"),
                     ("Главное меню", "menu:home")),
                )
                return
            if kind == "user-threshold":
                result = await run(update, lambda products, monitoring, user_id: products.set_user_threshold(user_id, value))
                clear_flow(context)
                if result is FAILED:
                    return
                if result.code != "UPDATED":
                    await recovery_screen(update, _operation_text(result))
                    return
                await reply(update, "Порог обновлён.")
                await settings_screen(update, context)
            else:
                product_id = flow["product_id"]
                result = await run(update, lambda products, monitoring, user_id: products.set_product_threshold(user_id, product_id, value))
                clear_flow(context)
                if result is FAILED:
                    return
                if result.code != "UPDATED":
                    await recovery_screen(update, _operation_text(result))
                    return
                await reply(update, "Порог товара обновлён.")
                await product_screen(update, context, product_id)
            return
        if kind == "processing":
            await reply(update, "Операция уже выполняется.")
            return
        await reply(update, "Этот диалог устарел. Вернитесь в главное меню.", (("Главное меню", "menu:home"),))

    async def create_summary(update, flow):
        preview = flow["preview"]
        price = _preview_price(preview)
        await reply(
            update,
            f"Создать товар?\nНазвание: {flow['name']}\nURL: {preview.normalized_url}\n{price}",
            (("Подтвердить", f"flow:create:{flow['operation_id']}"),
             ("Отмена", f"flow:cancel:{flow['operation_id']}")),
        )

    async def callback(update, context):
        if not await authorized(update, answer_callback=True):
            return
        data = update.callback_query.data or ""
        flow = context.user_data.get("flow")
        if flow and flow.get("kind") == CSV_RUNNING_KIND and not data.startswith("csv:"):
            await reply(update, "Импорт уже выполняется. Дождитесь завершения.")
            return
        if (
            flow
            and flow.get("kind") in {
                "csv-waiting-file",
                "csv-preview-ready",
                "csv-downloading",
            }
            and not data.startswith("csv:")
        ):
            # Любой переход из CSV-раздела синхронно инвалидирует старые
            # confirm/cancel callback до первого await нового действия.
            clear_flow(context)
        if data == "menu:home":
            await show_menu(update, context); return
        if data == "menu:products":
            clear_flow(context); await products_screen(update, context); return
        if data == "menu:add":
            await begin_add_product(update, context); return
        if data == "menu:csv-import":
            await begin_csv_import(update, context); return
        if data == "menu:csv-export":
            clear_flow(context); await csv_export(update, context); return
        if data == "menu:settings":
            await settings_screen(update, context); return
        if data == "menu:help":
            await help_screen(update, context); return
        if data == "settings:edit":
            flow = new_flow(context, "user-threshold", return_to=RETURN_SETTINGS)
            await reply(
                update,
                "Введите новый порог от 0 до 100 или /cancel.",
                (("Отмена", f"flow:cancel:{flow['operation_id']}"),),
            )
            return

        parts = data.split(":")
        if parts and parts[0] == "csv":
            await csv_flow_callback(update, context, parts); return
        if len(parts) == 2 and parts[0] == "products-page" and _digits(parts[1]):
            await products_screen(update, context, int(parts[1])); return
        if len(parts) == 3 and parts[0] == "product" and _digits(parts[1], parts[2]):
            clear_flow(context); await product_screen(update, context, int(parts[1]), int(parts[2])); return
        if len(parts) == 2 and parts[0] == "offer" and _digits(parts[1]):
            clear_flow(context); await offer_screen(update, context, int(parts[1])); return
        if len(parts) == 2 and parts[0] == "check" and _digits(parts[1]):
            clear_flow(context); await check_offer(update, context, int(parts[1])); return
        if len(parts) == 2 and parts[0] in {"add-offer", "rename", "product-threshold", "delete-product", "delete-offer"} and _digits(parts[1]):
            await begin_object_action(update, context, parts[0], int(parts[1])); return
        if parts and parts[0] == "flow":
            await flow_callback(update, context, parts); return
        if len(parts) == 4 and parts[0] == "anomaly" and parts[1] in {"confirm", "reject"} and _digits(parts[2], parts[3]):
            await anomaly_action(update, context, parts[1], int(parts[2]), int(parts[3])); return
        await recovery_screen(
            update,
            "Неизвестное или устаревшее действие. Выберите дальнейшее действие.",
        )

    async def begin_object_action(update, context, kind, object_id):
        if kind in {"add-offer", "rename", "product-threshold", "delete-product"}:
            details = await run(update, lambda products, monitoring, user_id: products.get_product_details(user_id, object_id))
            if details is FAILED:
                return
            if not details:
                await recovery_screen(update, "Товар не найден или недоступен."); return
        else:
            offer = await run(update, lambda products, monitoring, user_id: products.get_offer(user_id, object_id))
            if offer is FAILED:
                return
            if not offer:
                await recovery_screen(update, "Предложение не найдено или недоступно."); return
        if kind == "add-offer":
            flow = new_flow(
                context,
                "offer-url",
                return_to=product_return(object_id),
                product_id=object_id,
            )
            await reply(
                update,
                "Отправьте URL предложения или /cancel.",
                (("Отмена", f"flow:cancel:{flow['operation_id']}"),),
            )
        elif kind == "rename":
            flow = new_flow(
                context,
                "rename",
                return_to=product_return(object_id),
                product_id=object_id,
            )
            await reply(
                update,
                "Отправьте новое название или /cancel.",
                (("Отмена", f"flow:cancel:{flow['operation_id']}"),),
            )
        elif kind == "product-threshold":
            flow = new_flow(
                context,
                "product-threshold",
                return_to=product_return(object_id),
                product_id=object_id,
            )
            await reply(
                update,
                "Введите порог от 0 до 100 или сбросьте к значению пользователя.",
                (("Наследовать", f"flow:inherit:{object_id}:{flow['operation_id']}"),
                 ("Отмена", f"flow:cancel:{flow['operation_id']}")),
            )
        else:
            return_product_id = object_id if kind == "delete-product" else offer.product_id
            flow = new_flow(
                context,
                kind,
                return_to=product_return(return_product_id),
                object_id=object_id,
            )
            label = "товара" if kind == "delete-product" else "предложения"
            await reply(
                update,
                f"Подтвердите удаление {label}.",
                (("Да, удалить", f"flow:{kind}:{object_id}:{flow['operation_id']}"),
                 ("Отмена", f"flow:cancel:{flow['operation_id']}")),
            )

    async def flow_callback(update, context, parts):
        flow = context.user_data.get("flow")
        if len(parts) == 3 and parts[1] == "continue":
            operation_id = parts[2]
            if (
                not flow
                or flow.get("operation_id") != operation_id
                or flow.get("kind") not in TEXT_FLOW_PROMPTS
            ):
                await recovery_screen(update, "Действие устарело. Начните его заново из меню."); return
            await reply(
                update,
                TEXT_FLOW_PROMPTS[flow["kind"]],
                (("Отмена", f"flow:cancel:{operation_id}"),
                 ("Главное меню", "menu:home")),
            )
            return
        if len(parts) == 3 and parts[1] == "cancel":
            operation_id = parts[2]
            if not flow or flow.get("operation_id") != operation_id:
                await recovery_screen(update, "Действие устарело. Начните его заново из меню."); return
            if flow.get("kind") == CSV_RUNNING_KIND:
                await reply(update, "Импорт уже выполняется. Дождитесь завершения.")
                return
            return_to = flow.get("return_to", RETURN_HOME)
            clear_flow(context)
            await remove_confirmation_keyboard(update)
            await navigate_to_return(update, context, return_to)
            return
        if len(parts) == 3 and parts[1] in {"use-title", "custom-title", "create"}:
            operation_id = parts[2]
            if not flow or flow.get("operation_id") != operation_id:
                await recovery_screen(update, "Действие устарело. Начните его заново из меню."); return
            if parts[1] == "use-title" and flow.get("kind") == "new-name-choice" and flow["preview"].source_title:
                flow.update(kind="new-confirm", name=flow["preview"].source_title)
                await create_summary(update, flow); return
            if parts[1] == "custom-title" and flow.get("kind") == "new-name-choice":
                flow["kind"] = "new-name"
                await reply(
                    update,
                    "Введите своё название или /cancel.",
                    (("Отмена", f"flow:cancel:{flow['operation_id']}"),),
                ); return
            if parts[1] == "create" and flow.get("kind") == "new-confirm":
                flow["kind"] = "processing"
                preview, name = flow["preview"], flow["name"]
                await remove_confirmation_keyboard(update)
                result = await run(update, lambda products, monitoring, user_id: products.create_product_with_preview(user_id, name, preview))
                clear_flow_if_current(context, operation_id)
                if result is FAILED:
                    return
                if result.code == "CREATED" and result.product:
                    await reply(update, "Товар добавлен.")
                    await product_screen(update, context, result.product.id)
                else:
                    await recovery_screen(update, _operation_text(result))
                return
            await recovery_screen(update, "Действие устарело. Начните его заново из меню."); return
        if len(parts) == 4 and parts[1] == "inherit" and _digits(parts[2]):
            product_id, operation_id = int(parts[2]), parts[3]
            if not flow or flow.get("kind") != "product-threshold" or flow.get("product_id") != product_id or flow.get("operation_id") != operation_id:
                await recovery_screen(update, "Действие устарело. Начните его заново из меню."); return
            flow["kind"] = "processing"
            result = await run(update, lambda products, monitoring, user_id: products.set_product_threshold(user_id, product_id, None))
            clear_flow(context)
            if result is FAILED:
                return
            if result.code != "UPDATED":
                await recovery_screen(update, _operation_text(result))
                return
            await reply(update, "Порог товара сброшен.")
            await product_screen(update, context, product_id); return
        if len(parts) == 4 and parts[1] in {"delete-product", "delete-offer"} and _digits(parts[2]):
            kind, object_id, operation_id = parts[1], int(parts[2]), parts[3]
            if not flow or flow.get("kind") != kind or flow.get("object_id") != object_id or flow.get("operation_id") != operation_id:
                await recovery_screen(update, "Действие устарело. Начните его заново из меню."); return
            return_to = flow.get("return_to", RETURN_PRODUCTS)
            flow["kind"] = "processing"
            await remove_confirmation_keyboard(update)
            if kind == "delete-product":
                result = await run(update, lambda products, monitoring, user_id: products.delete_product(user_id, object_id))
            else:
                result = await run(update, lambda products, monitoring, user_id: products.delete_offer(user_id, object_id))
            clear_flow_if_current(context, operation_id)
            if result is FAILED:
                return
            if result.code != "DELETED":
                await recovery_screen(update, _operation_text(result))
                return
            await reply(update, "Удалено.")
            if kind == "delete-offer" and (
                isinstance(return_to, tuple)
                and len(return_to) == 2
                and return_to[0] == "product"
            ):
                if not await product_screen(
                    update, context, return_to[1], report_missing=False
                ):
                    await products_screen(update, context)
                return
            await products_screen(update, context); return
        await recovery_screen(
            update,
            "Неизвестное или устаревшее действие. Начните его заново из меню.",
        )

    async def anomaly_action(update, context, action, offer_id, attempt_id):
        query = update.callback_query
        message = query.message if query else None
        chat = update.effective_chat
        if message is None or chat is None:
            await recovery_screen(update, "Не удалось проверить источник этой кнопки.")
            return
        validation = await run(
            update,
            lambda products, monitoring, user_id: monitoring.validate_anomaly_notification(
                user_id, chat.id, message.message_id, offer_id, attempt_id
            ),
        )
        if validation is FAILED:
            return
        if validation.code == "INVALID":
            await recovery_screen(
                update,
                "Эта кнопка не связана с действительным уведомлением PriceWatch.",
            )
            return
        if validation.code == "STALE":
            await _set_anomaly_navigation(query, offer_id)
            await reply(
                update,
                "Эта аномалия уже обработана или кнопка устарела.",
                notification_navigation(offer_id),
            )
            return
        operation = (
            (lambda products, monitoring, user_id: monitoring.confirm_anomaly(user_id, offer_id, attempt_id))
            if action == "confirm" else
            (lambda products, monitoring, user_id: monitoring.reject_anomaly(user_id, offer_id, attempt_id))
        )
        result = await run(update, operation)
        if result is FAILED:
            return
        if result.code in {"CONFIRMED", "REJECTED"}:
            status = "✅ Цена подтверждена." if result.code == "CONFIRMED" else "❌ Цена отклонена."
            original_text = getattr(message, "text", None) or "Уведомление об аномальной цене"
            try:
                await query.edit_message_text(
                    text=f"{original_text}\n\n{status}",
                    reply_markup=keyboard(notification_navigation(offer_id)),
                    disable_web_page_preview=True,
                )
            except Exception as error:
                LOG.error(
                    "event=telegram_callback action=anomaly_message_edit "
                    "outcome=error offer_id=%s exception_class=%s",
                    offer_id,
                    type(error).__name__,
                )
                await recovery_screen(
                    update,
                    f"{status} Исходное уведомление изменить не удалось; повторное действие не требуется.",
                    notification_navigation(offer_id),
                )
            return
        if result.code in {"STALE", "NOT_FOUND"}:
            include_offer = result.code != "NOT_FOUND"
            await _set_anomaly_navigation(
                query, offer_id, include_offer=include_offer
            )
            await reply(
                update,
                "Эта аномалия уже обработана или предложение недоступно.",
                notification_navigation(offer_id, include_offer=include_offer),
            )
            return
        await recovery_screen(update, "Не удалось обработать аномалию.")

    async def _set_anomaly_navigation(query, offer_id, *, include_offer=True):
        try:
            await query.edit_message_reply_markup(
                reply_markup=keyboard(
                    notification_navigation(offer_id, include_offer=include_offer)
                )
            )
        except Exception as error:
            LOG.error(
                "event=telegram_callback action=anomaly_navigation_edit "
                "outcome=error offer_id=%s exception_class=%s",
                offer_id, type(error).__name__,
            )

    async def cancel(update, context):
        if not await authorized(update):
            return
        flow = context.user_data.get("flow")
        if flow and flow.get("kind") == CSV_RUNNING_KIND:
            await reply(update, "Импорт уже выполняется и не может быть отменён. Дождитесь завершения.")
            return
        if not flow:
            await reply(update, "Нет активной операции. Выберите действие:", MENU)
            return
        await cancel_flow(update, context)

    async def unknown(update, context):
        if await authorized(update):
            if context.user_data.get("flow", {}).get("kind") == CSV_RUNNING_KIND:
                await reply(update, "Импорт уже выполняется. Дождитесь завершения.")
                return
            await reply(
                update,
                "Неизвестная команда. Выберите действие или откройте помощь.",
                MENU,
            )

    application.add_handler(CommandHandler("start", start))
    application.add_handler(CommandHandler("help", help_screen))
    application.add_handler(CommandHandler("products", products_screen))
    application.add_handler(CommandHandler("add", command_add))
    application.add_handler(CommandHandler("check", command_check))
    application.add_handler(CommandHandler("cancel", cancel))
    application.add_handler(CallbackQueryHandler(callback))
    application.add_handler(MessageHandler(filters.Document.ALL, csv_document))
    application.add_handler(MessageHandler(filters.TEXT & ~filters.COMMAND, flow_message))
    application.add_handler(MessageHandler(filters.COMMAND, unknown))
    application.add_error_handler(global_error_handler)
    return application


def _csv_import_and_report(products, user_id, preview, operation_id=None):
    result = CsvImportService(products).import_preview(
        user_id, preview, operation_id=operation_id
    )
    report = CsvExportService(products).export_import_report(result)
    return result, report


def _csv_preview_text(preview):
    filename = preview.filename or "без имени"
    lines = [
        f"Предварительный просмотр CSV: {filename}",
        f"Всего записей: {preview.total_records}",
        f"Корректных: {preview.valid_count}",
        f"INVALID: {preview.invalid_count}",
    ]
    if preview.structural_rejected:
        lines.append("Файл структурно отклонён.")
    for error in preview.errors[:3]:
        line = f"Строка {error.line_number}: " if error.line_number is not None else ""
        lines.append(f"• {line}{_csv_error_message(error.code)}")
    if len(preview.errors) > 3:
        lines.append(f"Ещё ошибок: {len(preview.errors) - 3}")
    if preview.can_confirm:
        lines.append(
            "Preview проверяет только структуру и обязательные поля; URL будут "
            "проверены после подтверждения."
        )
    else:
        lines.append("Импортировать этот файл нельзя. Отправьте другой CSV.")
    return "\n".join(lines)


def _csv_error_message(code):
    return {
        CsvErrorCode.FILE_TOO_LARGE: "файл больше 5 МиБ",
        CsvErrorCode.INVALID_ENCODING: "файл не является корректным UTF-8",
        CsvErrorCode.MISSING_HEADER: "отсутствует заголовок CSV",
        CsvErrorCode.MISSING_REQUIRED_HEADER: "нет обязательных колонок product_name,url",
        CsvErrorCode.DUPLICATE_HEADER: "заголовок содержит повторяющиеся колонки",
        CsvErrorCode.EMPTY_HEADER: "заголовок содержит пустую колонку",
        CsvErrorCode.MALFORMED_CSV: "нарушена структура CSV",
        CsvErrorCode.TOO_MANY_RECORDS: "в файле больше 100 записей",
        CsvErrorCode.FIELD_COUNT_MISMATCH: "число полей не совпадает с заголовком",
        CsvErrorCode.EMPTY_PRODUCT_NAME: "product_name пуст",
        CsvErrorCode.EMPTY_URL: "url пуст",
    }.get(code, "ошибка CSV")


def _csv_import_summary(result):
    active = sum(
        record.code is CsvImportCode.IMPORTED
        and record.offer_status is not None
        and record.offer_status.value == "ACTIVE"
        for record in result.records
    )
    pending = sum(
        record.code is CsvImportCode.IMPORTED
        and record.offer_status is not None
        and record.offer_status.value == "PENDING"
        for record in result.records
    )
    completion = (
        "завершён полностью"
        if result.completed
        else "прерван"
        if result.interrupted
        else "не завершён"
    )
    return (
        "Импорт CSV завершён.\n"
        f"Всего строк: {result.total_records}\n"
        f"Импортировано: {result.imported_count} "
        f"(ACTIVE: {active}, PENDING: {pending})\n"
        f"Дубликатов: {result.duplicate_count}\n"
        f"Некорректных: {result.invalid_count}\n"
        f"Заблокированных: {result.blocked_count}\n"
        f"Неоднозначных: {result.ambiguous_count}\n"
        f"Ошибок: {result.error_count}\n"
        f"Не обработано: {result.not_processed_count}\n"
        f"Статус: {completion}."
    )


def _log_csv_import_completed(result, operation_id, started):
    pending = sum(
        record.code is CsvImportCode.IMPORTED
        and record.offer_status is not None
        and record.offer_status.value == "PENDING"
        for record in result.records
    )
    if result.completed and result.total_records == result.imported_count:
        outcome = "success"
    elif (
        result.imported_count == 0
        and result.total_records > 0
        and result.invalid_count == result.total_records
    ):
        outcome = "invalid"
    else:
        outcome = "partial"
    LOG.info(
        "event=csv_import_completed outcome=%s operation_id=%s duration_ms=%s "
        "total=%s imported=%s pending=%s invalid=%s duplicate=%s blocked=%s "
        "ambiguous=%s errors=%s unprocessed=%s",
        outcome, operation_id, _elapsed_ms(started), result.total_records,
        result.imported_count, pending, result.invalid_count,
        result.duplicate_count, result.blocked_count, result.ambiguous_count,
        result.error_count, result.not_processed_count,
    )


def _log_csv_import_invalid(operation_id, started, *, total, invalid):
    LOG.info(
        "event=csv_import_completed outcome=invalid operation_id=%s "
        "duration_ms=%s total=%s imported=0 pending=0 invalid=%s duplicate=0 "
        "blocked=0 ambiguous=0 errors=0 unprocessed=0",
        operation_id, _elapsed_ms(started), total, invalid,
    )


def _log_csv_failed(event, action, operation_id, started, error):
    template = (
        "event=csv_import_failed action=%s outcome=error operation_id=%s "
        "duration_ms=%s exception_class=%s"
        if event == "csv_import_failed"
        else
        "event=csv_export_failed action=%s outcome=error operation_id=%s "
        "duration_ms=%s exception_class=%s"
    )
    LOG.error(
        template,
        action, operation_id, _elapsed_ms(started), type(error).__name__,
    )


def _log_csv_import_interrupted(operation_id, started, reason):
    LOG.warning(
        "event=csv_import_interrupted outcome=uncertain operation_id=%s "
        "reason=%s duration_ms=%s",
        operation_id, reason, _elapsed_ms(started),
    )


def _elapsed_ms(started):
    return max(0, round((time.monotonic() - started) * 1000))


def _run_service(database_path, telegram_id, username, operation):
    connection = connect(database_path)
    try:
        checker = PriceChecker(RobotsPolicy(), RequestsFetcher(), UniversalExtractor())
        products = ProductService(connection, checker)
        user = products.get_or_create_user(telegram_id, username)
        return operation(products, MonitoringService(connection, checker), user.id)
    finally:
        connection.close()


def _price(minor, currency):
    """Format integer minor units exactly without binary floating point."""
    sign = "-" if minor < 0 else ""
    whole, fraction = divmod(abs(int(minor)), 100)
    return f"{sign}{whole}.{fraction:02d} {currency or ''}".strip()


def _percent(value):
    decimal = Decimal(str(value))
    return format(decimal.normalize(), "f")


def _operation_text(result):
    if result is FAILED or result is None:
        return "Не удалось выполнить действие. Попробуйте позже."
    return {
        "DUPLICATE": "Такой товар или URL уже существует.",
        "INVALID": "Некорректные данные.",
        "BLOCKED": "Этот URL небезопасен и не может быть использован.",
        "NOT_FOUND": "Объект не найден или недоступен.",
        "STALE": "Предварительная проверка устарела. Начните добавление заново.",
    }.get(getattr(result, "code", None), "Не удалось выполнить действие.")


def _monitoring_text(result):
    text = {
        "SUCCESS": "Проверка завершена: цена подтверждена.",
        "PENDING": "Проверка завершена: надёжная цена пока не получена.",
        "ERROR": "Проверка завершена с ошибкой.",
        "SUSPICIOUS": "Обнаружена подозрительная цена; требуется решение.",
        "STALE": "Данные изменились во время проверки; результат не сохранён.",
        "SKIPPED": "Проверка пропущена для неактивного предложения.",
        "NOT_FOUND": "Предложение не найдено или недоступно.",
        "INTERNAL_ERROR": (
            "Проверка не завершена из-за внутренней ошибки. "
            "Повтор автоматически не выполнялся."
        ),
    }.get(result.code, "Проверка завершена.")
    details = []
    for event in result.events:
        if event.type is EventType.PRICE_DROP:
            percent = _percent(event.drop_percent) if event.drop_percent is not None else "не указан"
            details.append(
                "Снижение: "
                f"{_price(event.previous_price_minor, event.currency)} → "
                f"{_price(event.new_price_minor, event.currency)} ({percent}%)."
            )
        elif event.type is EventType.ANOMALY_DETECTED:
            details.append(
                "Необычная цена: "
                f"{_price(event.previous_price_minor, event.currency)} → "
                f"{_price(event.new_price_minor, event.currency)}; требуется решение."
            )
        elif event.type is EventType.REPEATED_ERROR:
            details.append("Создано уведомление о трёх последовательных ошибках.")
    if details:
        text += "\n" + "\n".join(details) + "\nУведомление поставлено в очередь."
    return text


def _threshold_arg(value):
    try:
        parsed = Decimal(str(value).replace(",", "."))
    except (InvalidOperation, ValueError):
        return None
    if not parsed.is_finite() or parsed < 0 or parsed > 100:
        return None
    return float(parsed)


def _preview_price(preview):
    check = preview.check
    if not check or check.fetch.status.value != "SUCCESS" or check.extraction.status.value != "SUCCESS":
        return "Начальная цена не подтверждена; предложение будет сохранено как PENDING."
    price = check.extraction.price
    currency = check.extraction.currency
    return f"Найдена цена: {price} {currency}" if price is not None and currency else "Цена пока не подтверждена."


def _status_explanation(status):
    return {
        "ACTIVE": "есть подтверждённая цена",
        "PENDING": "цена ещё не подтверждена",
        "ERROR": "несколько проверок завершились ошибкой",
        "SUSPICIOUS": "новая цена ожидает подтверждения",
        "DISABLED": "проверки отключены",
    }.get(status, "неизвестное состояние")


def _digits(*values):
    return all(isinstance(value, str) and value.isdigit() for value in values)


def _telegram_update_type(update):
    if update is None:
        return "none"
    if getattr(update, "callback_query", None) is not None:
        return "callback"
    if getattr(update, "effective_message", None) is not None:
        return "message"
    return "unsupported"


def _operation_id():
    return secrets.token_urlsafe(6)
