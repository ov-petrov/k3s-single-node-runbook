"""Use official Xray for VLESS while sing-box keeps the host TUN.

Each provider profile gets a stable localhost SOCKS port for isolated probes.
The optional shared SOCKS port uses Xray's health checks and load balancer.
"""
from __future__ import annotations

import copy
import hashlib
import json
import subprocess
import tempfile
from contextlib import contextmanager
from pathlib import Path
from typing import Any, Iterator

from import_outbounds_catalog import atomic_write_text

PORT_FIRST = 20000
PORT_COUNT = 10000
BALANCER_TAG = 'provider-balancer'


class XrayTransportError(ValueError):
    pass


def load_pool(path: Path) -> dict[str, Any] | None:
    if not path.exists():
        return None
    data = json.loads(path.read_text(encoding='utf-8'))
    if not isinstance(data, dict):
        raise XrayTransportError('Xray pool is not a JSON object')
    return data


def _proxy(document: dict[str, Any]) -> dict[str, Any]:
    for outbound in document.get('outbounds', []):
        if outbound.get('tag') == 'proxy-out':
            return outbound
    raise XrayTransportError('candidate has no proxy-out outbound')


def _xray_outbound(name: str, source: dict[str, Any], interface: str) -> dict[str, Any]:
    tls = source.get('tls', {})
    reality = tls.get('reality', {})
    if source.get('type') != 'vless' or not tls.get('enabled') or not reality.get('enabled'):
        raise XrayTransportError('Xray transport currently requires VLESS with Reality')
    if tls.get('insecure'):
        raise XrayTransportError('insecure TLS is not supported by the Xray adapter')
    if source.get('packet_encoding') not in (None, '', 'xudp'):
        raise XrayTransportError('unsupported VLESS packet encoding')
    user: dict[str, Any] = {'id': source['uuid'], 'encryption': 'none'}
    if source.get('flow'):
        user['flow'] = source['flow']
    stream: dict[str, Any] = {
        'network': 'tcp',
        'security': 'reality',
        'sockopt': {'interface': interface},
        'realitySettings': {
            'serverName': tls.get('server_name', source['server']),
            'fingerprint': tls.get('utls', {}).get('fingerprint') or 'chrome',
            'publicKey': reality['public_key'],
            'shortId': reality.get('short_id', ''),
        },
    }
    transport = source.get('transport', {})
    network = transport.get('type', 'tcp')
    if network == 'grpc':
        stream['network'] = 'grpc'
        stream['grpcSettings'] = {'serviceName': transport.get('service_name', '')}
    elif network == 'ws':
        stream['network'] = 'ws'
        stream['wsSettings'] = {
            'path': transport.get('path', '/'),
            'headers': copy.deepcopy(transport.get('headers', {})),
        }
    elif network != 'tcp':
        raise XrayTransportError(f'unsupported Xray transport: {network}')
    return {
        'tag': name,
        'protocol': 'vless',
        'settings': {'vnext': [{
            'address': source['server'], 'port': source['server_port'], 'users': [user],
        }]},
        'streamSettings': stream,
    }


def _channels(pool: dict[str, Any] | None) -> dict[str, tuple[int, dict[str, Any]]]:
    if pool is None:
        return {}
    outbounds = {o['tag']: o for o in pool.get('outbounds', [])}
    rules = {
        tag: r.get('outboundTag')
        for r in pool.get('routing', {}).get('rules', [])
        for tag in r.get('inboundTag', [])
    }
    channels = {}
    for inbound in pool.get('inbounds', []):
        name = rules.get(inbound.get('tag'))
        if inbound.get('listen') == '127.0.0.1' and name in outbounds:
            channels[name] = (int(inbound['port']), copy.deepcopy(outbounds[name]))
    return channels


def _preferred_port(name: str) -> int:
    return PORT_FIRST + int(hashlib.sha256(name.encode()).hexdigest()[:8], 16) % PORT_COUNT


def _allocate_ports(names: set[str], existing: dict[str, tuple[int, dict[str, Any]]]) -> dict[str, int]:
    ports = {name: existing[name][0] for name in sorted(names) if name in existing}
    used = set(ports.values())
    if len(used) != len(ports):
        raise XrayTransportError('existing Xray pool has duplicate SOCKS ports')
    for name in sorted(names - ports.keys()):
        start = _preferred_port(name) - PORT_FIRST
        for step in range(PORT_COUNT):
            port = PORT_FIRST + (start + step) % PORT_COUNT
            if port not in used:
                ports[name] = port
                used.add(port)
                break
        else:
            raise XrayTransportError('no free SOCKS ports in Xray pool range')
    return ports


