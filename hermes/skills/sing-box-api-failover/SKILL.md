---
name: sing-box-api-failover
description: Operate the VPS TUN in sing-box, the Xray provider balancer, and subscription refresh checks.
---

# VPS proxy and subscription refresh

Sing-box handles the system TUN, DNS, and direct routing rules. It passes
proxied traffic to Xray at 127.0.0.1:10808. Xray checks provider profiles
with its observatory and chooses an outbound for new connections.

Inspect the live services and refresh schedule.

~~~sh
systemctl status sing-box-tun.service xray-provider-pool.service --no-pager
systemctl list-timers singbox-pool-refresh.timer --all --no-pager
journalctl -u singbox-pool-refresh.service -n 30 --no-pager
~~~

The refresh service downloads the subscription and checks Telegram getMe
and Codex transport through candidate SOCKS ports. To refresh on demand,
run `sudo systemctl start singbox-pool-refresh.service`. Refresh may restart
Xray when the provider pool changes.

Use a separate short Hermes request as the OAuth owner for application
validation. Keep subscription URLs, access parameters, tokens and logs
private. Before changing TUN routing, schedule a rollback and verify access
from a fresh SSH session.
