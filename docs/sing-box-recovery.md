# Восстановление sing-box и Xray на чистом VPS

Сценарий рассчитан на Ubuntu 24.04, x86-64, systemd и доступ по SSH. Sing-box
поднимает TUN. Xray подключается к провайдеру и выбирает узлы. Пул создаётся
из подписки до включения системной маршрутизации через TUN.

## Что сохранить вне VPS

| Где хранить | Что требуется |
| --- | --- |
| Git | Шаблон TUN, скрипты, systemd units и эта инструкция |
| Менеджер паролей или шифрованный бэкап вне VPS | Полная HTTPS-ссылка подписки и токен Telegram-бота для `getMe` |
| Канал восстановления доступа | SSH-ключ на своей машине и вход в консоль провайдера VPS |

Проверьте, что к хранилищу секретов можно войти при потере VPS. Для быстрого
старта достаточно двух секретов. Пул Xray, каталог, inventory и результаты
проверок собираются заново. Если нужна возможность восстановления при
недоступности сервера подписки, отдельно храните шифрованную копию последнего
рабочего пула вне VPS. Она содержит учётные данные и годится, пока профили
принимает провайдер. Для Hermes после восстановления сети отдельно выполните
вход под его пользователем.

## Установка бинарников

Подключитесь по SSH и убедитесь, что доступна консоль провайдера. Установите
зависимости и перенесите проверенную ревизию проекта из Git или с рабочей
машины. Остальные команды выполняйте из корня этого клона.

~~~bash
sudo apt-get update
sudo apt-get install -y ca-certificates curl git python3-yaml unzip
~~~

