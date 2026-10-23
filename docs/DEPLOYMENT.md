# Развёртывание PriceWatch

Текущий способ локальной эксплуатации — Docker Compose. `docker compose up --build -d` запускает один постоянный runtime: Telegram polling, ежедневную
автоматическую проверку в 03:00 UTC и Delivery Coordinator для SQLite Outbox.
Перед запуском создайте `.env` на основе `.env.example`, задайте Telegram
token/whitelist и ограничьте права командой `chmod 600 .env`. SQLite хранится
в Compose-volume `pricewatch-data-local` с фактическим Docker-именем
`pricewatcher_pricewatch-data-local`. Обычный `docker compose down` его не
удаляет; `docker compose down -v` запрещён, если данные нужно сохранить. VPS
deployment и его egress acceptance отложены до отдельного решения.

Постоянный локальный runtime управляется из корня репозитория без
`--project-name`:

```bash
docker compose config --quiet
docker compose up --build -d
docker compose ps
docker compose logs --tail=200 pricewatch
docker compose stop
docker compose start
# Полное пересоздание контейнера с сохранением SQLite:
docker compose down
docker compose up -d
```

Production Dockerfile использует явный Debian-вариант
`python:3.12-slim-bookworm`. В принятом образе фактически проверены  
Python 3.12.15, Debian 12 (bookworm) и OpenSSL 3.0.22.

Проверять Compose следует командой `docker compose config --quiet`. Не сохраняйте
и не публикуйте обычный вывод `docker compose config`: Compose разворачивает в
него значения `env_file`, включая Telegram token и whitelist. Если такой вывод
попал в журнал, CI artifact или чат, token необходимо отозвать через BotFather.

## Безопасность журналов

Во время локальной ручной приёмки M10.6 новый Telegram token был обнаружен в
локальных Docker-логах: `httpx` на уровне `INFO` записывал полный URL Bot API,
в котором token является частью пути. Сведений о публикации этих логов или о
внешней компрометации нет. После обновления `httpx` и `httpcore` не выводят
штатные request-line на `INFO`; `WARNING` и `ERROR` остаются доступными.

Все уже подключённые console/container handlers получают formatter, который до
вывода маскирует Bot API token в URL и отдельном тексте, значения
`TELEGRAM_BOT_TOKEN=...`, `Authorization` headers, сообщения исключений и
traceback. Исходный `LogRecord` formatter не изменяет. Приложение не пишет в
диагностику полный `.env` или объект Telegram-конфигурации.

Compose использует Docker logging driver `json-file` с `max-size=10m` и
`max-file=3`, поэтому локальные container logs не растут без ограничения.

После обновления безопасно проверьте последние строки локально:

```bash
docker compose up --build -d
docker compose logs --tail=200 pricewatch
```

Не копируйте этот вывод в публичные каналы. В строках с Bot API вместо token
должно быть `<redacted>`; если виден действующий token или обычный вывод
`docker compose config`, остановите распространение лога и отзовите token через
BotFather.

SIGTERM/SIGINT переводит runtime в STOPPING: новые scheduler jobs и wakeup-сигналы запрещаются, polling останавливается, активным компонентам даётся единый общий бюджет до 30 секунд, затем PTB Application закрывается. Синхронные monitoring/SQLite-фазы работают в daemon worker threads и создают/закрывают свои SQLite connections внутри того же потока; при исчерпании бюджета connection из другого потока не закрывается.

До SQLite bootstrap, Telegram polling и фоновых workers `main` захватывает
Linux OS advisory `flock` в `/data/pricewatch.db.lock` (рядом с каноническим
путём SQLite). Descriptor удерживается весь lifecycle и освобождается в
`finally`; после SIGTERM/SIGINT и SIGKILL его также освобождает ОС. Второй
экземпляр с той же базой получает понятный отказ и exit code 3, не меняя SQLite
и не запуская polling. Lock-файл никогда не удаляется: он не является
PID-маркером, поэтому его наличие без удерживаемого descriptor не блокирует
новый запуск. Защита не глобальна для одного token: разные базы или независимые
filesystem имеют разные lock-файлы.

Compose задаёт `stop_grace_period: 35s`, то есть больше общего 30-секундного
shutdown budget runtime. Это позволяет обработать SIGTERM до принудительного
SIGKILL, не ослабляя non-root/read-only/capability ограничения.

Превышение общего shutdown deadline или ошибка shutdown-компонента дают
ненулевой exit code. Отмена asyncio-ожидания не останавливает уже выполняющийся
daemon thread: до фактического выхода процесса он может завершить HTTP-запрос и
свою SQLite transaction. Поэтому scheduler гарантирует атомарный claim и не
обещает exactly-once HTTP execution, а Outbox остаётся at-least-once.

Состояние ежедневных запусков проверяется запросом:

```bash
docker compose exec pricewatch python -c "import os, sqlite3; c=sqlite3.connect(os.environ['PRICEWATCH_DATABASE_PATH']); print(c.execute('SELECT scheduled_for,status,attempt_count,error_message FROM scheduler_runs ORDER BY scheduled_for DESC LIMIT 10').fetchall())"
```

