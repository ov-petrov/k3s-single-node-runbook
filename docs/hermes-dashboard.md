# Панель Hermes через SSH

Панель управления работает как пользовательский сервис systemd и слушает
`127.0.0.1:9119` на VPS. Туннель SSH открывает этот же адрес на ноутбуке.
В таком режиме доступ контролируется SSH, а у самой панели отдельного
пароля нет. Любой процесс с доступом к loopback на одной из машин может
обратиться к панели, поэтому эта схема рассчитана на личный VPS и ноутбук.

## Установка на VPS

Из корня репозитория выполните команды от владельца профиля Hermes. Шаблон
ожидает CLI в `~/.local/bin/hermes` и готовые файлы интерфейса. Проверьте
наличие команды `hermes dashboard --help` перед запуском.

~~~bash
install -d -m 0700 "$HOME/.config/systemd/user"
install -m 0644 hermes/systemd/hermes-dashboard.service "$HOME/.config/systemd/user/"
systemctl --user daemon-reload
systemctl --user enable --now hermes-dashboard.service
systemctl --user status hermes-dashboard.service --no-pager
ss -ltn 'sport = :9119'
~~~

В выводе `ss` ожидается только `127.0.0.1:9119`. Для запуска после
перезагрузки до первого входа включите linger для владельца профиля.

~~~bash
sudo loginctl enable-linger "$(id -un)"
~~~

## Доступ с ноутбука

Подставьте свой SSH-алиас вместо `VPS_ALIAS` и оставьте команду запущенной,
пока пользуетесь панелью.

~~~bash
ssh -NT -o ExitOnForwardFailure=yes -L 127.0.0.1:9119:127.0.0.1:9119 VPS_ALIAS
~~~

Откройте [панель на локальном порту](http://127.0.0.1:9119).
[Документация Hermes](https://hermes-agent.nousresearch.com/docs/user-guide/features/web-dashboard)

## Скилл диагностики VPS

Чтобы добавить [инструкцию для Hermes](../hermes/skills/sing-box-api-failover/SKILL.md),
установите только `SKILL.md`. В действующей установке одноимённый каталог
может содержать скрипты, на которые ссылается сервис обновления подписки.

~~~bash
skill_dir="$HOME/.hermes/skills/software-development/sing-box-api-failover"
install -d -m 0700 "$skill_dir"
install -m 0600 hermes/skills/sing-box-api-failover/SKILL.md "$skill_dir/"
~~~

Скилл описывает проверки TUN, балансировщика и обновления подписки.
Файлы OAuth, токены, настройки личного профиля и журналы хранятся на VPS.
