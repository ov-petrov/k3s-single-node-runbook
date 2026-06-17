# Single-node K3s on Ubuntu VPS

Практический runbook для установки учебного single-node Kubernetes-кластера
на базе K3s с безопасным доступом через SSH-туннель.

Инструкция рассчитана на новый VPS с Ubuntu 24.04 и содержит воспроизводимые
команды, пояснения, диагностику и проверку self-healing.

## Оглавление

- [Что получится](#что-получится)
- [Требования](#требования)
- [Установка](#установка)
  - [1. Обновление Ubuntu](#1-обновление-ubuntu)
  - [2. Проверка предпосылок](#2-проверка-предпосылок)
  - [3. Защита Kubernetes API](#3-защита-kubernetes-api)
  - [4. Установка K3s](#4-установка-k3s)
  - [5. Проверка кластера на сервере](#5-проверка-кластера-на-сервере)
  - [6. Безопасный доступ с MacBook](#6-безопасный-доступ-с-macbook)
  - [7. Тестовое приложение](#7-тестовое-приложение)
  - [8. Полезная диагностика](#8-полезная-диагностика)
  - [9. Проверка текущего состояния](#9-проверка-текущего-состояния)
  - [10. Учебный стенд Kafka, PostgreSQL, ActiveMQ и интеграционного сервиса](#10-учебный-стенд-kafka-postgresql-activemq-и-интеграционного-сервиса)
  - [11. Экспорт логов в Elasticsearch через Vector](#11-экспорт-логов-в-elasticsearch-через-vector)
  - [Ошибка, обнаруженная во время установки](#ошибка-обнаруженная-во-время-установки)
- [Полезные ссылки](#полезные-ссылки)
- [Лицензия](#лицензия)

## Что получится

- single-node K3s, где VPS одновременно выполняет роли control plane и worker;
- CoreDNS, metrics-server, local-path-provisioner и Traefik;
- закрытые от публичного интернета порты `80`, `443` и `6443`;
- административный доступ `kubectl` с ноутбука через SSH-туннель;
- тестовый Deployment с двумя nginx Pods и внутренним Service.

## Требования

- Ubuntu 24.04;
- минимум 2 vCPU и 2 GiB RAM;
- рекомендуется минимум 15 GiB свободного места;
- root-доступ через `sudo`;
- SSH-доступ по ключу;
- `kubectl` на локальном компьютере.

В примерах используется SSH-алиас `k3s-vps`. Его можно добавить на локальном
компьютере в `~/.ssh/config`:

```sshconfig
Host k3s-vps
  HostName <PUBLIC_VPS_IP>
  User <SSH_USER>
  IdentityFile ~/.ssh/id_ed25519
```

> Важно: проверьте имя публичного сетевого интерфейса командой
> `ip -brief address`. В примерах используется `enp3s0`; на вашем VPS имя
> может отличаться.

## Установка

Ниже приведены команды для установки одноузлового K3s на новый VPS.

### 1. Обновление Ubuntu

Обновляем индексы пакетов, устанавливаем обновления и удаляем больше не
используемые зависимости:

```bash
sudo apt-get update
sudo DEBIAN_FRONTEND=noninteractive apt-get dist-upgrade -y
sudo apt-get autoremove --purge -y
sudo apt-get clean
```

Если система сообщает о необходимости перезагрузки:

```bash
sudo systemctl reboot
```

После возврата сервера проверяем, что обновлений больше нет:

```bash
apt list --upgradable
test -f /var/run/reboot-required && cat /var/run/reboot-required || echo "reboot not required"
```

### 2. Проверка предпосылок

Для server-ноды K3s нужны как минимум 2 CPU и 2 GiB RAM. Проверяем ресурсы,
свободное место и отсутствие swap:

```bash
nproc
free -h
df -hT /
swapon --show
```

Проверяем необходимые kernel-настройки и модули:

```bash
sysctl net.ipv4.ip_forward
sysctl net.bridge.bridge-nf-call-iptables
lsmod | grep -E '^(overlay|br_netfilter)'
```

Ожидаемые значения:

```text
net.ipv4.ip_forward = 1
net.bridge.bridge-nf-call-iptables = 1
```

Проверяем занятые порты и исходящий доступ к установщику:

```bash
sudo ss -lntup
curl -fsS --max-time 10 https://get.k3s.io -o /dev/null && echo "get.k3s.io is reachable"
```

### 3. Защита Kubernetes API

K3s server и встроенный agent на single-node сервере должны обращаться к API
по IP ноды. Поэтому API слушает порт `6443` на сервере, но внешний доступ к
нему блокируется правилом nftables. Доступ с ноутбука выполняется через SSH.

Создаем правило, которое блокирует подключения к API и встроенному Traefik
через публичный интерфейс `enp3s0`. Порты `80/443` откроем позже, когда
настроим HTTPS и решим, какие приложения должны быть публичными:

```bash
sudo install -d -m 0755 /etc/nftables.d

sudo tee /etc/nftables.d/k3s-api-guard.nft >/dev/null <<'EOF'
table inet k3s_api_guard {
  chain input {
    type filter hook input priority -10; policy accept;
    iifname "enp3s0" tcp dport { 80, 443, 6443 } drop
  }
}
EOF
```

Создаем systemd-сервис, который применяет правило до запуска K3s:

```bash
sudo tee /etc/systemd/system/k3s-api-guard.service >/dev/null <<'EOF'
[Unit]
Description=Block public access to K3s API
Before=k3s.service
After=network-pre.target
Wants=network-pre.target

[Service]
Type=oneshot
RemainAfterExit=yes
ExecStartPre=-/usr/sbin/nft delete table inet k3s_api_guard
ExecStart=/usr/sbin/nft -f /etc/nftables.d/k3s-api-guard.nft
ExecStop=-/usr/sbin/nft delete table inet k3s_api_guard

[Install]
WantedBy=multi-user.target
EOF

sudo systemctl daemon-reload
sudo systemctl enable --now k3s-api-guard.service
```

Проверяем примененное правило:

```bash
sudo nft list table inet k3s_api_guard
systemctl status k3s-api-guard.service --no-pager
```

### 4. Установка K3s

Устанавливаем стабильную версию K3s. `--tls-san 127.0.0.1` позволяет
использовать API через локальный SSH-туннель. Kubeconfig доступен только root:

```bash
curl -sfL https://get.k3s.io | \
  sudo INSTALL_K3S_EXEC="server --tls-san 127.0.0.1 --write-kubeconfig-mode 600" sh -
```

Установщик:

- загружает бинарный файл `k3s`;
- создает systemd-сервис `k3s.service`;
- включает автозапуск K3s;
- устанавливает ссылки `kubectl` и `crictl`;
- запускает single-node control plane и worker.

Проверяем сервис и версию:

```bash
systemctl status k3s --no-pager
sudo k3s --version
```

### 5. Проверка кластера на сервере

Проверяем состояние ноды:

```bash
sudo k3s kubectl get nodes -o wide
```

Ожидаем состояние `Ready`.

Проверяем системные Pods:

```bash
sudo k3s kubectl get pods -A -o wide
```

Основные системные компоненты должны быть в состояниях `Running` или
`Completed`:

- CoreDNS;
- metrics-server;
- local-path-provisioner;
- Traefik;
- ServiceLB для Traefik.

Проверяем сервисы и доступные StorageClass:

```bash
sudo k3s kubectl get svc -A
sudo k3s kubectl get storageclass
```

Проверяем потребление ресурсов:

```bash
sudo k3s kubectl top nodes
sudo k3s kubectl top pods -A
free -h
df -hT /
```

### 6. Безопасный доступ с MacBook

#### Установка kubectl

Устанавливаем [Homebrew](https://brew.sh/), если его еще нет:

```bash
/bin/bash -c "$(curl -fsSL https://raw.githubusercontent.com/Homebrew/install/HEAD/install.sh)"
```

Устанавливаем самостоятельный `kubectl`:

```bash
brew install kubectl
```

Проверяем установку:

```bash
kubectl version --client
```

#### Получение kubeconfig

Создаем каталог и копируем kubeconfig с VPS:

```bash
mkdir -p ~/.kube
chmod 700 ~/.kube

ssh k3s-vps 'sudo cat /etc/rancher/k3s/k3s.yaml' > ~/.kube/k3s-vps.yaml
chmod 600 ~/.kube/k3s-vps.yaml
```

Проверяем адрес API в полученном файле:

```bash
grep 'server:' ~/.kube/k3s-vps.yaml
```

Ожидаемый адрес:

```text
server: https://127.0.0.1:6443
```

Файл содержит административные сертификаты кластера. Его нельзя добавлять в
Git или передавать другим пользователям.

Если на MacBook еще нет основного kubeconfig, создаем ссылку, чтобы `kubectl`
и GUI-клиенты автоматически находили кластер:

```bash
test -e ~/.kube/config || ln -s k3s-vps.yaml ~/.kube/config
```

Если `~/.kube/config` уже используется для других кластеров, не заменяем его.
Указываем отдельный файл явно:

```bash
export KUBECONFIG=~/.kube/k3s-vps.yaml
```

#### Управляемый SSH-туннель

Запускаем SSH-туннель в фоне. Control socket позволяет проверить и корректно
остановить именно этот туннель:

```bash
ssh -M -S ~/.ssh/k3s-vps-tunnel.sock \
  -fN \
  -o ExitOnForwardFailure=yes \
  -o ServerAliveInterval=30 \
  -o ServerAliveCountMax=3 \
  -L 127.0.0.1:6443:127.0.0.1:6443 \
  k3s-vps
```

Проверяем туннель и доступ к кластеру:

```bash
ssh -S ~/.ssh/k3s-vps-tunnel.sock -O check k3s-vps
kubectl get nodes -o wide
kubectl get pods -A
```

Порт `6443` остается закрытым от публичного интернета. Локальный
`127.0.0.1:6443` передается через SSH к Kubernetes API на VPS.

Останавливаем туннель:

```bash
ssh -S ~/.ssh/k3s-vps-tunnel.sock -O exit k3s-vps
```

После перезагрузки или разрыва соединения туннель нужно запустить снова.

#### Подключение Aptakube

[Aptakube](https://aptakube.com/) — графический Kubernetes-клиент для macOS,
который использует локальные kubeconfig-файлы и не требует установки
дополнительных компонентов в кластер.

Скачиваем и устанавливаем Aptakube с официального сайта. Перед запуском
проверяем SSH-туннель и доступ через `kubectl`:

```bash
ssh -S ~/.ssh/k3s-vps-tunnel.sock -O check k3s-vps
kubectl get nodes
```

Запускаем Aptakube:

```bash
open -a Aptakube
```

Если `~/.kube/config` ссылается на kubeconfig K3s, Aptakube автоматически
обнаружит кластер. Иначе добавляем файл `~/.kube/k3s-vps.yaml` вручную через
интерфейс приложения.

Aptakube использует адрес `https://127.0.0.1:6443` из kubeconfig. Без
работающего SSH-туннеля кластер будет отображаться как недоступный.

### 7. Тестовое приложение

Создаем отдельный namespace для учебных ресурсов:

```bash
kubectl create namespace k3s-lab
```

Создаем Deployment с двумя экземплярами nginx:

```bash
kubectl create deployment hello-k3s \
  --namespace k3s-lab \
  --image=nginx:1.27-alpine \
  --replicas=2
```

Deployment декларативно поддерживает необходимое количество Pods. Если один
Pod завершится, Kubernetes создаст замену.

Публикуем Deployment только внутри кластера через Service типа `ClusterIP`:

```bash
kubectl expose deployment hello-k3s \
  --namespace k3s-lab \
  --name hello-k3s \
  --port 80 \
  --target-port 80 \
  --type ClusterIP
```

Ждем завершения развертывания и проверяем созданные ресурсы:

```bash
kubectl rollout status deployment/hello-k3s --namespace k3s-lab
kubectl get all --namespace k3s-lab -o wide
```

Для безопасной проверки приложения открываем временный port-forward с
ноутбука:

```bash
kubectl port-forward --namespace k3s-lab service/hello-k3s 8080:80
```

Пока команда работает, приложение доступно только локально:

```bash
curl http://127.0.0.1:8080
```

Проверяем Service запросом из временного Pod внутри кластера:

```bash
kubectl run curl-check \
  --namespace k3s-lab \
  --image=curlimages/curl:8.7.1 \
  --restart=Never \
  --rm -i --quiet \
  -- curl -fsS http://hello-k3s
```

Проверяем self-healing. Сначала смотрим список Pods:

```bash
kubectl get pods --namespace k3s-lab -o wide
```

Удаляем один Pod:

```bash
kubectl delete pod --namespace k3s-lab <pod-name>
```

Deployment автоматически создает новый Pod, чтобы снова получить две
реплики:

```bash
kubectl rollout status deployment/hello-k3s --namespace k3s-lab
kubectl get pods --namespace k3s-lab -o wide
```

Удалить все ресурсы учебного namespace можно одной командой:

```bash
kubectl delete namespace k3s-lab
```

### 8. Полезная диагностика

Статус K3s и последние сообщения сервиса:

```bash
systemctl status k3s --no-pager
sudo journalctl -u k3s --since "10 minutes ago" --no-pager
```

События Kubernetes в порядке возникновения:

```bash
sudo k3s kubectl get events -A --sort-by=.lastTimestamp
```

Логи конкретного Pod:

```bash
sudo k3s kubectl logs -n <namespace> <pod-name>
```

Описание ресурса с событиями и причиной ошибки:

```bash
sudo k3s kubectl describe pod -n <namespace> <pod-name>
```

### 9. Проверка текущего состояния

Этот короткий чек-лист удобно выполнять в начале каждой учебной сессии.

Проверяем VPS, сервис K3s и параметры его запуска:

```bash
ssh k3s-vps 'uptime; free -h; df -h /; swapon --show'
ssh k3s-vps 'systemctl is-enabled k3s; systemctl is-active k3s'
ssh k3s-vps 'sudo systemctl show k3s -p ExecStart --value'
```

Проверяем основные ресурсы кластера:

```bash
ssh k3s-vps 'sudo k3s kubectl get nodes -o wide'
ssh k3s-vps 'sudo k3s kubectl get pods -A -o wide'
ssh k3s-vps 'sudo k3s kubectl get svc,storageclass,ingressclass -A'
ssh k3s-vps 'sudo k3s kubectl top nodes'
```

Проверяем предупреждения:

```bash
ssh k3s-vps \
  'sudo k3s kubectl get events -A --field-selector type=Warning --sort-by=.lastTimestamp'
ssh k3s-vps \
  'sudo journalctl -u k3s --since "24 hours ago" -p warning --no-pager'
```

Проверяем host firewall:

```bash
ssh k3s-vps 'systemctl is-active k3s-api-guard.service'
ssh k3s-vps 'sudo nft list table inet k3s_api_guard'
```

На ноутбуке проверяем, что публичные порты закрыты. Таймаут ожидаем, если
firewall отбрасывает пакеты:

```bash
VPS_IP=<PUBLIC_VPS_IP>

for port in 80 443 6443; do
  curl -k -sS -o /dev/null \
    --connect-timeout 3 \
    --max-time 4 \
    -w "port=${port} http_code=%{http_code}\n" \
    "https://${VPS_IP}:${port}" || true
done
```

### 10. Учебный стенд Kafka, PostgreSQL, ActiveMQ и интеграционного сервиса

Манифест [`manifests/mq-kafka-lab.yaml`](manifests/mq-kafka-lab.yaml)
разворачивает в отдельном namespace:

- single-node Kafka в KRaft-режиме;
- PostgreSQL;
- Apache ActiveMQ Artemis как легкий JMS-брокер;
- интеграционный сервис из образа
  `opetrov2019/kafka-mq-integration-service:integration-latest`;
- ClusterIP Services и persistent volumes для Kafka, PostgreSQL и ActiveMQ.

IBM MQ, Kafka Connect и tracing collector в этот учебный стенд не входят.
Для JMS используется ActiveMQ Artemis, потому что он заметно легче IBM MQ и
достаточен для проверки `JmsTemplate`.

Статическая конфигурация Kafka хранится в ConfigMap `kafka-config` как файл
`server.properties`. В переменных окружения контейнера остается только
окруженческая JVM-настройка `KAFKA_HEAP_OPTS`.

Init container выполняет идемпотентное форматирование KRaft storage перед
запуском broker-а. При изменении `server.properties` увеличиваем значение
аннотации `mq-kafka-lab/config-revision` в шаблоне Kafka Pod, чтобы StatefulSet
перезапустил Pod с новой конфигурацией.

Создаем namespace:

```bash
kubectl create namespace mq-kafka-lab
```

Разворачиваем стенд:

```bash
kubectl apply -f manifests/mq-kafka-lab.yaml
```

Проверяем rollout и ресурсы:

```bash
kubectl rollout status statefulset/kafka --namespace mq-kafka-lab
kubectl rollout status statefulset/activemq --namespace mq-kafka-lab
kubectl rollout status statefulset/postgres --namespace mq-kafka-lab
kubectl rollout status deployment/mq-kafka-integration-service \
  --namespace mq-kafka-lab

kubectl get all,pvc --namespace mq-kafka-lab
kubectl top pods --namespace mq-kafka-lab
```

Проверяем health endpoint из временного Pod:

```bash
kubectl run health-check \
  --namespace mq-kafka-lab \
  --image=curlimages/curl:8.7.1 \
  --restart=Never \
  --rm -i --quiet \
  -- curl -fsS http://mq-kafka-integration-service:8080/readyz
```

Проверяем Kafka, ActiveMQ и таблицы PostgreSQL:

```bash
kubectl exec --namespace mq-kafka-lab kafka-0 -- \
  kafka-topics --bootstrap-server kafka:9092 --list

kubectl exec --namespace mq-kafka-lab activemq-0 -- \
  sh -c 'nc -zv 127.0.0.1 61616'

kubectl exec --namespace mq-kafka-lab postgres-0 -- \
  psql -U mq_kafka -d mq_kafka -c '\dt'
```

После базовой проверки можно выполнить end-to-end smoke-тест маршрута
`Kafka -> ActiveMQ` через integration service. Подробная инструкция для
тестировщика находится в
[`docs/k3s-kafka-to-mq-smoke-test.md`](docs/k3s-kafka-to-mq-smoke-test.md).

Манифест содержит учебные пароли PostgreSQL и ActiveMQ. Перед использованием
вне изолированного стенда замените их и храните секреты во внешнем secret
manager.

Удаление namespace удалит Pods, Services и связанные local-path volumes со
всеми данными стенда:

```bash
kubectl delete namespace mq-kafka-lab
```

### 11. Экспорт логов в Elasticsearch через Vector

Приложения пишут логи в `stdout` контейнеров. Манифест
[`manifests/vector-elasticsearch.yaml`](manifests/vector-elasticsearch.yaml)
разворачивает Vector как DaemonSet:

```text
container stdout → Vector → внешний Elasticsearch → Kibana
```

Vector:

- читает логи всех Pods на ноде;
- добавляет Kubernetes metadata;
- разбирает поле `message` как JSON, когда это возможно;
- сохраняет исходные не-JSON строки без потери;
- отправляет события в ежедневные индексы `k3s-logs-YYYY.MM.DD`.

Для экономии ресурсов манифест задает `requests`/`limits` для контейнера
Vector, а также `ResourceQuota` и `LimitRange` для namespace `observability`.

Elasticsearch и Kibana рекомендуется размещать вне этого VPS: вместе с Kafka
они потребуют слишком много памяти.

Создаем namespace и Secret с параметрами внешнего Elasticsearch. Secret нельзя
добавлять в Git:

```bash
kubectl create namespace observability

kubectl create secret generic vector-elasticsearch \
  --namespace observability \
  --from-literal=ELASTICSEARCH_ENDPOINT='https://<ELASTICSEARCH_HOST>:9200' \
  --from-literal=ELASTICSEARCH_USERNAME='<ELASTICSEARCH_USERNAME>' \
  --from-literal=ELASTICSEARCH_PASSWORD='<ELASTICSEARCH_PASSWORD>'
```

Перед развертыванием проверяем доступ к Elasticsearch из временного Pod:

```bash
kubectl run elasticsearch-check \
  --namespace observability \
  --image=curlimages/curl:8.7.1 \
  --restart=Never \
  --rm -i --quiet \
  -- curl -fsS -u '<ELASTICSEARCH_USERNAME>:<ELASTICSEARCH_PASSWORD>' \
  'https://<ELASTICSEARCH_HOST>:9200'
```

Разворачиваем Vector:

```bash
kubectl apply -f manifests/vector-elasticsearch.yaml
kubectl rollout status daemonset/vector --namespace observability
```

Проверяем Vector:

```bash
kubectl get pods --namespace observability
kubectl logs daemonset/vector --namespace observability --tail=100
kubectl top pods --namespace observability
```

Проверяем появление индексов:

```bash
curl -fsS -u '<ELASTICSEARCH_USERNAME>:<ELASTICSEARCH_PASSWORD>' \
  'https://<ELASTICSEARCH_HOST>:9200/_cat/indices/k3s-logs-*?v'
```

TLS-проверка сертификата включена. Если Elasticsearch использует собственный
CA, добавьте CA-файл в Secret или ConfigMap и настройте `ca_file` в Vector
вместо отключения проверки сертификата.

### Ошибка, обнаруженная во время установки

Первая установка использовала параметр:

```text
--bind-address 127.0.0.1
```

Это не подошло даже для single-node K3s: встроенный agent обращался к API по
IP ноды, получал `connection refused`, а системные Pods не могли обращаться к
сервису `kubernetes`.

Правильная схема для этого VPS:

- не ограничивать K3s API параметром `--bind-address`;
- блокировать публичный доступ к `6443` на уровне host firewall;
- использовать SSH-туннель для административного доступа.

## Полезные ссылки

- [K3s Documentation](https://docs.k3s.io/)
- [K3s Requirements](https://docs.k3s.io/installation/requirements)
- [Kubernetes Documentation](https://kubernetes.io/docs/home/)
- [kubectl Cheat Sheet](https://kubernetes.io/docs/reference/kubectl/quick-reference/)

## Лицензия

Материалы опубликованы под лицензией [MIT](LICENSE).
