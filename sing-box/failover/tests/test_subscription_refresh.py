from __future__ import annotations

import base64
import json
import sys
import tempfile
import unittest
from argparse import Namespace
from pathlib import Path
from unittest.mock import patch
from urllib import request


SCRIPTS = Path(__file__).resolve().parents[1] / 'scripts'
sys.path.insert(0, str(SCRIPTS))

from import_outbounds_catalog import build_candidates, outbound_identity
from singbox_subscription_refresh import (
    SubscriptionError, SubscriptionRedirectHandler, download_subscription,
    parse_subscription_payload, refresh,
)
from singbox_pool_probe import check_openai_transport
from xray_transport import activate_pool, build_pool


class SubscriptionParserTest(unittest.TestCase):
    def test_parses_base64_vless_reality_grpc(self) -> None:
        uri = (
            'vless://11111111-1111-1111-1111-111111111111@edge.example:443'
            '?security=reality&sni=cdn.example&fp=firefox&pbk=public-key&sid=abcd'
            '&type=grpc&serviceName=grpc-service&flow=xtls-rprx-vision#Moscow'
        )
        payload = base64.b64encode(uri.encode('utf-8'))

        catalog, summary = parse_subscription_payload(payload)

        self.assertEqual(summary, {'format': 'base64-vless-uri-list', 'skipped': 0})
        outbound = catalog['outbounds'][0]
        self.assertEqual(outbound['server'], 'edge.example')
        self.assertEqual(outbound['server_port'], 443)
        self.assertEqual(outbound['flow'], 'xtls-rprx-vision')
        self.assertEqual(outbound['transport'], {'type': 'grpc', 'service_name': 'grpc-service'})
        self.assertEqual(outbound['tls']['server_name'], 'cdn.example')
        self.assertEqual(outbound['tls']['utls']['fingerprint'], 'firefox')
        self.assertEqual(outbound['tls']['reality'], {'enabled': True, 'public_key': 'public-key', 'short_id': 'abcd'})

    def test_keeps_vless_and_skips_other_protocols(self) -> None:
        payload = (
            'trojan://unrelated@server.example:443\n'
            'vless://22222222-2222-2222-2222-222222222222@198.51.100.10:8443'
            '?security=none&type=tcp#direct\n'
        ).encode('utf-8')

        catalog, summary = parse_subscription_payload(payload)

        self.assertEqual(len(catalog['outbounds']), 1)
        self.assertEqual(catalog['outbounds'][0]['server'], '198.51.100.10')
        self.assertEqual(summary, {'format': 'vless-uri-list', 'skipped': 1})

    def test_parses_singbox_profile_list(self) -> None:
        payload = json.dumps([
            {
                'remarks': 'first profile',
                'outbounds': [
                    {'type': 'direct', 'tag': 'direct'},
                    {
                        'type': 'vless',
                        'tag': 'first label',
                        'server': 'edge-one.example',
                        'server_port': 443,
                        'uuid': 'aaaaaaaa-aaaa-aaaa-aaaa-aaaaaaaaaaaa',
                    },
                ],
            },
            {
                'remarks': 'duplicate profile label',
                'outbounds': [
                    {
                        'type': 'vless',
                        'tag': 'renamed label',
                        'server': 'edge-one.example',
                        'server_port': 443,
                        'uuid': 'aaaaaaaa-aaaa-aaaa-aaaa-aaaaaaaaaaaa',
                    },
                    {
                        'type': 'vless',
                        'tag': 'second label',
                        'server': 'edge-two.example',
                        'server_port': 8443,
                        'uuid': 'bbbbbbbb-bbbb-bbbb-bbbb-bbbbbbbbbbbb',
                    },
                ],
            },
        ]).encode('utf-8')

        catalog, summary = parse_subscription_payload(payload)

        self.assertEqual(summary, {'format': 'sing-box-json-profile-list', 'skipped': 2})
        self.assertEqual(len(catalog['outbounds']), 2)
        self.assertEqual(catalog['outbounds'][0]['server'], 'edge-one.example')
        self.assertEqual(catalog['outbounds'][1]['server'], 'edge-two.example')

    def test_rejects_payload_without_supported_entries(self) -> None:
        with self.assertRaises(SubscriptionError):
            parse_subscription_payload(b'trojan://password@example.com:443')

    def test_identity_does_not_depend_on_provider_label(self) -> None:
        first = {
            'type': 'vless',
            'tag': 'Moscow',
            'server': 'edge.example',
            'server_port': 443,
            'uuid': '33333333-3333-3333-3333-333333333333',
        }
        second = {**first, 'tag': 'New label'}

        self.assertEqual(outbound_identity(first), outbound_identity(second))

    def test_candidates_keep_stable_names_and_bypass_all_proxy_endpoints(self) -> None:
        base = {
            'outbounds': [
                {'type': 'vless', 'tag': 'proxy-out', 'server': 'old.example'},
                {'type': 'direct', 'tag': 'direct'},
            ],
            'route': {
                'rules': [
                    {'ip_cidr': ['10.0.0.0/8'], 'outbound': 'direct'},
                ],
            },
        }
        catalog = {
            'outbounds': [
                {
                    'type': 'vless',
                    'tag': 'one',
                    'server': '198.51.100.10',
                    'server_port': 443,
                    'uuid': '44444444-4444-4444-4444-444444444444',
                },
                {
                    'type': 'vless',
                    'tag': 'two',
                    'server': '2001:db8::10',
                    'server_port': 443,
                    'uuid': '55555555-5555-5555-5555-555555555555',
                },
            ],
        }

        with tempfile.TemporaryDirectory() as directory:
            configs, documents = build_candidates(base, catalog, Path(directory))

        self.assertEqual(len(configs), 2)
        self.assertTrue(all(config['name'].startswith('cfg-') for config in configs))
        direct_cidrs = documents[configs[0]['name']]['route']['rules'][0]['ip_cidr']
        self.assertEqual(direct_cidrs, ['10.0.0.0/8', '198.51.100.10/32', '2001:db8::10/128'])


