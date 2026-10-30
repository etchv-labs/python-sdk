import unittest
from unittest import mock
from urllib.parse import parse_qs

import httpx

from etchv import ConflictError, DeadlineExceededError, Etchv

ID = 'ab' * 32
ASSET = 'ast_' + 'a' * 64
DELIVERY = 'std_' + 'd' * 64
PNG = b'\x89PNG\r\n\x1a\n' + b'x' * 200
DETECTED = {'watermarked': True, 'confidence': .99, 'watermark_id': ID}


def conflict(code, **headers):
    return httpx.Response(409, json={'detail': {'code': code, 'message': code}}, headers=headers)


def client(handler, **options):
    return Etchv('test-key', transport=httpx.MockTransport(handler), **options)


@mock.patch('etchv.client.time.sleep')
class InstantDetectionRetryTests(unittest.TestCase):
    def test_a_dropped_connection_is_resent_with_the_same_generated_key(self, sleep):
        keys = []
        def handle(request):
            keys.append(request.headers.get('idempotency-key'))
            if len(keys) == 1:
                raise httpx.ReadError('connection reset')
            return httpx.Response(200, json=DETECTED, headers={'x-request-id': 'req_1', 'idempotent-replayed': 'true'})
        with client(handle) as sdk:
            for detect in (sdk.detect_image, sdk.detect_document):
                keys.clear()
                self.assertEqual(detect(PNG).watermark_id, ID)
                self.assertEqual(len(keys), 2)
                self.assertRegex(keys[0], r'^[a-f0-9]{32}$')
                self.assertEqual(keys[0], keys[1])

    def test_a_given_key_is_kept(self, sleep):
        keys = []
        def handle(request):
            keys.append(request.headers['idempotency-key'])
            if len(keys) == 1:
                raise httpx.ConnectError('reset')
            return httpx.Response(200, json=DETECTED)
        with client(handle) as sdk:
            sdk.detect_image(PNG, idempotency_key='scan-0001')
        self.assertEqual(keys, ['scan-0001', 'scan-0001'])

    def test_request_in_progress_waits_for_retry_after_and_sends_again(self, sleep):
        responses = iter([conflict('request_in_progress', **{'retry-after': '2'}), httpx.Response(200, json=DETECTED)])
        with client(lambda request: next(responses)) as sdk:
            self.assertTrue(sdk.detect_image(PNG).watermarked)
        self.assertEqual([call.args[0] for call in sleep.call_args_list], [2.0])

    def test_request_in_progress_waits_are_bounded(self, sleep):
        responses = iter([conflict('request_in_progress', **{'retry-after': '600'}), httpx.Response(200, json=DETECTED)])
        with client(lambda request: next(responses)) as sdk:
            sdk.detect_document(PNG)
        self.assertEqual([call.args[0] for call in sleep.call_args_list], [5])

    def test_replay_expired_and_request_failed_raise_conflicts_without_retrying(self, sleep):
        for code in ('replay_expired', 'request_failed'):
            keys = []
            def handle(request):
                keys.append(request.headers['idempotency-key'])
                return conflict(code)
            with client(handle) as sdk:
                with self.assertRaises(ConflictError) as error:
                    sdk.detect_image(PNG)
            self.assertEqual((error.exception.code, len(keys)), (code, 1))
            self.assertEqual(error.exception.idempotency_key, keys[0])

    def test_other_errors_are_still_not_retried(self, sleep):
        for status in (429, 503):
            calls = []
            def handle(request):
                calls.append(1)
                return httpx.Response(status, json={'detail': 'busy'})
            with client(handle) as sdk:
                with self.assertRaises(Exception):
                    sdk.detect_image(PNG)
            self.assertEqual(len(calls), 1)

    def test_running_out_of_time_reports_the_key(self, sleep):
        def handle(request):
            raise httpx.ConnectError('reset')
        ticks = iter(range(0, 1000, 50))
        with mock.patch('etchv.client.time.monotonic', side_effect=lambda: next(ticks)), client(handle) as sdk:
            with self.assertRaises(DeadlineExceededError) as error:
                sdk.detect_image(PNG, idempotency_key='scan-0002')
        self.assertEqual(error.exception.detail['idempotency_key'], 'scan-0002')
        self.assertEqual(error.exception.idempotency_key, 'scan-0002')

    def test_keep_result_is_a_query_parameter(self, sleep):
        queries = []
        def handle(request):
            queries.append(dict(request.url.params))
            return httpx.Response(200, json=DETECTED)
        with client(handle) as sdk:
            sdk.detect_image(PNG)
            sdk.detect_image(PNG, keep_result=False)
            sdk.detect_document(PNG, keep_result=True, accelerator='gpu')
            with self.assertRaises(ValueError):
                sdk.detect_image(PNG, keep_result='no')
        self.assertEqual(queries, [{}, {'keep_result': 'false'}, {'accelerator': 'gpu', 'keep_result': 'true'}])

    def test_a_consumed_upload_is_replaced_by_a_new_upload_with_the_same_key(self, sleep):
        uploads, posts = iter(['upl_' + '1' * 32, 'upl_' + '2' * 32]), []
        def handle(request):
            if request.url.path == '/uploads':
                upload_id = next(uploads)
                return httpx.Response(201, json={'upload_id': upload_id, 'kind': 'detect', 'status': 'pending', 'upload': {
                    'method': 'PUT', 'url': f'https://uploads.etchv.com/detect/{upload_id}.png'}})
            if request.url.host == 'uploads.etchv.com':
                return httpx.Response(200)
            form = {k: v[0] for k, v in parse_qs(request.read().decode()).items()}
            posts.append((request.headers['idempotency-key'], form['upload_id']))
            if len(posts) == 1:
                raise httpx.ReadError('connection reset')
            if len(posts) == 2:
                return conflict('upload_consumed')
            return httpx.Response(200, json=DETECTED)
        with client(handle, large_file_threshold=100) as sdk:
            sdk.detect_image(PNG, keep_result=False)
        key = posts[0][0]
        self.assertEqual(posts, [(key, 'upl_' + '1' * 32), (key, 'upl_' + '1' * 32), (key, 'upl_' + '2' * 32)])

    def test_an_upload_still_held_by_the_first_copy_is_waited_for(self, sleep):
        uploads, posts = [], []
        def handle(request):
            if request.url.path == '/uploads':
                uploads.append(1)
                return httpx.Response(201, json={'upload_id': 'upl_' + '1' * 32, 'upload': {
                    'method': 'PUT', 'url': 'https://uploads.etchv.com/detect/x.png'}})
            if request.url.host == 'uploads.etchv.com':
                return httpx.Response(200)
            form = {k: v[0] for k, v in parse_qs(request.read().decode()).items()}
            posts.append((request.headers['idempotency-key'], form['upload_id']))
            if len(posts) == 1:
                raise httpx.RemoteProtocolError('Server disconnected without sending a response.')
            if len(posts) == 2:
                return conflict('upload_in_use')
            return httpx.Response(200, json=DETECTED)
        with client(handle, large_file_threshold=100) as sdk:
            self.assertTrue(sdk.detect_image(PNG).watermarked)
        self.assertEqual(len(uploads), 1)
        self.assertEqual(len(set(posts)), 1)
        self.assertEqual([call.args[0] for call in sleep.call_args_list], [1, 1])

    def test_upload_in_use_is_not_waited_for_on_a_file_in_the_request_body(self, sleep):
        with client(lambda request: conflict('upload_in_use')) as sdk:
            with self.assertRaises(ConflictError) as error:
                sdk.detect_image(PNG)
        self.assertRegex(error.exception.idempotency_key, r'^[a-f0-9]{32}$')

    def test_the_resend_after_a_new_upload_keeps_the_first_deadline(self, sleep):
        now, posts = [0.0], []
        def handle(request):
            if request.url.path == '/uploads':
                return httpx.Response(201, json={'upload_id': 'upl_' + '1' * 32, 'upload': {
                    'method': 'PUT', 'url': 'https://uploads.etchv.com/detect/x.png'}})
            if request.url.host == 'uploads.etchv.com':
                return httpx.Response(200)
            posts.append(request.headers['idempotency-key'])
            now[0] += 121  # The first send used up the 120 s client timeout.
            return conflict('upload_consumed')
        with mock.patch('etchv.client.time.monotonic', side_effect=lambda: now[0]), \
                client(handle, large_file_threshold=100) as sdk:
            with self.assertRaises(DeadlineExceededError) as error:
                sdk.detect_image(PNG, idempotency_key='scan-0004')
        self.assertEqual((posts, error.exception.idempotency_key), (['scan-0004'], 'scan-0004'))

    def test_a_network_error_from_the_new_upload_carries_the_key(self, sleep):
        now, puts, keys = [0.0], [], []
        def handle(request):
            if request.url.path == '/uploads':
                return httpx.Response(201, json={'upload_id': 'upl_' + '1' * 32, 'upload': {
                    'method': 'PUT', 'url': 'https://uploads.etchv.com/detect/x.png'}})
            if request.url.host == 'uploads.etchv.com':
                puts.append(1)
                if len(puts) == 1:
                    return httpx.Response(200)
                now[0] += 10  # The new upload keeps failing until its own deadline passes.
                raise httpx.ConnectError('connection reset')
            keys.append(request.headers['idempotency-key'])
            if len(keys) == 1:
                raise httpx.ReadError('connection reset')
            return conflict('upload_consumed')
        with mock.patch('etchv.client.time.monotonic', side_effect=lambda: now[0]), \
                client(handle, large_file_threshold=100) as sdk:
            with self.assertRaises(httpx.ConnectError) as error:
                sdk.detect_image(PNG, keep_result=False)
        self.assertEqual(len(keys), 2)
        self.assertGreater(len(puts), 2)
        self.assertEqual(error.exception.idempotency_key, keys[0])

    def test_a_consumed_upload_on_embedding_is_not_uploaded_again(self, sleep):
        calls = []
        def handle(request):
            calls.append(request.url.path)
            if request.url.path == '/uploads':
                return httpx.Response(201, json={'upload_id': 'upl_' + '1' * 32, 'upload': {
                    'method': 'PUT', 'url': 'https://uploads.etchv.com/image/x.png'}})
            if request.url.host == 'uploads.etchv.com':
                return httpx.Response(200)
            return conflict('upload_consumed')
        with client(handle, large_file_threshold=100) as sdk:
            with self.assertRaises(ConflictError):
                sdk.embed_image(PNG, {'recipient': 'a'})
        self.assertEqual(calls.count('/uploads'), 1)


