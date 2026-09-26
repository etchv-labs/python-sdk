import hashlib
import hmac
import json
import tomllib
import unittest
from pathlib import Path
import httpx
import etchv
from etchv import (Etchv, EtchvError, AuthenticationError, ConflictError, GoneError, NotFoundError,
                   DeadlineExceededError, WebhookVerificationError, verify_webhook)

WH = 'wh_' + 'a' * 32
EVT = 'evt_' + 'b' * 64
DST = 'dst_' + 'c' * 32
STD = 'std_' + 'd' * 64
AST = 'ast_' + 'e' * 64
REQ = 'req_' + 'f' * 64
WID = 'ab' * 32


def client(handler, **options):
    return Etchv('test-key', transport=httpx.MockTransport(handler), **options)


class VersionTests(unittest.TestCase):
    def test_version_and_user_agent(self):
        project = tomllib.loads(Path(__file__).parents[1].joinpath('pyproject.toml').read_text())
        self.assertEqual(etchv.__version__, '1.0.0')
        self.assertEqual(project['project']['version'], etchv.__version__)
        def handle(request):
            self.assertEqual(request.headers['user-agent'], 'etchv-python/1.0.0')
            self.assertEqual(request.url.path, '/auth/api-key')
            return httpx.Response(200, json={'organization_id': 'org_1', 'key_id': 'key_1', 'scopes': ['watermarks:embed']})
        with client(handle) as sdk:
            self.assertEqual(sdk.check_api_key()['scopes'], ['watermarks:embed'])


class ErrorTests(unittest.TestCase):
    def test_typed_errors_carry_status_and_request_id(self):
        for status, kind in [(401, AuthenticationError), (404, NotFoundError), (409, ConflictError), (410, GoneError), (500, EtchvError)]:
            with client(lambda r: httpx.Response(status, json={'detail': 'no'}, headers={'x-request-id': 'req_trace'})) as sdk:
                with self.assertRaises(kind) as caught:
                    sdk.check_api_key()
                self.assertIsInstance(caught.exception, EtchvError)
                self.assertEqual((caught.exception.status_code, caught.exception.request_id), (status, 'req_trace'))
                self.assertIn('req_trace', str(caught.exception))
                self.assertNotIn('test-key', str(caught.exception) + repr(caught.exception))

    def test_expired_result_is_gone_and_not_retried(self):
        calls = []
        def handle(request):
            calls.append(request)
            return httpx.Response(410, json={'status': 'expired', 'request_id': REQ}, headers={'x-request-id': REQ})
        with client(handle) as sdk:
            with self.assertRaises(GoneError) as caught:
                sdk.get_embed_result(REQ)
        self.assertEqual((caught.exception.request_id, len(calls)), (REQ, 1))

    def test_deadline_is_typed(self):
        with client(lambda r: httpx.Response(202, json={'request_id': REQ}), timeout=.02) as sdk:
            with self.assertRaises(DeadlineExceededError) as caught:
                sdk.get_detection_result(REQ)
        self.assertEqual(caught.exception.status_code, 0)
        self.assertEqual(caught.exception.request_id, REQ)


class JobTests(unittest.TestCase):
    def test_detection_result_polls_until_ready(self):
        calls = []
        def handle(request):
            calls.append(request.url.path)
            if len(calls) == 1:
                return httpx.Response(202, json={'request_id': REQ, 'status': 'running'}, headers={'retry-after': '0.01'})
            return httpx.Response(200, json={'watermarked': True, 'confidence': .9, 'watermark_id': WID,
                'units': [{'index': 0, 'watermarked': True, 'confidence': .9, 'watermark_id': WID}]}, headers={'x-request-id': REQ})
        with client(handle) as sdk:
            result = sdk.get_detection_result(REQ)
            self.assertEqual((result.watermark_id, result.request_id, len(result.units)), (WID, REQ, 1))
            with self.assertRaises(ValueError): sdk.get_detection_result('req_bad')
        self.assertEqual(calls, [f'/watermarks/detection-jobs/{REQ}/result'] * 2)

    def test_job_status_paths(self):
        paths = []
        def handle(request):
            paths.append(request.url.path)
            return httpx.Response(200, json={'request_id': REQ, 'status': 'queued'})
        with client(handle) as sdk:
            sdk.get_job(REQ); sdk.get_job(REQ, detect=True)
        self.assertEqual(paths, [f'/watermarks/jobs/{REQ}', f'/watermarks/detection-jobs/{REQ}'])


