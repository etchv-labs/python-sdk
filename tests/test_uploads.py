import json
import unittest
from urllib.parse import parse_qs

import httpx

from etchv import Etchv

ID = 'ab' * 32
UPLOAD = 'upl_' + '1' * 32
URL = 'https://uploads.etchv.com/image/' + UPLOAD + '.png?Expires=1&Signature=s&Key-Pair-Id=K1'
PNG = b'\x89PNG\r\n\x1a\n' + b'x' * 200


def form(request):
    return {k: v[0] for k, v in parse_qs(request.read().decode()).items()}


class UploadSessionTests(unittest.TestCase):
    def session(self, kind):
        return {'upload_id': UPLOAD, 'kind': kind, 'filename': 'photo.png', 'size': len(PNG), 'status': 'pending',
                'expires_at': '2026-10-10T00:00:00+00:00', 'upload': {'method': 'PUT', 'url': URL.replace('image', kind), 'expires_at': 'x'}}

    def test_large_embed_uploads_once_then_sends_the_upload_id(self):
        calls = []
        def handle(request):
            calls.append((request.method, request.url.host, request.url.path))
            if request.url.path == '/uploads':
                self.assertEqual(json.loads(request.read()), {'kind': 'image', 'filename': 'photo.png', 'size': len(PNG)})
                return httpx.Response(201, json=self.session('image'))
            if request.url.host == 'uploads.etchv.com':
                self.assertNotIn('x-api-key', request.headers)
                self.assertEqual(request.read(), PNG)
                return httpx.Response(200)
            self.assertEqual(request.headers['idempotency-key'], 'key-1')
            self.assertEqual(form(request), {'data': '{"recipient": "test"}', 'upload_id': UPLOAD})
            return httpx.Response(200, content=PNG, headers={'content-type': 'image/png', 'x-watermark-id': ID})
        with Etchv('test-key', transport=httpx.MockTransport(handle), large_file_threshold=100) as sdk:
            result = sdk.embed_image(PNG, {'recipient': 'test'}, filename='photo.png', idempotency_key='key-1')
        self.assertEqual(result.watermark_id, ID)
        self.assertEqual(calls, [('POST', 'api.etchv.com', '/uploads'), ('PUT', 'uploads.etchv.com', '/image/' + UPLOAD + '.png'),
                                 ('POST', 'api.etchv.com', '/watermarks/images')])

    def test_small_files_still_go_in_the_request_body(self):
        def handle(request):
            self.assertIn(b'name="file"', request.read())
            return httpx.Response(200, content=PNG, headers={'content-type': 'image/png', 'x-watermark-id': ID})
        with Etchv('test-key', transport=httpx.MockTransport(handle)) as sdk:
            sdk.embed_image(PNG, {'recipient': 'test'})

    def test_retries_resend_the_same_upload_without_uploading_again(self):
        puts, posts = [], []
        def handle(request):
            if request.url.path == '/uploads':
                return httpx.Response(201, json=self.session('image'))
            if request.url.host == 'uploads.etchv.com':
                puts.append(1)
                return httpx.Response(200)
            posts.append(form(request)['upload_id'])
            if len(posts) == 1:
                return httpx.Response(503, json={'detail': 'busy'}, headers={'retry-after': '0.01'})
            return httpx.Response(200, content=PNG, headers={'content-type': 'image/png', 'x-watermark-id': ID})
        with Etchv('test-key', transport=httpx.MockTransport(handle), large_file_threshold=100) as sdk:
            sdk.embed_image(PNG, {'recipient': 'test'})
        self.assertEqual((len(puts), posts), (1, [UPLOAD, UPLOAD]))

    def test_large_sync_image_detection_runs_as_a_job(self):
        request_id = 'req_' + 'c' * 64
        paths = []
        big = PNG + b'x' * (95 * 1024 * 1024)
        def handle(request):
            paths.append(request.url.path)
            if request.url.path == '/uploads':
                self.assertEqual(json.loads(request.read())['kind'], 'detect')
                return httpx.Response(201, json=self.session('detect'))
            if request.url.host == 'uploads.etchv.com':
                return httpx.Response(200)
            if request.url.path == '/watermarks/images/detect/async':
                return httpx.Response(202, json={'request_id': request_id})
            return httpx.Response(200, json={'watermarked': True, 'confidence': .99, 'watermark_id': ID,
                                             'units': [{'index': 0, 'watermarked': True, 'confidence': .99, 'watermark_id': ID}]})
        with Etchv('test-key', transport=httpx.MockTransport(handle)) as sdk:
            result = sdk.detect_image(big)
        self.assertEqual(result.watermark_id, ID)
        self.assertEqual(paths, ['/uploads', '/detect/' + UPLOAD + '.png', '/watermarks/images/detect/async',
                                 f'/watermarks/detection-jobs/{request_id}/result'])

    def test_limits_follow_the_operation(self):
        with Etchv('test-key', transport=httpx.MockTransport(lambda r: httpx.Response(500))) as sdk:
            with self.assertRaises(ValueError):
                sdk.embed_image(b'x' * (50 * 1024 * 1024 + 1), {'a': 1})
            with self.assertRaises(ValueError):
                sdk.detect_image(b'x' * (192 * 1024 * 1024 + 1))
            with self.assertRaises(ValueError):
                sdk.upload_file('audio', PNG)

    def test_a_refused_upload_raises(self):
        def handle(request):
            if request.url.path == '/uploads':
                return httpx.Response(201, json=self.session('image'))
            return httpx.Response(403, text='Forbidden')
        with Etchv('test-key', transport=httpx.MockTransport(handle)) as sdk:
            with self.assertRaises(Exception) as error:
                sdk.upload_file('image', PNG)
        self.assertEqual(error.exception.status_code, 403)
