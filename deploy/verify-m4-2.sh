#!/usr/bin/env bash
# Запуск от администратора Linux VPS из корня репозитория:
#   bash deploy/verify-m4-2.sh
# Скрипт не применяет и не удаляет nftables policy.
set -u

compose=(docker compose)
network_name=pricewatch_egress
expected_subnet=172.30.0.0/24
failures=0
unverified=0

report() { printf '%-14s %s\n' "$1" "$2"; }
pass() { report PASS "$1"; }
fail() { report FAIL "$1"; failures=$((failures + 1)); }
not_verified() { report 'NOT VERIFIED' "$1"; unverified=$((unverified + 1)); }
finish() {
    if [ "$failures" -gt 0 ]; then
        report SUMMARY "$failures FAIL, $unverified NOT VERIFIED"
        exit 1
    fi
    if [ "$unverified" -gt 0 ]; then
        report SUMMARY "no FAIL, $unverified NOT VERIFIED"
        exit 2
    fi
    report SUMMARY 'all required checks PASS'
    exit 0
}
run_check() {
    local label=$1
    shift
    if "$@"; then pass "$label"; else fail "$label"; fi
}

# Внутренний deterministic hook для pytest: проверяет общую семантику exit
# codes без Docker/nftables и не является режимом инфраструктурной проверки.
if [ -n "${VERIFY_M4_2_RESULT_TEST:-}" ]; then
    case "$VERIFY_M4_2_RESULT_TEST" in
        pass) pass 'self-test PASS' ;;
        fail) fail 'self-test FAIL' ;;
        unverified) not_verified 'self-test NOT VERIFIED' ;;
        refused-no-counter) fail 'TCP connection-refused, но nft counter не изменился — не firewall block' ;;
        *) fail 'invalid self-test mode' ;;
    esac
    finish
fi

if ! command -v docker >/dev/null 2>&1 || ! docker info >/dev/null 2>&1; then
    not_verified 'Docker Engine недоступен: Docker/nftables проверки не запускались.'
    finish
fi
if ! "${compose[@]}" version >/dev/null 2>&1; then
    not_verified 'Docker Compose plugin недоступен.'
    finish
fi

run_check 'compose config' "${compose[@]}" config -q
if "${compose[@]}" build; then pass 'image build'; else fail 'image build'; finish; fi
if "${compose[@]}" up --no-start; then pass 'Compose create'; else fail 'Compose create'; finish; fi

subnet=$(docker network inspect "$network_name" --format '{{(index .IPAM.Config 0).Subnet}}' 2>/dev/null || true)
if [ "$subnet" = "$expected_subnet" ]; then pass "dedicated network ($subnet)"; else fail "dedicated network: got ${subnet:-missing}, expected $expected_subnet"; fi
if ip link show pw-egress0 >/dev/null 2>&1; then pass 'dedicated bridge interface'; else fail 'dedicated bridge interface'; fi

run_check 'non-root runtime' "${compose[@]}" run --rm --no-deps --entrypoint sh pricewatch -c '[ "$(id -u)" != 0 ]'
run_check 'read-only rootfs' "${compose[@]}" run --rm --no-deps --entrypoint sh pricewatch -c '! touch /pricewatch-rootfs-probe'

# Проверяется именно service-container: два force-recreate разделены чтением
# volume; volume намеренно не удаляется.
if "${compose[@]}" up --force-recreate --no-build pricewatch && \
   "${compose[@]}" run --rm --no-deps --entrypoint python pricewatch -c 'import os, sqlite3; c=sqlite3.connect(os.environ["PRICEWATCH_DATABASE_PATH"]); assert c.execute("select name from sqlite_master where type=\"table\"").fetchone(); c.close()' && \
   "${compose[@]}" up --force-recreate --no-build pricewatch && \
   "${compose[@]}" run --rm --no-deps --entrypoint python pricewatch -c 'import os, sqlite3; c=sqlite3.connect(os.environ["PRICEWATCH_DATABASE_PATH"]); assert c.execute("select name from sqlite_master where type=\"table\"").fetchone(); c.close()'; then
    pass 'SQLite persistence after service recreate'
else
    fail 'SQLite persistence after service recreate'
fi

container_id=$("${compose[@]}" ps -aq pricewatch | head -n 1)
if [ -n "$container_id" ] && [ "$(docker inspect -f '{{len .HostConfig.PortBindings}}' "$container_id")" = 0 ]; then pass 'no published ports'; else fail 'no published ports'; fi

counter_packets() {
    local chain=$1 comment=$2 payload
    payload=$(nft -j list chain inet pricewatch_egress "$chain" 2>/dev/null) || return 1
    python3 -c '
import json, sys
comment = sys.argv[1]
def counters(value):
    if isinstance(value, dict):
        if "counter" in value and isinstance(value["counter"], dict):
            yield value["counter"]
        for child in value.values():
            yield from counters(child)
    elif isinstance(value, list):
        for child in value:
            yield from counters(child)
for item in json.load(sys.stdin)["nftables"]:
    rule = item.get("rule")
    if rule and rule.get("comment") == comment:
        values = list(counters(rule.get("expr", [])))
        if values:
            print(values[0].get("packets", 0))
            raise SystemExit(0)
raise SystemExit(1)
' "$comment" <<<"$payload"
}

