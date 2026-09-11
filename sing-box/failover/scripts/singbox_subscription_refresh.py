#!/usr/bin/env python3
"""Fetch a private VLESS subscription and safely refresh the candidate pool.

The subscription URL is read from a root-only file, never from arguments,
environment variables, or logs.  A failed download or parse leaves the current
inventory and pool untouched.
"""

from __future__ import annotations

import argparse
import base64
import fcntl
import hashlib
import json
import os
import re
import shlex
import subprocess
import sys
import tempfile
import uuid
from contextlib import contextmanager
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Iterator
from urllib import error, parse, request

from import_outbounds_catalog import (
    CatalogError,
    atomic_write_text,
    build_candidates,
    build_inventory_yaml,
    outbound_identity,
    write_candidates,
)


MAX_SUBSCRIPTION_BYTES = 4 * 1024 * 1024
UTC = timezone.utc


class SubscriptionError(ValueError):
    """Raised when a provider response cannot be safely used."""


def utc_now() -> str:
    return datetime.now(UTC).isoformat()


def log(event: str, **fields: Any) -> None:
    print(json.dumps({'ts': utc_now(), 'event': event, **fields}, ensure_ascii=False), flush=True)


def load_yaml(path: Path) -> dict[str, Any]:
    try:
        import yaml  # type: ignore
    except ImportError as exc:
        raise SubscriptionError('PyYAML is required to read inventory') from exc
    document = yaml.safe_load(path.read_text(encoding='utf-8'))
    if not isinstance(document, dict):
        raise SubscriptionError('inventory is not a YAML object')
    if not isinstance(document.get('runtime'), dict):
        raise SubscriptionError('inventory has no runtime section')
    if not isinstance(document.get('configs'), list):
        raise SubscriptionError('inventory has no configs array')
    return document


def read_subscription_url(path: Path) -> str:
    try:
        lines = [
            line.strip()
            for line in path.read_text(encoding='utf-8').splitlines()
            if line.strip() and not line.lstrip().startswith('#')
        ]
    except OSError as exc:
        raise SubscriptionError(f'cannot read subscription URL file: {exc}') from exc
    if len(lines) != 1:
        raise SubscriptionError('subscription URL file must contain exactly one non-comment line')
    value = lines[0]
    if value.startswith('SUBSCRIPTION_URL='):
        value = value.split('=', 1)[1].strip()
    parts = parse.urlsplit(value)
    if parts.scheme != 'https' or not parts.netloc:
        raise SubscriptionError('subscription URL must use HTTPS and include a host')
    return value


def load_metadata(path: Path) -> dict[str, str]:
    if not path.exists():
        return {}
    try:
        data = json.loads(path.read_text(encoding='utf-8'))
    except (OSError, json.JSONDecodeError) as exc:
        raise SubscriptionError(f'cannot read subscription metadata: {exc}') from exc
    if not isinstance(data, dict):
        raise SubscriptionError('subscription metadata is not an object')
    return {key: value for key, value in data.items() if isinstance(key, str) and isinstance(value, str)}


def download_subscription(url: str, metadata: dict[str, str], timeout_seconds: int, max_bytes: int) -> tuple[bytes | None, dict[str, str]]:
    headers = {
        'Accept': 'text/plain, application/json;q=0.9, */*;q=0.1',
        'Accept-Encoding': 'identity',
        'User-Agent': 'singbox-subscription-refresh/1.0',
    }
    if metadata.get('etag'):
        headers['If-None-Match'] = metadata['etag']
    if metadata.get('last_modified'):
        headers['If-Modified-Since'] = metadata['last_modified']
    response_request = request.Request(url, headers=headers)
    try:
        with request.urlopen(response_request, timeout=timeout_seconds) as response:
            content_length = response.headers.get('Content-Length')
            if content_length and int(content_length) > max_bytes:
                raise SubscriptionError(f'subscription response exceeds {max_bytes} bytes')
            body = response.read(max_bytes + 1)
            if len(body) > max_bytes:
                raise SubscriptionError(f'subscription response exceeds {max_bytes} bytes')
            return body, {
                'etag': response.headers.get('ETag', ''),
                'last_modified': response.headers.get('Last-Modified', ''),
            }
    except error.HTTPError as exc:
        if exc.code == 304:
            return None, metadata
        raise SubscriptionError(f'subscription request returned HTTP {exc.code}') from exc
    except (OSError, ValueError) as exc:
        raise SubscriptionError(f'subscription request failed: {exc}') from exc


