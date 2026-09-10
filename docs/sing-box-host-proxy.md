# sing-box host proxy на cloud-ru

Краткая памятка по `sing-box` на сервере `cloud-ru`.

Актуализирована 10 сентября 2026 года. Механизм автоматического переключения,
его рабочие скрипты и ограничения описаны отдельно:
[sing-box failover](sing-box-failover.md).

## Что сейчас настроено

Есть два варианта запуска:

- старый вариант: Docker container `task-tracker-sing-box`;
- новый вариант: host-level systemd service `sing-box-tun.service`.

Новый вариант нужен для сценария "весь исходящий трафик сервера идет через
прокси", без использования Docker-контейнера как отдельного proxy endpoint.

Файлы на сервере:

```text
/usr/local/bin/sing-box
/etc/sing-box/config.json
/etc/systemd/system/sing-box-tun.service
/home/petrovov/planning-bot/sing-box/vless-outbound.json
```

Важно: `vless-outbound.json` содержит чувствительные параметры подключения
к proxy endpoint. Не коммитить его в Git.

Зафиксированное рабочее состояние (10 сентября 2026 года; это не live-статус):

```text
sing-box-tun.service: active
autostart: enabled
task-tracker-sing-box Docker container: stopped
```

Для TUN в диагностике зафиксирована версия sing-box `1.12.20`. На момент
изучения контроллера основной конфиг — `cfg-03`; в последнем сохранённом
обследовании пула здоров только он из семи кандидатов. Автоматика работает
через `singbox-failover.timer` (5 минут) и `singbox-pool-refresh.timer` (12 часов).
Состояние старого Docker-контейнера выше взято из прежней памятки и при
актуализации отдельно не перепроверялось.

DNS обрабатывается самим `sing-box`: запросы на порт `53` перехватываются
правилом `hijack-dns`, а затем резолвятся через DNS-секцию конфига.

## Команды управления

Проверить статус:

```bash
sudo systemctl status sing-box-tun.service --no-pager
```

Запустить:

```bash
sudo systemctl start sing-box-tun.service
```

Остановить:

```bash
sudo systemctl stop sing-box-tun.service
```

Перезапустить после изменения конфига:

```bash
sudo systemctl restart sing-box-tun.service
```

Посмотреть логи:

```bash
sudo journalctl -u sing-box-tun.service -n 100 --no-pager
```

Включить автозапуск:

```bash
sudo systemctl enable sing-box-tun.service
```

Отключить автозапуск:

```bash
sudo systemctl disable sing-box-tun.service
```

## Безопасный запуск с rollback

TUN-прокси меняет маршрутизацию сервера. Ошибка в правилах может сломать SSH
или DNS. Поэтому первый запуск лучше делать с автоматическим откатом:

Если failover уже установлен, сначала остановите его таймер и дождитесь
завершения контроллера. Иначе он может перезапустить TUN после срабатывания
таймера остановки. Этот rollback только останавливает сервис и не восстанавливает
прежний файл конфигурации; его резервную копию нужно сохранять отдельно.

```bash
sudo systemd-run \
  --unit=sing-box-rollback \
  --on-active=180s \
  /bin/systemctl stop sing-box-tun.service

sudo systemctl start sing-box-tun.service
```

Если после запуска SSH жив, DNS работает и проверки проходят, rollback можно
отменить:

```bash
sudo systemctl stop sing-box-rollback.timer sing-box-rollback.service
```

Если SSH отвалился, через 180 секунд systemd сам остановит
`sing-box-tun.service`.

## Как править конфиг