class RefreshSafetyTest(unittest.TestCase):
    def test_redirect_rejects_downgrade_and_other_origins(self) -> None:
        handler = SubscriptionRedirectHandler()
        req = request.Request('https://subscription.example/access?token=private')
        for target in ['http://subscription.example/access', 'https://other.example/access',
                       'https://subscription.example:8443/access', 'https://user@subscription.example/access']:
            with self.subTest(target=target), self.assertRaises(SubscriptionError):
                handler.redirect_request(req, None, 302, 'Found', {}, target)
        redirected = handler.redirect_request(req, None, 302, 'Found', {}, 'https://subscription.example/new')
        self.assertEqual(redirected.full_url, 'https://subscription.example/new')

    def test_download_error_does_not_disclose_subscription_url(self) -> None:
        with patch('singbox_subscription_refresh.request.build_opener') as opener:
            opener.return_value.open.side_effect = OSError('https://subscription.example/?token=private')
            with self.assertRaises(SubscriptionError) as failure:
                download_subscription('https://subscription.example/?token=private', {}, 10, 1024)
        self.assertNotIn('private', str(failure.exception))

    def test_openai_policy_and_server_errors_fail_probe(self) -> None:
        for code in [200, 401, 404, 405, 403, 429, 500, 503, 302, 0]:
            with self.subTest(code=code), patch('singbox_pool_probe.subprocess.run') as run:
                run.return_value.returncode = 0
                run.return_value.stdout = str(code)
                result = check_openai_transport('http://127.0.0.1:2080', 'https://api.example/', 10)
                self.assertEqual(result['ok'], code in {200, 401, 404, 405})
                self.assertIn('--noproxy', run.call_args.args[0])

    def test_unchanged_subscription_with_no_healthy_nodes_fails(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            args = Namespace(inventory='unused', metadata_path='unused', subscription_url_file='unused',
                             lock_path=str(Path(directory) / 'refresh.lock'), timeout=10, max_bytes=1024,
                             refresh_pool=True)
            with patch('singbox_subscription_refresh.load_yaml', return_value={'runtime': {}, 'configs': [{}]}), \
                 patch('singbox_subscription_refresh.load_metadata', return_value={}), \
                 patch('singbox_subscription_refresh.read_subscription_url', return_value='https://subscription.example/'), \
                 patch('singbox_subscription_refresh.download_subscription', return_value=(None, {})), \
                 patch('singbox_subscription_refresh.run_pool_probe', return_value=(1, 0)):
                with self.assertRaisesRegex(SubscriptionError, 'no healthy candidates'):
                    refresh(args)


class XrayPoolTest(unittest.TestCase):
    def test_balanced_tun_keeps_its_port_when_subscription_changes(self) -> None:
        catalog, _ = parse_subscription_payload(
            b'vless://11111111-1111-1111-1111-111111111111@198.51.100.10:443'
            b'?security=reality&sni=cdn.example&fp=firefox&pbk=public-key&sid=abcd'
        )
        outbound = {**catalog['outbounds'][0], 'tag': 'proxy-out'}
        native = {'outbounds': [outbound]}
        initial = {'cfg-original': native}
        _, pool = build_pool(initial, None, native, 'eth0', balancer_port=10808)
        tun = {'outbounds': [{
            'type': 'socks', 'tag': 'proxy-out', 'server': '127.0.0.1', 'server_port': 10808,
        }]}
        replacement = {'outbounds': [{**outbound, 'server': '198.51.100.11'}]}

        configs, refreshed = build_pool(
            {'cfg-new': replacement}, pool, tun, 'eth0', balancer_port=10808,
        )

        self.assertEqual([o['tag'] for o in refreshed['outbounds']], ['cfg-new'])
        self.assertEqual(refreshed['observatory']['subjectSelector'], ['cfg-new'])
        self.assertEqual(refreshed['routing']['balancers'][0]['strategy'], {'type': 'leastPing'})
        self.assertEqual(refreshed['routing']['rules'][-1]['balancerTag'], 'provider-balancer')
        self.assertEqual(refreshed['inbounds'][-1]['port'], 10808)
        self.assertNotEqual(configs['cfg-new']['outbounds'][0]['server_port'], 10808)

    def test_refresh_keeps_the_active_channel_after_provider_removal(self) -> None:
        catalog, _ = parse_subscription_payload(
            b'vless://11111111-1111-1111-1111-111111111111@198.51.100.10:443'
            b'?security=reality&sni=cdn.example&fp=firefox&pbk=public-key&sid=abcd'
        )
        outbound = {**catalog['outbounds'][0], 'tag': 'proxy-out'}
        old = {'outbounds': [outbound]}
        reserve = {'outbounds': [{**outbound, 'server': '198.51.100.11'}]}
        documents, pool = build_pool({'cfg-live': old, 'cfg-reserve': reserve}, None, old, 'eth0')
        live = documents['cfg-live']

        refreshed, next_pool = build_pool({'cfg-reserve': reserve}, pool, live, 'eth0')

        self.assertEqual(next_pool, pool)
        self.assertEqual(refreshed['cfg-reserve'], documents['cfg-reserve'])
        self.assertEqual(live['outbounds'][0]['type'], 'socks')
        self.assertEqual(live['outbounds'][0]['server'], '127.0.0.1')
        self.assertEqual(next_pool['outbounds'][0]['streamSettings']['sockopt']['interface'], 'eth0')

    def test_failed_probe_restores_the_previous_pool(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / 'provider-pool.json'
            path.write_text('{"previous": true}\n', encoding='utf-8')
            with patch('xray_transport.subprocess.run') as run:
                run.return_value.returncode = 0
                with self.assertRaisesRegex(SubscriptionError, 'no healthy candidates'):
                    with activate_pool({'replacement': True}, {'xray_config_path': str(path)}):
                        self.assertEqual(json.loads(path.read_text()), {'replacement': True})
                        raise SubscriptionError('no healthy candidates')
                self.assertEqual(json.loads(path.read_text()), {'previous': True})
                self.assertEqual(
                    [call.args[0] for call in run.call_args_list[1:]],
                    [['systemctl', 'restart', 'xray-provider-pool.service']] * 2,
                )


if __name__ == '__main__':
    unittest.main()
