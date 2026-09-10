# sing-box: TUN-прокси и автоматическое переключение

Этот каталог содержит обезличенную рабочую конфигурацию `sing-box` для
host-level TUN-прокси, а также копию рабочих скриптов автоматического
переключения с `cloud-ru` от 10 сентября 2026 года.

Файлы:

- `config.example.json` - шаблон `/etc/sing-box/config.json`;
- `sing-box-tun.service` - systemd unit для `/etc/systemd/system/`.
- `failover/scripts/` — контроллер переключения, проверка пула и импорт каталога;
- `failover/templates/` — обезличенные примеры inventory, env и systemd units;
- `failover/SHA256SUMS` — контрольные суммы исходных скриптов с сервера.

Документация:

- [Механика failover](../docs/sing-box-failover.md): алгоритм, критерии здоровья,
  состояние, расписания, ограничения реализации и работа со скриптами;
- [Управление TUN на cloud-ru](../docs/sing-box-host-proxy.md).

Скрипты скопированы без изменения алгоритма. Реальные конфиги подключений,
inventory, env, логи и состояние в репозиторий не включены. Примеры в
`failover/templates/` используют `/opt/singbox-failover`; рабочие пути на
`cloud-ru` приведены в документации. Это снимок реализации, а не новая
установка или исправление обнаруженных ограничений.

В шаблоне все чувствительные значения заменены на заметные плейсхолдеры:

- `<VLESS_SERVER_IP_OR_HOSTNAME>`;
- `12345` в поле `server_port`;
- `<VLESS_CLIENT_UUID>`;
- `<REALITY_TLS_SERVER_NAME>`;
- `<REALITY_PUBLIC_KEY>`;
- `<REALITY_SHORT_ID>`;
- `<GRPC_SERVICE_NAME>`;
- `<VPS_PROVIDER_NETWORK_CIDR>`;
- `<ADMIN_SSH_CLIENT_IP>`;
- `<VLESS_SERVER_IP>`.

Перед установкой замените все плейсхолдеры реальными значениями.

## Установка на сервер

```bash
sudo install -d -m 0755 /etc/sing-box
sudo install -m 0600 config.example.json /etc/sing-box/config.json
sudo install -m 0644 sing-box-tun.service /etc/systemd/system/sing-box-tun.service

sudo /usr/local/bin/sing-box check -c /etc/sing-box/config.json
sudo systemctl daemon-reload
```

Первый запуск лучше делать с rollback:

На сервере с установленным failover предварительно остановите его таймер и
дождитесь завершения контроллера. Таймер rollback ниже только останавливает
TUN; прежний файл конфига он не восстанавливает.

```bash
sudo systemd-run \
  --unit=sing-box-rollback \
  --on-active=180s \
  /bin/systemctl stop sing-box-tun.service

sudo systemctl start sing-box-tun.service
```

Если SSH, DNS и внешние API работают, rollback можно отменить:

```bash
sudo systemctl stop sing-box-rollback.timer sing-box-rollback.service
sudo systemctl enable sing-box-tun.service
```

## Проверки

```bash
sudo systemctl status sing-box-tun.service --no-pager
getent hosts api.telegram.org
curl --connect-timeout 8 --max-time 15 https://api.telegram.org/botINVALID/getMe
curl --connect-timeout 8 --max-time 15 https://api.openai.com/v1/models
```

Ожидаемые признаки рабочей маршрутизации:

- Telegram API с invalid token возвращает `404 Not Found`;
- OpenAI API без токена возвращает `401 Missing bearer authentication`;
- в логах `sing-box` видны `inbound/tun`, `dns: exchanged` и
  `outbound/vless[proxy-out]`.