@mock.patch('etchv.client.time.sleep')
class DownloadRetryTests(unittest.TestCase):
    def test_downloads_retry_a_dropped_connection(self, sleep):
        for path, download, identifier in ((f'/assets/{ASSET}/content', 'download_asset', ASSET),
                                           (f'/storage/deliveries/{DELIVERY}/content', 'download_storage_delivery', DELIVERY)):
            calls = []
            def handle(request):
                calls.append(request.url.path)
                if len(calls) == 1:
                    raise httpx.RemoteProtocolError('peer closed connection')
                return httpx.Response(200, content=b'file')
            with client(handle) as sdk:
                self.assertEqual(getattr(sdk, download)(identifier), b'file')
            self.assertEqual(calls, [path, path])

    def test_download_errors_are_not_retried(self, sleep):
        calls = []
        def handle(request):
            calls.append(1)
            return httpx.Response(503, json={'detail': 'busy'})
        with client(handle) as sdk:
            with self.assertRaises(Exception):
                sdk.download_asset(ASSET)
        self.assertEqual(len(calls), 1)

    def test_a_download_out_of_time_raises_the_dropped_connection(self, sleep):
        def handle(request):
            raise httpx.ConnectError('reset')
        ticks = iter(range(0, 1000, 50))
        with mock.patch('etchv.client.time.monotonic', side_effect=lambda: next(ticks)), client(handle) as sdk:
            with self.assertRaises(httpx.ConnectError):
                sdk.download_asset(ASSET)


if __name__ == '__main__':
    unittest.main()
