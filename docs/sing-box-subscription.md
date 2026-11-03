# Подписка VLESS и обновление пула Xray

`singbox_subscription_refresh.py` получает список узлов по закрытой HTTPS-ссылке.
Поддерживаются текстовый или base64-список VLESS URI, JSON с массивом
`outbounds` и JSON-массив профилей sing-box. Размер ответа ограничен 4 MiB.
Адаптер Xray использует VLESS с Reality и преобразует TCP, gRPC и WebSocket.

## Хранение секретов

Храните полный URL подписки в `/etc/singbox-failover/subscription.url` с
владельцем root и правами `0600`. Формат файла указан в
[subscription.url.example](../sing-box/failover/templates/subscription.url.example).
URL читает сам скрипт из файла. Telegram-токен задаётся через защищённый
`failover.env`, который systemd читает как `EnvironmentFile`.

Конфиги VLESS, каталог подписки и `/etc/xray/provider-pool.json` содержат
параметры доступа. Держите их вне Git вместе с журналами проверок,
файлами OAuth и резервными копиями.

## Схема обновления

1. Refresh берёт блокировку `/run/singbox-failover.lock`, скачивает подписку,
   разбирает список и присваивает профилям устойчивые идентификаторы.
2. Из базового TUN-конфига строятся кандидаты с выделенными локальными
   SOCKS-портами. Скрипт проверяет кандидатов через `sing-box check` и пул
   через `xray run -test`.
3. Если пул изменился, скрипт атомарно заменяет файл Xray и перезапускает
   сервис. При запуске с `--refresh-pool` отдельно проверяются Telegram
   `getMe` и доступность Codex через каждого кандидата. Если ни один не
   проходит или проверка завершается ошибкой, скрипт возвращает прежний пул.
4. После проверки публикуются результаты, каталог и inventory. Метаданные
   загрузки сохраняются после публикации inventory. При HTTP 304 скрипт
   повторяет проверку известных кандидатов с `--refresh-pool`.

Имя кандидата зависит от параметров VLESS, поэтому смена отображаемой метки
провайдера сохраняет его идентификатор. Сервис refresh не заменяет
`/etc/sing-box/config.json`.

## Балансировка

Sing-box направляет обычный трафик на общий SOCKS-порт `127.0.0.1:10808`.
Xray проверяет выходы через `observatory` с интервалом 30 секунд и отправляет
новые соединения через доступный узел с наименьшей измеренной задержкой
(`leastPing`). Если измерений ещё нет или ни один выход не прошёл проверку,
Xray пробует первый профиль из подписки. Проверка использует HTTPS-запрос к
`www.gstatic.com/generate_204`. Проверки Telegram и Codex при обновлении пула
выполняются отдельно и не определяют выбор балансировщика.

[Проверки Xray](https://xtls.github.io/en/config/observatory.html)
· [Балансировщик Xray](https://xtls.github.io/en/config/routing.html#balancerobject)

## Установка поверх работающего TUN

Нужны Python 3.10+, PyYAML, curl и официальные бинарники
`/usr/local/bin/sing-box` и `/usr/local/bin/xray`. Шаблоны используют
`/opt/singbox-failover`. Для существующей установки задайте её реальные пути
вместо этого каталога во всех связанных файлах.

1. Разместите [скрипты](../sing-box/failover/scripts/) в каталоге, доступном
   для записи только root. Подготовьте файл подписки, закрытый env и
   `base-config.json` со своими правилами DNS и direct-исключениями.
   [Инструкция по TUN](sing-box-host-proxy.md)
2. Адаптируйте [пример inventory](../sing-box/failover/templates/inventory.example.yaml).
   В новой установке задайте `configs: []`. Укажите имя физического интерфейса
   вместо `eth0` и выберите свободный порт вне диапазона кандидатов 20000–29999.

   ~~~yaml
   runtime:
     transport_backend: xray
     xray_config_path: /etc/xray/provider-pool.json
     xray_outbound_interface: eth0
     xray_balancer_port: 10808
   ~~~

3. Создайте каталог `/etc/xray` с владельцем root и правами `0750`.
   Установите [сервис Xray](../sing-box/failover/templates/xray-provider-pool.service),
   [сервис обновления](../sing-box/failover/templates/singbox-pool-refresh.service)
   и [таймер](../sing-box/failover/templates/singbox-pool-refresh.timer).
   Для `singbox-pool-refresh.service` добавьте drop-in.

   ~~~ini
   [Service]
   ReadWritePaths=/etc/xray
   ~~~

   Для `sing-box-tun.service` добавьте зависимость от Xray.

   ~~~ini
   [Unit]
   Wants=xray-provider-pool.service
   After=xray-provider-pool.service
   ~~~

4. Остановите старый таймер переключения и дождитесь завершения контроллера.
   Перечитайте units через `systemctl daemon-reload` и вручную запустите
   `singbox-pool-refresh.service`. Для первого обновления после смены backend
   сохраните прежние метаданные отдельно, чтобы refresh скачал полный список.
   Проверьте, что он создал пул и общий SOCKS-порт отвечает.
5. В живом конфиге TUN замените `proxy-out` на SOCKS-выход. Сохраните
   `route.final`, DNS и direct-правила. Проверьте конфиг и перезапустите
   TUN с [автоматическим откатом](sing-box-host-proxy.md#управление).

   ~~~json
   {
     "type": "socks",
     "tag": "proxy-out",
     "server": "127.0.0.1",
     "server_port": 10808,
     "version": "5",
     "bind_interface": "lo"
   }
   ~~~

6. После проверки нового SSH-сеанса и сетевого доступа включите автозапуск
   `xray-provider-pool.service` и `sing-box-tun.service`, затем запустите
   `systemctl enable --now singbox-pool-refresh.timer`. Таймер обновляет
   подписку каждые 12 часов. Для переключения профилей используется Xray.

## Проверка исходников

~~~bash
cd sing-box/failover
shasum -a 256 -c SHA256SUMS
PYTHONDONTWRITEBYTECODE=1 python3 -m unittest discover -s tests -v
~~~

Тесты используют фиктивные параметры VLESS и адреса из документационных
диапазонов.
