# Single-node K3s on Ubuntu VPS

Практический runbook для установки учебного single-node Kubernetes-кластера
на базе K3s с безопасным доступом через SSH-туннель.

Инструкция рассчитана на новый VPS с Ubuntu 24.04 и содержит воспроизводимые
команды, пояснения, диагностику и проверку self-healing.

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

На MacBook создаем отдельный kubeconfig. Файл содержит административные
сертификаты, поэтому его нельзя добавлять в Git:

```bash
mkdir -p ~/.kube
ssh k3s-vps 'sudo cat /etc/rancher/k3s/k3s.yaml' > ~/.kube/k3s-vps.yaml
chmod 600 ~/.kube/k3s-vps.yaml
```

В отдельном терминале открываем SSH-туннель:

```bash
ssh -N -L 6443:127.0.0.1:6443 k3s-vps
```

Пока туннель работает, используем кластер с ноутбука:

```bash
KUBECONFIG=~/.kube/k3s-vps.yaml kubectl get nodes
KUBECONFIG=~/.kube/k3s-vps.yaml kubectl get pods -A
```

Для удобства можно установить переменную только в текущей shell-сессии:

```bash
export KUBECONFIG=~/.kube/k3s-vps.yaml
kubectl get nodes
```

Порты `80`, `443` и `6443` пока не должны открываться в публичный интернет.
SSH-туннель передает запросы через защищенное SSH-соединение и подключается к
API локально на VPS.

Остановить созданный SSH-туннель можно командой:

```bash
pkill -f 'ssh -fN -o ExitOnForwardFailure=yes -L 6443:127.0.0.1:6443 k3s-vps'
```

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