def build_pool(
    documents: dict[str, dict[str, Any]],
    previous: dict[str, Any] | None,
    active: dict[str, Any],
    interface: str,
    *,
    balancer_port: int | None = None,
) -> tuple[dict[str, dict[str, Any]], dict[str, Any]]:
    if not interface:
        raise XrayTransportError('runtime.xray_outbound_interface is required')
    if balancer_port is not None and (
        type(balancer_port) is not int or not 1024 <= balancer_port <= 65535
        or PORT_FIRST <= balancer_port < PORT_FIRST + PORT_COUNT
    ):
        raise XrayTransportError('runtime.xray_balancer_port must be outside the candidate port range 20000–29999')
    existing = _channels(previous)
    outbounds = {name: _xray_outbound(name, _proxy(doc), interface) for name, doc in documents.items()}
    live = _proxy(active)
    if live.get('type') == 'socks':
        if live.get('server') != '127.0.0.1':
            raise XrayTransportError('active SOCKS proxy is not the local Xray pool')
        if balancer_port is None or live.get('server_port') != balancer_port:
            retained = [name for name, (port, _) in existing.items() if port == live.get('server_port')]
            if len(retained) != 1:
                raise XrayTransportError('active SOCKS channel is absent from the previous Xray pool')
            name = retained[0]
            outbounds.setdefault(name, existing[name][1])
    ports = _allocate_ports(set(outbounds), existing)
    converted = copy.deepcopy(documents)
    for name, document in converted.items():
        proxy = _proxy(document)
        proxy.clear()
        proxy.update({
            'type': 'socks', 'tag': 'proxy-out', 'server': '127.0.0.1',
            'server_port': ports[name], 'version': '5', 'bind_interface': 'lo',
        })
    names = sorted(outbounds)
    pool = {
        'log': {'loglevel': 'warning'},
        'inbounds': [{
            'tag': f'socks-{name}', 'listen': '127.0.0.1', 'port': ports[name],
            'protocol': 'socks', 'settings': {'auth': 'noauth', 'udp': True},
        } for name in names],
        'outbounds': [outbounds[name] for name in names],
        'routing': {'rules': [{
            'type': 'field', 'inboundTag': [f'socks-{name}'], 'outboundTag': name,
        } for name in names]},
    }
    if balancer_port is not None:
        pool['inbounds'].append({
            'tag': BALANCER_TAG, 'listen': '127.0.0.1', 'port': balancer_port,
            'protocol': 'socks', 'settings': {'auth': 'noauth', 'udp': True},
        })
        pool['routing']['rules'].append({
            'type': 'field', 'inboundTag': [BALANCER_TAG], 'balancerTag': BALANCER_TAG,
        })
        pool['routing']['balancers'] = [{
            'tag': BALANCER_TAG,
            'selector': names,
            'strategy': {'type': 'leastPing'},
            'fallbackTag': next(iter(documents)),
        }]
        pool['observatory'] = {
            'subjectSelector': names,
            'probeUrl': 'https://www.gstatic.com/generate_204',
            'probeInterval': '30s',
            'enableConcurrency': True,
        }
    return converted, pool


def _service(runtime: dict[str, Any], action: str) -> None:
    result = subprocess.run(
        ['systemctl', action, str(runtime.get('xray_service_name', 'xray-provider-pool.service'))],
        capture_output=True, text=True, timeout=30,
    )
    if result.returncode:
        raise XrayTransportError(f'Xray service {action} failed; inspect its journal')


@contextmanager
def activate_pool(pool: dict[str, Any], runtime: dict[str, Any]) -> Iterator[None]:
    path = Path(runtime['xray_config_path'])
    path.parent.mkdir(parents=True, exist_ok=True)
    payload = json.dumps(pool, ensure_ascii=False, indent=2) + '\n'
    previous = path.read_text(encoding='utf-8') if path.exists() else None
    with tempfile.TemporaryDirectory(prefix='.xray-check-', dir=path.parent) as directory:
        staged = Path(directory) / 'pool.json'
        atomic_write_text(staged, payload)
        result = subprocess.run(
            [str(runtime.get('xray_binary', '/usr/local/bin/xray')), 'run', '-test', '-config', str(staged)],
            capture_output=True, text=True, timeout=20,
        )
        if result.returncode:
            raise XrayTransportError('Xray pool validation failed; current pool preserved')
    if previous == payload:
        yield
        return
    try:
        atomic_write_text(path, payload)
        _service(runtime, 'restart')
        yield
    except BaseException:
        if previous is None:
            path.unlink(missing_ok=True)
            _service(runtime, 'stop')
        else:
            atomic_write_text(path, previous)
            _service(runtime, 'restart')
        raise