def query_value(values: dict[str, str], key: str) -> str:
    return values.get(key, '').strip()


def parse_vless_uri(uri: str, position: int) -> dict[str, Any]:
    try:
        parts = parse.urlsplit(uri)
    except ValueError as exc:
        raise SubscriptionError(f'VLESS entry {position} has an invalid URL') from exc
    if parts.scheme.lower() != 'vless':
        raise SubscriptionError(f'entry {position} is not VLESS')
    if not parts.username or not parts.hostname:
        raise SubscriptionError(f'VLESS entry {position} has no UUID or server')
    try:
        client_uuid = str(uuid.UUID(parse.unquote(parts.username)))
        server_port = parts.port
    except (ValueError, AttributeError) as exc:
        raise SubscriptionError(f'VLESS entry {position} has an invalid UUID or port') from exc
    if server_port is None or not 1 <= server_port <= 65535:
        raise SubscriptionError(f'VLESS entry {position} has no valid port')

    values = {key: value for key, value in parse.parse_qsl(parts.query, keep_blank_values=True)}
    security = query_value(values, 'security').lower() or 'none'
    if security not in {'none', 'tls', 'reality'}:
        raise SubscriptionError(f'VLESS entry {position} uses unsupported security {security!r}')

    outbound: dict[str, Any] = {
        'type': 'vless',
        'tag': parse.unquote(parts.fragment).strip()[:128] or f'subscription-{position}',
        'server': parts.hostname,
        'server_port': server_port,
        'uuid': client_uuid,
    }
    flow = query_value(values, 'flow')
    if flow:
        outbound['flow'] = flow
    packet_encoding = query_value(values, 'packetEncoding') or query_value(values, 'packet_encoding')
    if packet_encoding:
        outbound['packet_encoding'] = packet_encoding

    if security != 'none':
        tls: dict[str, Any] = {'enabled': True}
        server_name = query_value(values, 'sni')
        if server_name:
            tls['server_name'] = server_name
        alpn = [item.strip() for item in query_value(values, 'alpn').split(',') if item.strip()]
        if alpn:
            tls['alpn'] = alpn
        fingerprint = query_value(values, 'fp')
        if fingerprint:
            tls['utls'] = {'enabled': True, 'fingerprint': fingerprint}
        if security == 'reality':
            public_key = query_value(values, 'pbk')
            if not public_key:
                raise SubscriptionError(f'REALITY entry {position} has no public key')
            tls.setdefault('utls', {'enabled': True, 'fingerprint': 'chrome'})
            reality: dict[str, Any] = {'enabled': True, 'public_key': public_key}
            short_id = query_value(values, 'sid')
            if short_id:
                reality['short_id'] = short_id
            tls['reality'] = reality
        outbound['tls'] = tls

    transport_type = (query_value(values, 'type') or 'tcp').lower()
    if transport_type in {'tcp', 'raw'}:
        return outbound
    if transport_type == 'grpc':
        service_name = query_value(values, 'serviceName') or query_value(values, 'service_name')
        outbound['transport'] = {'type': 'grpc', 'service_name': service_name}
        return outbound
    if transport_type == 'ws':
        transport: dict[str, Any] = {'type': 'ws', 'path': query_value(values, 'path') or '/'}
        host = query_value(values, 'host')
        if host:
            transport['headers'] = {'Host': host}
        outbound['transport'] = transport
        return outbound
    raise SubscriptionError(f'VLESS entry {position} uses unsupported transport {transport_type!r}')


