# Системный прокси VPS

Sing-box поднимает интерфейс `singtun0`, применяет правила DNS и сохраняет
прямые маршруты для административного доступа. Xray обслуживает Reality и
выбирает узел провайдера.

~~~text
Обычный трафик → singtun0 → sing-box → SOCKS 127.0.0.1:10808 → Xray → VLESS/Reality
~~~

## Маршрутизация

В sing-box DNS-пакеты на TCP/UDP-порт 53, попавшие в TUN, обрабатываются
действием `hijack-dns`. Локальный stub resolver может принять запрос раньше.
При диагностике проверяйте системный resolver и upstream DNS отдельно.

В шаблоне sing-box типизированный UDP DNS без `detour` использует прямое
подключение. После DNS-правила срабатывают direct-исключения, в том числе
локальные сети, сеть провайдера VPS и адрес клиента SSH. Остальные запросы
обрабатывает `route.final: proxy-out`. Xray привязывает подключения к
физическому интерфейсу через `sockopt.interface`, чтобы они обходили TUN.

TUN принимает IP-пакеты от ОС. VLESS и Reality работают на внешнем участке
от Xray до узла провайдера. VLESS передаёт запросы прокси, а Reality защищает
соединение с узлом.

[Параметры TUN sing-box](https://sing-box.sagernet.org/configuration/inbound/tun/)
· [Правила маршрутизации sing-box](https://sing-box.sagernet.org/configuration/route/)
· [DNS UDP](https://sing-box.sagernet.org/configuration/dns/server/udp/)

## Управление

Рабочая конфигурация TUN хранится в `/etc/sing-box/config.json`, а конфигурация
пула Xray — в `/etc/xray/provider-pool.json`. Пул генерирует
[обновление подписки](sing-box-subscription.md).

~~~bash
sudo systemctl status sing-box-tun.service xray-provider-pool.service --no-pager
systemctl list-timers 'singbox-*' --all --no-pager
sudo journalctl -u sing-box-tun.service -u xray-provider-pool.service -n 50 --no-pager
~~~

При ручной работе с конфигами остановите таймер обновления и дождитесь
завершения текущего запуска `singbox-pool-refresh.service`.

Проверьте подготовленные конфиги перед применением.

~~~bash
sudo /usr/local/bin/sing-box check -c /etc/sing-box/config.json
sudo /usr/local/bin/xray run -test -config /etc/xray/provider-pool.json
~~~

Для изменения рабочего маршрута сохраните копии обоих конфигов и запланируйте
через `systemd-run --on-active=180s` восстановление копий и перезапуск Xray,
затем sing-box. Отменяйте таймер отката после проверки нового SSH-сеанса,
DNS и сетевых запросов. Для первого запуска на новом VPS вместо восстановления
можно запланировать остановку TUN.

Для новой установки подготовьте конфиг из
[config.example.json](../sing-box/config.example.json) с параметрами своего
сервера. Разместите готовый файл в `/etc/sing-box/config.json` с правами
`0600`, установите [unit sing-box](../sing-box/sing-box-tun.service)
в `/etc/systemd/system/` и выполните `sudo systemctl daemon-reload`.
Если используется Xray, сначала подготовьте его пул по
[инструкции установки](sing-box-subscription.md#установка-поверх-работающего-tun).

~~~bash
sudo systemd-run --unit=sing-box-rollback --on-active=180s \
  /bin/systemctl stop sing-box-tun.service
sudo systemctl start sing-box-tun.service

# Выполните после проверки нового SSH-сеанса и сети.
sudo systemctl stop sing-box-rollback.timer
~~~

## Проверки сети

~~~bash
ip -brief link show singtun0
ss -ltn 'sport = :10808'
getent hosts api.telegram.org
resolvectl status

curl --noproxy '*' --max-time 12 -sS -o /dev/null -w '%{http_code}\n' \
  https://api.telegram.org/botINVALID/getMe
curl --noproxy '*' --max-time 12 -sS -o /dev/null -w '%{http_code}\n' \
  https://chatgpt.com/backend-api/codex/responses
~~~

Telegram возвращает `404` с заведомо неверным токеном. Codex может вернуть
`405` на GET к endpoint, который принимает POST. Такие ответы проверяют
доступность API. Проверку действующего токена выполняйте через `getMe`, а
проверку LLM — коротким запросом Hermes от владельца OAuth-сессии.

Если системный запрос не проходит, сравните его с запросом через локальный
SOCKS Xray.

~~~bash
curl --noproxy '' --socks5-hostname 127.0.0.1:10808 --max-time 12 \
  -sS -o /dev/null -w '%{http_code}\n' https://api.github.com/
~~~

## Остановка

~~~bash
sudo systemctl disable --now singbox-pool-refresh.timer
# Дождитесь завершения обновления подписки перед остановкой прокси.
sudo systemctl disable --now sing-box-tun.service xray-provider-pool.service
~~~