class WebhookTests(unittest.TestCase):
    def test_endpoint_management(self):
        calls = []
        endpoint = {'id': WH, 'url': 'https://example.com/hook', 'enabled': True, 'created_at': '2026-09-12T00:00:00Z'}
        def handle(request):
            calls.append((request.method, request.url.path, request.url.params.get('after'), json.loads(request.content or b'null')))
            path = request.url.path
            if request.method == 'POST' and path == '/webhooks': return httpx.Response(201, json={**endpoint, 'signing_secret': 'whsec_test'})
            if request.method == 'PATCH': return httpx.Response(200, json={**endpoint, 'enabled': False})
            if request.method == 'DELETE': return httpx.Response(204)
            if path.endswith('/redeliver'): return httpx.Response(202, json={'id': EVT, 'status': 'queued'})
            if path.endswith('/deliveries'): return httpx.Response(200, json={'data': [], 'next_cursor': None})
            return httpx.Response(200, json=[endpoint])
        with client(handle) as sdk:
            self.assertEqual(sdk.list_webhooks()[0]['id'], WH)
            self.assertEqual(sdk.create_webhook('https://example.com/hook')['signing_secret'], 'whsec_test')
            self.assertFalse(sdk.update_webhook(WH, enabled=False)['enabled'])
            self.assertEqual(sdk.list_webhook_deliveries(WH, after=EVT)['data'], [])
            self.assertEqual(sdk.redeliver_webhook(WH, EVT)['status'], 'queued')
            sdk.delete_webhook(WH)
            before = len(calls)
            for bad in (lambda: sdk.delete_webhook('../assets'), lambda: sdk.redeliver_webhook(WH, 'evt_x'),
                        lambda: sdk.create_webhook('http://example.com'), lambda: sdk.update_webhook(WH, enabled='no')):
                with self.assertRaises(ValueError): bad()
            self.assertEqual(len(calls), before)
        self.assertEqual(calls, [
            ('GET', '/webhooks', None, None),
            ('POST', '/webhooks', None, {'url': 'https://example.com/hook'}),
            ('PATCH', f'/webhooks/{WH}', None, {'enabled': False}),
            ('GET', f'/webhooks/{WH}/deliveries', EVT, None),
            ('POST', f'/webhooks/{WH}/deliveries/{EVT}/redeliver', None, None),
            ('DELETE', f'/webhooks/{WH}', None, None),
        ])

    def test_verify_webhook(self):
        secret = 'whsec_test'
        body = json.dumps({'id': EVT, 'type': 'watermark.embed.succeeded'}).encode()
        def sign(timestamp, payload=body):
            return 'v1=' + hmac.new(secret.encode(), str(timestamp).encode() + b'.' + payload, hashlib.sha256).hexdigest()
        headers = {'X-Etchv-Event-ID': EVT, 'X-Etchv-Timestamp': '1000', 'X-Etchv-Signature': sign(1000)}
        self.assertEqual(verify_webhook(body, headers, secret, now=1100)['type'], 'watermark.embed.succeeded')
        lower = {k.lower(): v for k, v in headers.items()}
        self.assertEqual(verify_webhook(body, lower, secret, now=1000)['id'], EVT)
        for bad_headers, payload, now in [
            (headers, body, 1301),
            ({**headers, 'X-Etchv-Signature': sign(1000, b'{}')}, body, 1000),
            (headers, body.replace(b'succeeded', b'failed'), 1000),
            ({**headers, 'X-Etchv-Event-ID': 'evt_other'}, body, 1000),
            ({k: v for k, v in headers.items() if k != 'X-Etchv-Timestamp'}, body, 1000),
        ]:
            with self.assertRaises(WebhookVerificationError):
                verify_webhook(payload, bad_headers, secret, now=now)
        with self.assertRaises(WebhookVerificationError):
            verify_webhook(body, headers, 'whsec_other', now=1000)