policy_ready=1
if ! command -v nft >/dev/null 2>&1 || ! nft list table inet pricewatch_egress >/dev/null 2>&1; then
    not_verified 'nft table inet pricewatch_egress is not readable/applied.'
    policy_ready=0
else
    for object in blocked_v4 blocked_v6; do
        if ! nft list set inet pricewatch_egress "$object" >/dev/null 2>&1; then
            fail "nft missing set $object"
            policy_ready=0
        fi
    done
    for object in forward input; do
        if ! nft list chain inet pricewatch_egress "$object" >/dev/null 2>&1; then
            fail "nft missing chain $object"
            policy_ready=0
        fi
    done
    for spec in 'forward:pricewatch-forward-v4-block' 'forward:pricewatch-forward-v6-block' 'input:pricewatch-input-host-block'; do
        chain=${spec%%:*}; comment=${spec#*:}
        if ! counter_packets "$chain" "$comment" >/dev/null; then
            fail "nft counter/rule missing: $comment"
            policy_ready=0
        fi
    done
    if [ "$policy_ready" -eq 1 ]; then
        if nft list chain inet pricewatch_egress forward | grep -Fq 'iifname "pw-egress0"' && nft list chain inet pricewatch_egress input | grep -Fq 'iifname "pw-egress0"'; then
            pass 'nft chains, sets, interface and counters'
        else
            fail 'nft rules do not scope pw-egress0'
            policy_ready=0
        fi
    fi
fi

if ! "${compose[@]}" run --rm --no-deps --entrypoint python pricewatch -c 'import os, socket; host=os.environ.get("PRICEWATCH_PUBLIC_PROBE_HOST", "example.com"); s=socket.create_connection((host, 443), 8); s.close()'; then
    fail 'public HTTPS'
else
    pass 'public HTTPS'
fi

container_tcp_result() {
    local host=$1 port=$2
    "${compose[@]}" run --rm --no-deps --entrypoint python pricewatch -c "import errno, socket; host='$host'; family=socket.AF_INET6 if ':' in host else socket.AF_INET; s=socket.socket(family); s.settimeout(4); code=s.connect_ex((host, $port)); s.close(); names={0:'connected', errno.ECONNREFUSED:'connection-refused', errno.ETIMEDOUT:'timeout', errno.ENETUNREACH:'network-unreachable', errno.EHOSTUNREACH:'host-unreachable'}; print(names.get(code, 'error-%s' % code))"
}
counter_probe() {
    local label=$1 chain=$2 comment=$3 host=$4 port=$5 before after outcome
    before=$(counter_packets "$chain" "$comment") || { not_verified "$label: невозможно прочитать counter"; return; }
    outcome=$(container_tcp_result "$host" "$port") || { not_verified "$label: контейнерная попытка не выполнилась"; return; }
    after=$(counter_packets "$chain" "$comment") || { not_verified "$label: невозможно повторно прочитать counter"; return; }
    if [ "$after" -gt "$before" ]; then
        pass "$label: nft counter +$((after - before)) (TCP: $outcome)"
    else
        fail "$label: TCP $outcome, но nft counter не изменился — это не доказательство firewall block"
    fi
}

if [ "$policy_ready" -eq 1 ]; then
    # localhost контейнера намеренно не используется как доказательство host firewall.
    counter_probe 'private IPv4 forwarding' forward pricewatch-forward-v4-block 10.0.0.1 80
    counter_probe 'metadata/DNS-rebinding forwarding' forward pricewatch-forward-v4-block 169.254.169.254 80
    counter_probe 'Docker host/gateway INPUT' input pricewatch-input-host-block 172.30.0.1 80
    not_verified 'IPv6 counter: сеть PriceWatch не имеет IPv6 source; правило проверено статически.'

    image_id=$("${compose[@]}" images -q pricewatch | head -n 1)
    before=$(counter_packets forward pricewatch-forward-v4-block)
    if [ -n "$image_id" ] && docker run --rm --network bridge --entrypoint python "$image_id" -c 'import socket; s=socket.socket(); s.settimeout(2); print(s.connect_ex(("10.0.0.1",80))); s.close()' >/dev/null; then
        after=$(counter_packets forward pricewatch-forward-v4-block)
        if [ "$after" = "$before" ]; then pass 'other Docker network does not hit PriceWatch counter'; else fail 'other Docker network changed PriceWatch counter'; fi
    else
        not_verified 'other Docker network counter isolation could not be executed.'
    fi
else
    not_verified 'real egress and DNS-rebinding probes skipped without verifiable nft policy.'
fi

finish
