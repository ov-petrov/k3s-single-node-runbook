# Системный TUN в sing-box и балансировка в Xray

Sing-box принимает трафик VPS через `singtun0` и обслуживает DNS и
direct-исключения, в том числе для SSH. Остальной трафик он передаёт в Xray
через SOCKS на `127.0.0.1:10808`. Xray проверяет VLESS/Reality-узлы и
выбирает доступный выход для каждого нового соединения.

~~~text
Приложение → singtun0 → sing-box → SOCKS 127.0.0.1:10808 → Xray → VLESS/Reality
~~~

`singbox-pool-refresh.timer` обновляет список узлов из подписки каждые 12 часов.
Переключением в этой схеме управляет Xray. Отдельные SOCKS-порты остаются
доступны локально для проверок Telegram и Codex через конкретные узлы.

## Файлы и инструкции

- [Восстановление после wipe](../docs/sing-box-recovery.md) описывает установку
  с нуля и минимальный набор секретов для хранения вне VPS.
- [Управление TUN и Xray](../docs/sing-box-host-proxy.md) описывает маршрутизацию,
  безопасное применение конфигов и диагностику.
- [Подписка провайдера](../docs/sing-box-subscription.md) описывает настройку
  балансировки, обновление пула и хранение секретов.
- [tun-xray.example.json](tun-xray.example.json) служит базой для TUN с Xray.
  [config.example.json](config.example.json) показывает прямой VLESS-выход.
- [Drop-in networkd](networkd/90-sing-box-routing.conf) сохраняет правила TUN
  при перезапуске менеджера сети.
- [Правило SSH](networkd/90-ssh-bypass.conf) направляет ответы SSH напрямую
  через основную таблицу маршрутизации для IPv4 и IPv6.
- [Панель и скилл Hermes](../docs/hermes-dashboard.md) описывает доступ по SSH
  и установку инструкций диагностики VPS.
- [failover/templates](failover/templates/) содержит примеры inventory,
  окружения и systemd units.
- [SHA256SUMS](failover/SHA256SUMS) содержит контрольные суммы скриптов.

Рабочие VLESS-конфиги, закрытая ссылка подписки, файлы OAuth, токены,
inventory, состояние и журналы хранятся на сервере. Примеры используют
`/opt/singbox-failover` и плейсхолдеры для параметров установки.

## Проверка исходников

~~~bash
cd sing-box/failover
shasum -a 256 -c SHA256SUMS
PYTHONDONTWRITEBYTECODE=1 python3 -m unittest discover -s tests -v
~~~