def parse_vless_lines(text: str) -> tuple[list[dict[str, Any]], int]:
    outbounds: list[dict[str, Any]] = []
    skipped = 0
    for position, raw_line in enumerate(text.splitlines(), start=1):
        line = raw_line.strip()
        if not line or line.startswith(('#', ';')):
            continue
        if not line.lower().startswith('vless://'):
            skipped += 1
            continue
        try:
            outbounds.append(parse_vless_uri(line, position))
        except SubscriptionError:
            skipped += 1
    unique: list[dict[str, Any]] = []
    seen: set[str] = set()
    for outbound in outbounds:
        identity = outbound_identity(outbound)
        if identity not in seen:
            unique.append(outbound)
            seen.add(identity)
    return unique, skipped


def decode_base64_subscription(text: str) -> str:
    compact = ''.join(text.split())
    if not compact or not re.fullmatch(r'[A-Za-z0-9+/=_-]+', compact):
        raise SubscriptionError('subscription is neither JSON nor a VLESS URI list')
    padded = compact + '=' * (-len(compact) % 4)
    try:
        return base64.b64decode(padded.encode('ascii'), altchars=b'-_', validate=True).decode('utf-8')
    except (ValueError, UnicodeDecodeError) as exc:
        raise SubscriptionError('subscription base64 cannot be decoded as UTF-8') from exc


def parse_subscription_payload(body: bytes) -> tuple[dict[str, Any], dict[str, Any]]:
    try:
        text = body.decode('utf-8-sig').strip()
    except UnicodeDecodeError as exc:
        raise SubscriptionError('subscription response is not UTF-8') from exc
    if not text:
        raise SubscriptionError('subscription response is empty')

    if text.startswith('{'):
        try:
            document = json.loads(text)
        except json.JSONDecodeError as exc:
            raise SubscriptionError('subscription JSON is invalid') from exc
        if not isinstance(document, dict) or not isinstance(document.get('outbounds'), list):
            raise SubscriptionError('subscription JSON has no outbounds array')
        outbounds = [outbound for outbound in document['outbounds'] if isinstance(outbound, dict) and outbound.get('type') == 'vless']
        if not outbounds:
            raise SubscriptionError('subscription JSON contains no VLESS outbounds')
        return {'outbounds': outbounds}, {'format': 'sing-box-json', 'skipped': len(document['outbounds']) - len(outbounds)}

    outbounds, skipped = parse_vless_lines(text)
    source_format = 'vless-uri-list'
    if not outbounds:
        decoded = decode_base64_subscription(text)
        outbounds, skipped = parse_vless_lines(decoded)
        source_format = 'base64-vless-uri-list'
    if not outbounds:
        raise SubscriptionError('subscription contains no supported VLESS entries')
    return {'outbounds': outbounds}, {'format': source_format, 'skipped': skipped}