Пример использует проверенные на VPS релизы sing-box 1.14.4 и Xray 26.10.31.
При использовании других релизов повторите проверки конфигурации и сети.
[Релиз sing-box](https://github.com/SagerNet/sing-box/releases/tag/v1.14.4) ·
[Релиз Xray](https://github.com/XTLS/Xray-core/releases/tag/v26.10.31)

~~~bash
(
  set -euo pipefail
  work=$(mktemp -d)
  trap 'rm -rf -- "$work"' EXIT
  cd "$work"

  fetch_asset() {
    local repo=$1 tag=$2 asset=$3 digest
    digest=$(curl -fsSL "https://api.github.com/repos/$repo/releases/tags/$tag" |
      python3 -c 'import json, sys
assets = json.load(sys.stdin)["assets"]
digest = next(a["digest"] for a in assets if a["name"] == sys.argv[1])
if not digest or not digest.startswith("sha256:"):
    raise SystemExit("GitHub release has no SHA-256 digest for this asset")
print(digest.split(":", 1)[1])' "$asset")
    curl -fsSL -o "$asset" "https://github.com/$repo/releases/download/$tag/$asset"
    printf '%s  %s\n' "$digest" "$asset" | sha256sum -c -
  }

  fetch_asset SagerNet/sing-box v1.14.4 sing-box-1.14.4-linux-amd64.tar.gz
  fetch_asset XTLS/Xray-core v26.10.31 Xray-linux-64.zip
  tar -xzf sing-box-1.14.4-linux-amd64.tar.gz sing-box-1.14.4-linux-amd64/sing-box
  unzip -q Xray-linux-64.zip xray
  sudo install -m 0755 sing-box-1.14.4-linux-amd64/sing-box /usr/local/bin/sing-box
  sudo install -m 0755 xray /usr/local/bin/xray
)
~~~

## Конфиги и секреты

Создайте root-only каталоги и установите скрипты. Команды предназначены
для новой установки.

~~~bash
sudo install -d -m 0700 /opt/singbox-failover /etc/singbox-failover /etc/xray
sudo install -d -m 0700 /opt/singbox-failover/{scripts,generated}
sudo install -d -m 0755 /etc/sing-box
sudo install -m 0644 sing-box/failover/scripts/*.py /opt/singbox-failover/scripts/
sudo install -m 0600 /dev/null /etc/singbox-failover/subscription.url
sudo install -m 0600 /dev/null /opt/singbox-failover/failover.env
sudoedit /etc/singbox-failover/subscription.url /opt/singbox-failover/failover.env
~~~

В первый файл вставьте URL одной строкой. Во второй добавьте строку
`TELEGRAM_BOT_TOKEN=<значение из хранилища>`. Так значения не попадают в
аргументы команд или историю shell.

Подготовьте TUN и inventory. Команды берут адрес из текущего SSH-сеанса
и физический интерфейс из основного IPv4-маршрута. Если администрируете VPS
через несколько адресов или отдельную сеть, внесите их CIDR в direct-правило
обоих файлов до включения TUN.

~~~bash
(
  set -euo pipefail
  read -r ssh_peer _ <<< "${SSH_CONNECTION:?Выполните из SSH-сеанса}"
  uplink=$(ip -4 route show default | awk 'NR==1 {for (i=1; i<NF; i++) if ($i=="dev") print $(i+1)}')
  test -n "$uplink"
  sudo python3 - "$ssh_peer" "$uplink" <<'PY'
import ipaddress
import json
import os
import sys
from pathlib import Path

import yaml

os.umask(0o077)
home = Path('/opt/singbox-failover')
config = json.loads(Path('sing-box/tun-xray.example.json').read_text())
ssh_client = ipaddress.ip_network(sys.argv[1], strict=False)
direct = next(rule for rule in config['route']['rules'] if rule.get('outbound') == 'direct')
direct['ip_cidr'].append(str(ssh_client))
payload = json.dumps(config, indent=2) + '\n'
(home / 'base-config.json').write_text(payload)
Path('/etc/sing-box/config.json').write_text(payload)

inventory = yaml.safe_load(Path('sing-box/failover/templates/inventory.example.yaml').read_text())
inventory['configs'] = []
inventory['runtime'].update(
    transport_backend='xray',
    xray_config_path='/etc/xray/provider-pool.json',
    xray_outbound_interface=sys.argv[2],
    xray_balancer_port=10808,
)
(home / 'inventory.yaml').write_text(yaml.safe_dump(inventory, sort_keys=False))
PY
)
~~~

## Первый запуск и проверки

Установите units и разрешите сервису обновления запись в каталог Xray.
Первая загрузка подписки использует подготовленный SOCKS-конфиг sing-box
на диске. Запускать TUN для этого не требуется.

~~~bash
sudo install -m 0644 sing-box/sing-box-tun.service /etc/systemd/system/
sudo install -m 0644 sing-box/failover/templates/{xray-provider-pool.service,singbox-pool-refresh.service,singbox-pool-refresh.timer} /etc/systemd/system/
sudo install -d -m 0755 /etc/systemd/system/{singbox-pool-refresh.service.d,sing-box-tun.service.d}
printf '[Service]\nReadWritePaths=/etc/xray\n' |
  sudo tee /etc/systemd/system/singbox-pool-refresh.service.d/xray.conf >/dev/null
printf '[Unit]\nWants=xray-provider-pool.service\nAfter=xray-provider-pool.service\n' |
  sudo tee /etc/systemd/system/sing-box-tun.service.d/xray.conf >/dev/null

sudo systemctl daemon-reload
sudo /usr/local/bin/sing-box check -c /etc/sing-box/config.json
sudo systemctl start singbox-pool-refresh.service
sudo /usr/local/bin/xray run -test -config /etc/xray/provider-pool.json
sudo journalctl -u singbox-pool-refresh.service -n 15 --no-pager
curl --noproxy '' --socks5-hostname 127.0.0.1:10808 --max-time 12 -fsS -o /dev/null https://api.github.com/
~~~

Убедитесь, что в событии `pool_refreshed` есть хотя бы один здоровый профиль.
Затем запланируйте остановку TUN на случай потери SSH и запустите его.

~~~bash
sudo systemd-run --unit=sing-box-recovery-rollback --on-active=180s \
  /bin/systemctl stop sing-box-tun.service
sudo systemctl start sing-box-tun.service
ip -brief link show singtun0
curl --noproxy '*' --max-time 12 -fsS -o /dev/null https://api.github.com/
~~~

Откройте **новый** SSH-сеанс и проверьте DNS и доступность API по
[инструкции проверки сети](sing-box-host-proxy.md#проверки-сети). Только после
этого отмените откат и включите автозапуск.

~~~bash
sudo systemctl stop sing-box-recovery-rollback.timer
sudo systemctl enable xray-provider-pool.service sing-box-tun.service
sudo systemctl enable --now singbox-pool-refresh.timer
systemctl list-timers singbox-pool-refresh.timer --no-pager
~~~
