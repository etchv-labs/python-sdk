import unittest
import httpx
from etchv import Etchv, EtchvError

PNG = b'\x89PNG\r\n\x1a\nimage'
ID = 'ab' * 32

class ClientTests(unittest.TestCase):
    def test_embed_multipart_and_headers(self):
        def handle(request):
            self.assertEqual(str(request.url), 'https://pilot.api.etchv.com/watermarks/images')
            self.assertEqual(request.headers['x-api-key'], 'test-key')
            self.assertEqual(request.headers['idempotency-key'], 'unique-request')
            body = request.read()
            self.assertIn(b'name="file"; filename="photo.png"', body)
            self.assertIn(b'{"recipient": "test"}', body)
            self.assertIn(PNG, body)
            return httpx.Response(200, content=PNG, headers={'content-type':'image/png', 'x-watermark-id':ID, 'x-request-id':'req_1'})
        with Etchv('test-key', transport=httpx.MockTransport(handle)) as sdk:
            result = sdk.embed_image(PNG, {'recipient':'test'}, filename='photo.png', idempotency_key='unique-request')
            self.assertEqual((result.image, result.watermark_id, result.request_id), (PNG, ID, 'req_1'))

    def test_detection(self):
        for marked in [True, False]:
            def handle(request):
                self.assertEqual(request.url.path, '/watermarks/images/detect')
                return httpx.Response(200, json={'watermarked':marked, 'confidence':0.99 if marked else 0.5, 'watermark_id':ID if marked else None})
            with Etchv('test-key', transport=httpx.MockTransport(handle)) as sdk:
                self.assertEqual(sdk.detect_image(PNG).watermarked, marked)

    def test_errors_are_not_retried_or_redirected(self):
        for status in [302, 401, 402, 403, 409, 422, 429, 503]:
            calls = []
            def handle(request):
                calls.append(request)
                return httpx.Response(status, json={'detail':'failure'}, headers={'location':'https://example.com', 'x-request-id':'req_failure'})
            with Etchv('test-key', transport=httpx.MockTransport(handle)) as sdk:
                with self.assertRaises(EtchvError) as caught:
                    sdk.detect_image(PNG)
                self.assertEqual(caught.exception.status_code, status)
                self.assertEqual(caught.exception.request_id, 'req_failure')
                self.assertEqual(len(calls), 1)

    def test_invalid_responses(self):
        for payload in [{}, {'watermarked':True, 'confidence':0.9, 'watermark_id':'bad'}, {'watermarked':False,'confidence':2,'watermark_id':None}]:
            with Etchv('test-key', transport=httpx.MockTransport(lambda r: httpx.Response(200,json=payload))) as sdk:
                with self.assertRaises(EtchvError): sdk.detect_image(PNG)

    def test_invalid_input(self):
        with self.assertRaises(ValueError): Etchv('')
        with self.assertRaises(ValueError): Etchv('test-key', base_url='http://example.com')
        with Etchv('test-key') as sdk:
            with self.assertRaises(ValueError): sdk.embed_image(PNG, {})
            with self.assertRaises(ValueError): sdk.embed_image(PNG, {'value':float('nan')})
            with self.assertRaises(ValueError): sdk.detect_image(b'')

if __name__ == '__main__': unittest.main()