class StorageTests(unittest.TestCase):
    def test_destinations_and_deliveries(self):
        calls = []
        destination = {'id': DST, 'name': 'Archive', 'provider': 's3', 'bucket': 'archive-bucket', 'verified_at': None}
        delivery = {'id': STD, 'asset_id': AST, 'destination_id': DST, 'status': 'queued'}
        def handle(request):
            calls.append((request.method, request.url.path, request.url.params.get('after'), json.loads(request.content or b'null')))
            path = request.url.path
            if request.method == 'DELETE': return httpx.Response(204)
            if path.endswith('/content'): return httpx.Response(200, content=b'stored-bytes')
            if path.endswith('/retry') or (request.method == 'POST' and path.endswith('/deliveries')):
                return httpx.Response(202, json=delivery)
            if path.endswith('/deliveries'): return httpx.Response(200, json={'items': [delivery], 'next_cursor': None})
            if path.startswith('/storage/deliveries/'): return httpx.Response(200, json={**delivery, 'status': 'stored'})
            if path.endswith('/verify'): return httpx.Response(200, json={**destination, 'verified_at': '2026-09-12T00:00:00Z'})
            if request.method == 'POST': return httpx.Response(201, json=destination)
            if request.method == 'PATCH': return httpx.Response(200, json={**destination, 'enabled': False})
            return httpx.Response(200, json=[destination])
        with client(handle) as sdk:
            self.assertEqual(sdk.list_storage_destinations()[0]['id'], DST)
            created = sdk.create_storage_destination(name='Archive', provider='s3', bucket='archive-bucket',
                region='us-east-1', role_arn='arn:aws:iam::123456789012:role/etchv-storage-archive')
            self.assertEqual(created['id'], DST)
            self.assertFalse(sdk.update_storage_destination(DST, enabled=False)['enabled'])
            self.assertTrue(sdk.verify_storage_destination(DST)['verified_at'])
            self.assertEqual(sdk.list_storage_deliveries(DST, after=STD)['items'][0]['id'], STD)
            self.assertEqual(sdk.create_storage_delivery(DST, AST, key='exports/a.png')['status'], 'queued')
            self.assertEqual(sdk.get_storage_delivery(STD)['status'], 'stored')
            self.assertEqual(sdk.retry_storage_delivery(STD)['id'], STD)
            self.assertEqual(sdk.download_storage_delivery(STD), b'stored-bytes')
            sdk.delete_storage_destination(DST)
            before = len(calls)
            for bad in (lambda: sdk.update_storage_destination(DST), lambda: sdk.verify_storage_destination('dst_1'),
                        lambda: sdk.create_storage_delivery(DST, 'ast_1'), lambda: sdk.retry_storage_delivery('../x')):
                with self.assertRaises(ValueError): bad()
            self.assertEqual(len(calls), before)
        self.assertEqual([c[:2] for c in calls], [
            ('GET', '/storage/destinations'), ('POST', '/storage/destinations'),
            ('PATCH', f'/storage/destinations/{DST}'), ('POST', f'/storage/destinations/{DST}/verify'),
            ('GET', f'/storage/destinations/{DST}/deliveries'), ('POST', f'/storage/destinations/{DST}/deliveries'),
            ('GET', f'/storage/deliveries/{STD}'), ('POST', f'/storage/deliveries/{STD}/retry'),
            ('GET', f'/storage/deliveries/{STD}/content'), ('DELETE', f'/storage/destinations/{DST}'),
        ])
        self.assertEqual(calls[1][3]['provider'], 's3')
        self.assertEqual(calls[2][3], {'enabled': False})
        self.assertEqual(calls[4][2], STD)
        self.assertEqual(calls[5][3], {'asset_id': AST, 'key': 'exports/a.png'})

    def test_storage_errors_do_not_echo_credentials(self):
        def handle(request):
            return httpx.Response(422, json={'detail': [{'loc': ['body', 'credentials'], 'msg': 'Invalid storage field'}]})
        with client(handle) as sdk:
            with self.assertRaises(EtchvError) as caught:
                sdk.update_storage_destination(DST, credentials='sv=2026&sig=private-signature')
        self.assertNotIn('private-signature', str(caught.exception))


if __name__ == '__main__': unittest.main()