`FAILED` фиксируется как терминальный результат текущего слота и не повторяется при каждом restart; следующий день выполняется штатно. Для обычной оперативной проверки причины используйте `error_message` и container logs, затем безопаснее запустить нужные Offers вручную через Telegram. Если необходим именно административный повтор того же слота, остановите единственный экземпляр приложения, сделайте backup SQLite и в явной transaction измените выбранную строку `FAILED` на `INTERRUPTED`, оставив `attempt_count = 1`; следующий startup разрешит ровно одну recovery-попытку. Удалять строку или менять завершённый `SUCCESS` не следует.

Контейнер не публикует порты, не использует host network, Docker socket, `privileged` или `NET_ADMIN`, работает непривилегированным пользователем и имеет read-only root filesystem. Лимиты: 256 MiB RAM, 0.5 CPU и 100 PID. `no-new-privileges` намеренно не задан: на проверенном Docker runtime он запрещал exec даже стандартного `/bin/sh`; `cap_drop: ALL` остаётся включённым.

## Сеть и egress на Linux VPS

Compose создаёт только для PriceWatch сеть `pricewatch_egress` с IPv4 `172.30.0.0/24`, gateway `172.30.0.1` и bridge-интерфейсом `pw-egress0`. IPv6 на этой сети отключён. Фиксированные имя и subnet — контракт между `compose.yaml` и [`deploy/pricewatch-egress.nft`](../deploy/pricewatch-egress.nft), а не замена проверки фактической сети.

При запуске того же Compose-файла с альтернативным `--project-name` Docker
Compose может предупредить, что явно именованная `pricewatch_egress` создана
другим Compose-проектом. В M10.9.2 это наблюдалось для изолированного проекта
`pricewatcher-m1092`. M10.9.3 перевёл постоянную эксплуатацию на основной
проект `pricewatcher`, которому сеть уже принадлежит; предупреждение исчезло
без изменения сети и без объявления `external`. Для постоянной эксплуатации
следует использовать обычные команды `docker compose` из корня репозитория.

`UrlSecurity` предотвращает известные private/special IP до HTTP-запроса, но это не является полной защитой от DNS rebinding. Host-level nftables policy нужна как независимый барьер: она ограничена входящим интерфейсом `pw-egress0`, блокирует special/private IPv4 и IPv6 в `FORWARD`, а в `INPUT` отвергает весь трафик контейнера к самому Docker host/gateway. Глобальные IPv6-правила и rule вида `ip6 saddr ::/0` не используются. При отключённом IPv6 у контейнера нет IPv6 source; правила IPv6 остаются защитой на случай ошибочной будущей конфигурации.

Политика не устанавливается автоматически. До production администратор должен выполнить на VPS:

```bash
docker compose up --no-start
docker network inspect pricewatch_egress
ip link show pw-egress0
sudo nft list ruleset > nftables.before-pricewatch-$(date +%F-%H%M%S).conf
sudo nft -c -f deploy/pricewatch-egress.nft
sudo nft -f deploy/pricewatch-egress.nft
sudo nft list table inet pricewatch_egress
```

Перед применением убедитесь, что таблицы `inet pricewatch_egress` ещё нет: файл намеренно не заменяет глобальный ruleset. Если фактические bridge/subnet отличаются от указанных выше, остановитесь, исправьте согласованно Compose, nft-конфигурацию и проверку, затем заново выполните валидацию. Откат только этой изолированной таблицы: `sudo nft delete table inet pricewatch_egress`; сохранённый ruleset предназначен для ручного аварийного восстановления администратором.

После установки запустите воспроизводимую проверку:

```bash
bash deploy/verify-m4-2.sh
```

Сценарий строит образ, проверяет Compose, non-root/read-only режим, сохранность SQLite после реального `force-recreate` service-container, отсутствие published ports, фактические network/bridge и public HTTPS. Он не меняет firewall. Каждый пункт печатается как `PASS`, `FAIL` или `NOT VERIFIED`; exit code: `0` — все обязательные проверки PASS, `1` — хотя бы один FAIL, `2` — FAIL нет, но есть NOT VERIFIED.

При доступном nftables скрипт сначала проверяет table, chains, `blocked_v4`/`blocked_v6`, interface `pw-egress0` и counters на самих правилах. Перед каждым controlled TCP probe сохраняется packet counter нужного правила; PASS изоляции выдается только при его росте. `connection-refused`, timeout, отсутствие маршрута или другой TCP-result без роста counter являются FAIL, а не доказательством firewall. Public HTTPS проверяется отдельно. `127.0.0.1` внутри контейнера не используется как доказательство host egress firewall.

DNS-rebinding сценарий направляет трафик к `169.254.169.254` после отдельной проверки UrlSecurity с public DNS result и требует роста IPv4 FORWARD counter. Gateway проверяет INPUT counter. Дополнительно тестируется, что попытка из обычной Docker bridge не меняет PriceWatch counter. IPv6 egress нельзя честно сгенерировать при намеренно отключённом IPv6 на этой сети, поэтому этот пункт остаётся NOT VERIFIED до отдельной IPv6-enabled test environment.

Не считать production deployment готовым, пока egress isolation не получила `PASS` на реальном Linux VPS.
