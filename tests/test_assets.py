import json
from pathlib import Path
import httpx
import unittest
from etchv import Etchv, EtchvError

RECORD = json.loads(Path(__file__).with_name('assets.json').read_text())

class AssetsTests(unittest.TestCase):
    def test_asset_protocol_and_errors(self):
        calls=[]
        def respond(request):
            assert request.headers['X-API-Key']=='test-key'
            calls.append(request)
            if request.url.params.get('cursor'): return httpx.Response(409,json={'detail':'changed'})
            if request.method=='PATCH':
                body=json.loads(request.content)
                assert body=={'version':1,'name':'renamed'}
                return httpx.Response(200,json={**RECORD,'name':'renamed','version':2})
            if request.method=='DELETE': return httpx.Response(204)
            if request.method=='POST':
                assert json.loads(request.content)=={'asset_ids':[RECORD['id']]}
                return httpx.Response(204)
            if request.url.path.endswith('/content'):return httpx.Response(200,content=b'file')
            return httpx.Response(200,json={'items':[RECORD],'next_cursor':'next-page'} if request.url.path=='/assets' else RECORD)
        with Etchv('test-key',transport=httpx.MockTransport(respond)) as client:
            assert client.list_assets(kind='watermarked')['next_cursor']=='next-page'
            assert calls[0].url.params['kind']=='watermarked'
            assert client.get_asset(RECORD['id'])['metadata']['campaign']=='launch'
            assert client.update_asset(RECORD['id'],version=1,name='renamed')['version']==2
            assert client.download_asset(RECORD['id'])==b'file'
            client.delete_asset(RECORD['id']);client.delete_assets([RECORD['id']])
            with self.assertRaises(EtchvError) as error: client.list_assets(cursor='next-page')
            assert error.exception.status_code==409
            before=len(calls)
            with self.assertRaises(ValueError):client.get_asset('../other')
            assert len(calls)==before
