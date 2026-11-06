# Восстановление sing-box и Xray на чистом VPS

Сценарий рассчитан на Ubuntu 24.04, x86-64, systemd и доступ по SSH. Sing-box
поднимает TUN. Xray подключается к провайдеру и выбирает узлы. Пул создаётся
из подписки до включения системной маршрутизации через TUN.

## Что сохранить вне VPS

| Где хранить | Что требуется |
| --- | --- |
| Git | Шаблон TUN, скрипты, systemd units, настройка networkd и эта инструкция |
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
и физический интерфейс из основного IPv4-маршрута. Добавьте CIDR других
административных адресов в `route_exclude_address` в обоих файлах до
включения TUN. Ядро будет направлять трафик к этим адресам мимо TUN.

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
tun = next(inbound for inbound in config['inbounds'] if inbound['type'] == 'tun')
tun.setdefault('route_exclude_address', []).append(str(ssh_client))
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

Если физический интерфейс управляется `systemd-networkd`, установите
[drop-in](../sing-box/networkd/90-sing-box-routing.conf) до запуска TUN.
Он сохраняет правила маршрутизации, созданные sing-box, при перезапуске
networkd. Второй [drop-in](../sing-box/networkd/90-ssh-bypass.conf) направляет
ответы локального SSH-сервера через основную таблицу маршрутизации.
Его приоритет `8990` срабатывает перед правилами TUN с приоритетом от
`9000`. Если SSH слушает другой порт, подставьте его в `SourcePort`.
Устанавливайте настройки при остановленном TUN и доступной консоли провайдера.
Для текущей загрузки добавьте правило SSH через `ip rule`. Drop-in применится
при следующем запуске networkd. Проверьте несколько новых SSH-сеансов.
[Параметры networkd](https://www.freedesktop.org/software/systemd/man/256/networkd.conf.html#ManageForeignRoutingPolicyRules=)

~~~bash
if systemctl is-active --quiet systemd-networkd.service; then
  uplink=$(ip -4 route show default | awk 'NR==1 {for (i=1; i<NF; i++) if ($i=="dev") print $(i+1)}')
  network_file=$(LC_ALL=C networkctl status "$uplink" --no-pager | awk '/Network File:/ {print $NF}')
  if [[ "$network_file" != /*.network ]]; then
    printf '%s\n' 'Не удалось найти .network-файл физического интерфейса.' >&2
    exit 1
  fi
  sudo install -D -m 0644 sing-box/networkd/90-sing-box-routing.conf \
    /etc/systemd/networkd.conf.d/90-sing-box-routing.conf
  sudo install -D -m 0644 sing-box/networkd/90-ssh-bypass.conf \
    "/etc/systemd/network/${network_file##*/}.d/90-ssh-bypass.conf"
  for family in -4 -6; do
    if ! ip "$family" rule show priority 8990 | grep -q .; then
      sudo ip "$family" rule add priority 8990 iif lo ipproto tcp sport 22 lookup main protocol static
    fi
  done
  ip -4 rule show priority 8990
  ip -6 rule show priority 8990
fi
~~~

В выводе обоих семейств ожидается `iif lo ipproto tcp sport 22 lookup main`.
Если приоритет `8990` уже занят другим правилом, разрешите конфликт до запуска
TUN. Установленный `ManageForeignRoutingPolicyRules=no` вступит в силу при
следующем запуске networkd. До этого избегайте его перезапуска при работающем TUN.

Установите units. Шаблон службы обновления разрешает запись в каталог Xray.
Первая загрузка подписки использует подготовленный SOCKS-конфиг sing-box
на диске. Запускать TUN для этого не требуется.

~~~bash
sudo install -m 0644 sing-box/sing-box-tun.service /etc/systemd/system/
sudo install -m 0644 sing-box/failover/templates/{xray-provider-pool.service,singbox-pool-refresh.service,singbox-pool-refresh.timer} /etc/systemd/system/
sudo install -d -m 0755 /etc/systemd/system/sing-box-tun.service.d
printf '[Unit]\nWants=xray-provider-pool.service\nAfter=xray-provider-pool.service\n' |
  sudo tee /etc/systemd/system/sing-box-tun.service.d/xray.conf >/dev/null

sudo systemctl daemon-reload
sudo systemctl disable sing-box-tun.service
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
ip -4 rule show
ip -4 route show table 2022
curl --noproxy '*' --max-time 12 -fsS -o /dev/null https://api.github.com/
~~~

Откройте несколько **новых** SSH-сеансов и проверьте DNS и доступность API по
[инструкции проверки сети](sing-box-host-proxy.md#проверки-сети).
Если доступен второй административный маршрут, проверьте вход и через него.
До запуска TUN убедитесь, что можно восстановить доступ через консоль VPS.
После проверки отмените откат и включите автозапуск Xray и таймера обновления.
TUN запускайте вручную, чтобы обычная перезагрузка возвращала прямой доступ.

~~~bash
sudo systemctl stop sing-box-recovery-rollback.timer
sudo systemctl enable xray-provider-pool.service
sudo systemctl enable --now singbox-pool-refresh.timer
systemctl list-timers singbox-pool-refresh.timer --no-pager
~~~

## Если SSH недоступен

Если в консоли VPS уже открыта командная строка с доступом к `sudo`,
остановите TUN командой `sudo systemctl stop sing-box-tun.service`.

Если вход в ОС настроен только по SSH-ключу, используйте меню GRUB через
виртуальную консоль провайдера. Перезагрузка прервёт работу VPS.

1. При перезагрузке откройте меню GRUB. Выберите обычную запись Ubuntu и
   нажмите `e`.
2. Добавьте `systemd.mask=sing-box-tun.service` в конец строки, которая
   начинается с `linux`.
3. Нажмите `Ctrl+X` и после загрузки подключитесь по SSH прежним ключом.

Параметр действует на одну загрузку. После входа проверьте доступ по SSH
и установите правило для ответов SSH перед включением TUN.
Под маской GRUB `systemctl disable` может пропустить отключение автозапуска.
Удалите ссылку `sudo rm -f /etc/systemd/system/multi-user.target.wants/sing-box-tun.service`
и проверьте `systemctl list-dependencies --reverse sing-box-tun.service`.
Если маска GRUB действует, для ручного запуска используйте временную службу
`sing-box-tun-manual.service`. Сначала запланируйте её остановку по таймеру.

~~~bash
sudo systemd-run --unit=sing-box-tun-manual-rollback --on-active=240s \
  /usr/bin/systemctl stop sing-box-tun-manual.service
sudo systemd-run --unit=sing-box-tun-manual.service --property=Type=exec \
  /usr/local/bin/sing-box run -c /etc/sing-box/config.json
~~~

Откройте несколько новых SSH-сеансов. Проверьте Telegram и запрос к модели,
затем отмените таймер командой
`sudo systemctl stop sing-box-tun-manual-rollback.timer`.
Временная служба существует до перезагрузки. После обычной загрузки
запускайте TUN вручную командой `sudo systemctl start sing-box-tun.service`.
[Параметр systemd.mask](https://www.freedesktop.org/software/systemd/man/latest/systemd-debug-generator.html)