При ручном обслуживании сначала остановите таймер контроллера и дождитесь
завершения уже запущенного `singbox-failover.service`. Остановка таймера сама
по себе не прерывает текущий запуск. Иначе контроллер может заменить конфиг
параллельно с ручными изменениями. После обслуживания нужно сверить live-конфиг
с inventory и согласовать `state.active_name` перед возобновлением таймера;
[подробнее о состоянии](sing-box-failover.md#состояние-и-определение-активного-конфига).

Основной конфиг:

```bash
sudo nano /etc/sing-box/config.json
```

Проверка перед применением:

```bash
sudo /usr/local/bin/sing-box check -c /etc/sing-box/config.json
```

Применение:

```bash
sudo systemctl restart sing-box-tun.service
sudo systemctl status sing-box-tun.service --no-pager
```

Если меняется только VLESS outbound, удобно сначала править исходный фрагмент:

```bash
nano /home/petrovov/planning-bot/sing-box/vless-outbound.json
python3 -m json.tool /home/petrovov/planning-bot/sing-box/vless-outbound.json >/dev/null
```

Это путь из первоначальной ручной установки. Контроллер не читает этот
фрагмент: он копирует полные конфиги из путей в `inventory.yaml`.
Изменение фрагмента само по себе не обновляет ни активный конфиг, ни пул.

Важный момент: в host-конфиге outbound должен иметь:

```json
"tag": "proxy-out"
```

Потому что `route.final` отправляет обычный трафик именно в outbound
`proxy-out`.

## Как это работает

На пальцах:

```text
приложение на сервере
  -> обычный сетевой запрос
  -> TUN-интерфейс singtun0
  -> sing-box
  -> VLESS/Reality proxy endpoint
  -> интернет
```

`TUN` - это виртуальная сетевая карта. Для приложений на сервере все выглядит
так, будто они просто ходят в интернет. На самом деле пакеты сначала попадают в
`sing-box`, а он решает, что делать дальше.

В конфиге есть два outbound:

- `proxy-out` - VLESS-подключение к удаленному proxy endpoint;
- `direct` - обычный прямой выход в интернет без прокси.

`route.final = proxy-out` означает: если правило не сказало иное, отправлять
трафик через прокси.

DNS настроен отдельно:

```text
DNS-запрос приложения
  -> TUN
  -> sing-box route action hijack-dns
  -> sing-box dns server google-dns
  -> ответ приложению
```

Правило применяется к DNS-пакетам, уже попавшим в TUN. Запрос приложения к
локальному stub `127.0.0.53:53` может быть доставлен локально; в TUN затем
попадает запрос самого resolver к внешнему DNS. Поэтому одного наличия
`hijack-dns` недостаточно, чтобы утверждать, что весь локальный DNS перехвачен:
нужно учитывать системные маршруты и настройки resolver.

В конфиге используется типизированный UDP DNS-сервер без `detour`:
его upstream-соединение по умолчанию прямое, независимо от `route.final`.
См. [DNS UDP](https://sing-box.sagernet.org/configuration/dns/server/udp/).

Direct-исключения нужны, чтобы не сломать сам сервер:

- локальные сети `10.0.0.0/8`, `172.16.0.0/12`, `192.168.0.0/16`;
- loopback `127.0.0.0/8`;
- сеть провайдера VPS;
- IP SSH-клиента, чтобы не потерять текущую SSH-сессию;
- IP VLESS-серверов, чтобы соединения к этим адресам, попавшие в TUN,
  направлялись прямо;
- DNS-серверы для прямых соединений, не завершённых предыдущим DNS-перехватом.

Кроме списка исключений, `route.auto_detect_interface = true` привязывает
исходящие соединения sing-box к основному сетевому интерфейсу и предотвращает
возврат соединения в TUN. Правило `outbound: direct` не равнозначно исключению
адреса из системных маршрутов TUN. См. [Route](https://sing-box.sagernet.org/configuration/route/).

## Что такое proxy

Proxy - это посредник. Вместо того чтобы сервер шел к Telegram напрямую, он
идет к proxy endpoint. Endpoint уже сам идет к Telegram и возвращает ответ.

Пример:

```text
cloud-ru -> proxy endpoint -> api.telegram.org
```

Для Telegram видно соединение со стороны proxy endpoint, а не напрямую с
`cloud-ru`.

## Что такое VLESS

VLESS - это протокол проксирования из семейства Xray/V2Ray. В нашем случае
`sing-box` на `cloud-ru` выступает клиентом VLESS:

```text
sing-box client -> VLESS server -> нужный сайт
```

В VLESS outbound важны:

- `server` и `server_port` - куда подключаться;
- `uuid` - идентификатор клиента;
- `transport` - как упакован трафик, сейчас используется `grpc`;
- `tls` - шифрование и маскировка соединения.

## Что такое Reality

Reality - это режим TLS-маскировки. Идея простая: соединение выглядит как
обычный TLS-трафик к известному домену, например к `discord.com`, но внутри
используется проверка ключом Reality.

В конфиге это задают поля:

- `tls.server_name` - домен, под который выглядит TLS-соединение;
- `tls.utls.fingerprint` - имитация TLS-поведения обычного браузера;
- `tls.reality.public_key` - публичный ключ Reality-сервера;
- `tls.reality.short_id` - короткий идентификатор Reality-сессии.

Упрощенно:

```text
обычный TLS снаружи
VLESS-туннель внутри
Reality проверяет, что клиент и сервер "свои"
```

## Проверки

Проверка Telegram с сервера:

```bash
curl --connect-timeout 8 --max-time 15 \
  https://api.telegram.org/botINVALID/getMe
```

Ожидаемый результат при рабочей сети:

```text
{"ok":false,"error_code":404,"description":"Not Found"}
```

`404` здесь нормально: токен заведомо неверный, но сам Telegram API доступен.

Проверка маршрутов и TUN:

```bash
ip route
ip addr show singtun0
```

Проверка DNS:

```bash
getent hosts api.telegram.org
resolvectl status
```

Если `curl` зависает на `Resolving timed out`, сбой произошёл на стадии DNS.
Это ещё не устанавливает первопричину: проверьте resolver, upstream и
маршрутизацию. В `/etc/sing-box/config.json` должно быть правило:

```json
{
  "network": ["udp", "tcp"],
  "port": 53,
  "action": "hijack-dns"
}
```

и секция `dns.servers`.

## Быстро отключить все

Сначала отключите автоматику, иначе очередная проверка может снова запустить
TUN при применении кандидата:

```bash
sudo systemctl disable --now singbox-failover.timer singbox-pool-refresh.timer
sudo systemctl stop singbox-failover.service singbox-pool-refresh.service
```

Прерывание уже выполняющегося переключения может оставить файл конфига
изменённым; перед следующим включением сверяйте его с inventory и состоянием.

```bash
sudo systemctl stop sing-box-tun.service
sudo systemctl disable sing-box-tun.service
```

Если был запущен старый Docker-контейнер:

```bash
docker stop task-tracker-sing-box
```