@contextmanager
def exclusive_lock(path: Path) -> Iterator[bool]:
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open('a+', encoding='utf-8') as handle:
        try:
            fcntl.flock(handle.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError:
            yield False
            return
        try:
            yield True
        finally:
            fcntl.flock(handle.fileno(), fcntl.LOCK_UN)


def proxy_outbound(document: dict[str, Any]) -> dict[str, Any]:
    for outbound in document.get('outbounds', []):
        if isinstance(outbound, dict) and outbound.get('tag') == 'proxy-out':
            return outbound
    raise SubscriptionError('active sing-box config has no proxy-out outbound')


def preserve_active_config(
    configs: list[dict[str, Any]],
    documents: dict[str, dict[str, Any]],
    active_config_path: Path,
    generated_dir: Path,
) -> None:
    try:
        active_document = json.loads(active_config_path.read_text(encoding='utf-8'))
    except (OSError, json.JSONDecodeError) as exc:
        raise SubscriptionError(f'cannot read active sing-box config: {exc}') from exc
    active_outbound = proxy_outbound(active_document)
    active_identity = outbound_identity(active_outbound)
    if any(outbound_identity(proxy_outbound(document)) == active_identity for document in documents.values()):
        return
    name = f'active-{active_identity[:16]}'
    documents[name] = active_document
    configs.append({
        'name': name,
        'path': str(generated_dir / f'{name}.json'),
        'priority': 0,
        'enabled': False,
        'tags': ['active-live'],
        'source_outbound_tag': 'active-live',
        'server': active_outbound.get('server'),
        'server_port': active_outbound.get('server_port'),
    })


def check_candidates(configs: list[dict[str, Any]], runtime: dict[str, Any]) -> None:
    command_template = str(runtime.get('singbox_check_command', '/usr/local/bin/sing-box check -c {config_path}'))
    for config in configs:
        if not config.get('enabled', True):
            continue
        command = command_template.format(config_path=shlex.quote(str(config['path'])))
        try:
            result = subprocess.run(command, shell=True, capture_output=True, text=True, timeout=60)
        except subprocess.TimeoutExpired as exc:
            raise SubscriptionError(f'candidate {config["name"]} validation timed out') from exc
        if result.returncode != 0:
            detail = (result.stderr or result.stdout or 'sing-box check failed')[-300:].replace('\n', ' ')
            raise SubscriptionError(f'candidate {config["name"]} validation failed: {detail}')


def run_pool_probe(
    inventory_path: Path,
    runtime: dict[str, Any],
    config_count: int,
    output_path: Path | None = None,
) -> tuple[int, int]:
    script = runtime.get('pool_probe_script')
    if not isinstance(script, str) or not script:
        raise SubscriptionError('runtime.pool_probe_script is not configured')
    configured_output = runtime.get('pool_results_path')
    if output_path is None and (not isinstance(configured_output, str) or not configured_output):
        raise SubscriptionError('runtime.pool_results_path is not configured')
    output = str(output_path) if output_path else configured_output
    timeout_seconds = int(runtime.get('request_timeout_seconds', 10))
    command = [
        sys.executable,
        script,
        '--inventory', str(inventory_path),
        '--openai-mode', 'transport',
        '--timeout', str(timeout_seconds),
        '--out', output,
    ]
    process_timeout = int(runtime.get('pool_probe_timeout_seconds', max(120, config_count * (timeout_seconds * 3 + 25))))
    try:
        result = subprocess.run(command, capture_output=True, text=True, timeout=process_timeout)
    except subprocess.TimeoutExpired as exc:
        raise SubscriptionError('pool probe timed out') from exc
    if result.returncode != 0:
        detail = (result.stderr or result.stdout or 'pool probe failed')[-400:].replace('\n', ' ')
        raise SubscriptionError(f'pool probe failed: {detail}')
    try:
        results = json.loads(Path(output).read_text(encoding='utf-8'))
    except (OSError, json.JSONDecodeError) as exc:
        raise SubscriptionError(f'pool probe produced no readable result: {exc}') from exc
    if not isinstance(results, list):
        raise SubscriptionError('pool probe result is not an array')
    healthy_count = sum(1 for item in results if isinstance(item, dict) and item.get('healthy') is True)
    return config_count, healthy_count


def refresh(args: argparse.Namespace) -> int:
    inventory_path = Path(args.inventory)
    document = load_yaml(inventory_path)
    runtime = document['runtime']
    metadata_path = Path(args.metadata_path)
    with exclusive_lock(Path(args.lock_path)) as acquired:
        if not acquired:
            log('subscription_refresh_skipped', reason='lock_busy')
            return 0

        metadata = load_metadata(metadata_path)
        subscription_url = read_subscription_url(Path(args.subscription_url_file))
        body, remote_metadata = download_subscription(
            subscription_url,
            metadata,
            timeout_seconds=args.timeout,
            max_bytes=args.max_bytes,
        )
        if body is None:
            log('subscription_not_modified')
            if args.refresh_pool:
                checked, healthy = run_pool_probe(inventory_path, runtime, len(document['configs']))
                log('pool_refreshed', checked=checked, healthy=healthy, source_changed=False)
            return 0

        catalog, summary = parse_subscription_payload(body)
        try:
            base = json.loads(Path(args.base_config).read_text(encoding='utf-8'))
            configs, candidate_documents = build_candidates(base, catalog, Path(args.generated_dir))
        except (OSError, json.JSONDecodeError, CatalogError) as exc:
            raise SubscriptionError(f'candidate generation failed: {exc}') from exc

        preserve_active_config(
            configs,
            candidate_documents,
            Path(runtime.get('active_config_path', '/etc/sing-box/config.json')),
            Path(args.generated_dir),
        )
        write_candidates(candidate_documents, Path(args.generated_dir))
        check_candidates(configs, runtime)

        catalog_payload = json.dumps(catalog, ensure_ascii=False, indent=2) + '\n'
        inventory_payload = build_inventory_yaml(configs, runtime)
        content_sha256 = hashlib.sha256(body).hexdigest()
        next_metadata = {
            'etag': remote_metadata.get('etag', ''),
            'last_modified': remote_metadata.get('last_modified', ''),
            'content_sha256': content_sha256,
            'fetched_at': utc_now(),
        }
        if args.refresh_pool:
            configured_output = runtime.get('pool_results_path')
            if not isinstance(configured_output, str) or not configured_output:
                raise SubscriptionError('runtime.pool_results_path is not configured')
            configured_output_path = Path(configured_output)
            configured_output_path.parent.mkdir(parents=True, exist_ok=True)
            with tempfile.TemporaryDirectory(prefix='singbox-subscription-', dir=inventory_path.parent) as temporary_dir:
                temporary_root = Path(temporary_dir)
                temporary_inventory = temporary_root / 'inventory.yaml'
                temporary_pool = temporary_root / 'pool-probe-results.json'
                atomic_write_text(temporary_inventory, inventory_payload)
                checked, healthy = run_pool_probe(temporary_inventory, runtime, len(configs), temporary_pool)
                if healthy == 0:
                    raise SubscriptionError('new subscription has no healthy candidates')
                atomic_write_text(inventory_path, inventory_payload)
                atomic_write_text(Path(args.catalog_path), catalog_payload)
                atomic_write_text(metadata_path, json.dumps(next_metadata, ensure_ascii=False, indent=2) + '\n')
                temporary_pool.chmod(0o600)
                temporary_pool.replace(configured_output_path)
                log(
                    'subscription_published',
                    configs=len(configs),
                    healthy=healthy,
                    source_format=summary['format'],
                    skipped=summary['skipped'],
                )
                log('pool_refreshed', checked=checked, healthy=healthy, source_changed=True)
        else:
            atomic_write_text(inventory_path, inventory_payload)
            atomic_write_text(Path(args.catalog_path), catalog_payload)
            atomic_write_text(metadata_path, json.dumps(next_metadata, ensure_ascii=False, indent=2) + '\n')
            log('subscription_published', configs=len(configs), source_format=summary['format'], skipped=summary['skipped'])
    return 0


def main() -> int:
    parser = argparse.ArgumentParser(description='Fetch a private VLESS subscription and refresh sing-box candidates')
    parser.add_argument('--inventory', required=True)
    parser.add_argument('--base-config', required=True)
    parser.add_argument('--generated-dir', required=True)
    parser.add_argument('--subscription-url-file', required=True)
    parser.add_argument('--metadata-path', required=True)
    parser.add_argument('--catalog-path', required=True)
    parser.add_argument('--lock-path', default='/run/singbox-failover.lock')
    parser.add_argument('--timeout', type=int, default=20)
    parser.add_argument('--max-bytes', type=int, default=MAX_SUBSCRIPTION_BYTES)
    parser.add_argument('--refresh-pool', action='store_true')
    args = parser.parse_args()
    try:
        return refresh(args)
    except (SubscriptionError, OSError, ValueError) as exc:
        print(f'subscription refresh failed: {exc}', file=sys.stderr)
        return 1


if __name__ == '__main__':
    raise SystemExit(main())
