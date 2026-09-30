"""COS control protocol and gateway JSON boundaries; all network calls mocked."""
import hashlib,base64,json,sys,unittest
from pathlib import Path
from unittest.mock import Mock,patch
import xml.etree.ElementTree as ET
sys.path.insert(0,str(Path(__file__).resolve().parents[1]))
import cos_store as cos
from cloud_origin import merged_origin,merged_policy,MEDIA_HOST
from gateway_async import AsyncImages,GatewayAsyncError
import requests

class Fixture:
    def tencent(self):return {'secret_id':'fixture','secret_key':'fixture','cos_bucket':'fixture-123456','cos_region':'ap-guangzhou','cos_custom_domain':'cos.invalid'}
def response(http=200,body=b'{}',headers=None):
    r=Mock(status_code=http,content=body,headers=headers or {})
    r.__enter__=Mock(return_value=r);r.__exit__=Mock(return_value=False)
    r.iter_content=Mock(return_value=[body]);r.json=Mock(side_effect=lambda:json.loads(body));return r

class OriginTests(unittest.TestCase):
    def test_policy_is_prefix_service_write_not_public_read(self):
        p=json.loads(merged_policy(None,'fixture-123456','ap-guangzhou'));s=p['Statement'][0]
        self.assertEqual(s['Principal'],{'service':'cos.qcloud.com'})
        self.assertEqual(s['Resource'],['qcs::cos:ap-guangzhou:uid/123456:fixture-123456/images/*'])
        self.assertFalse(any('Get' in a or 'Delete' in a for a in s['Action']))
        self.assertEqual(p,json.loads(merged_policy(p,'fixture-123456','ap-guangzhou')))

    def test_policy_preserves_existing_statements(self):
        prior={'version':'2.0','Statement':[{'Effect':'Deny','Resource':['*'],'Action':['name/cos:DeleteObject'],'Principal':{'qcs':['fixture']}}]}
        p=json.loads(merged_policy(prior,'fixture-123456','ap-guangzhou'))
        self.assertEqual(p['Statement'][0],prior['Statement'][0]);self.assertEqual(len(prior['Statement']),1)

    def test_origin_is_404_only_https_and_no_signature_forwarding(self):
        root=ET.fromstring(merged_origin(None));r=root.find('OriginRule')
        self.assertEqual(r.findtext('OriginCondition/Prefix'),'images/')
        self.assertEqual(r.findtext('OriginCondition/HTTPStatusCode'),'404')
        self.assertEqual(r.findtext('OriginParameter/Protocol'),'HTTPS')
        self.assertEqual(r.findtext('OriginParameter/FollowQueryString'),'false')
        self.assertEqual(r.findtext('OriginParameter/FollowRedirection'),'false')
        self.assertEqual(r.findtext('OriginParameter/HttpHeader/FollowAllHeaders'),'false')
        self.assertEqual(r.findtext('OriginInfo/HostInfo/HostName'),MEDIA_HOST)

    def test_origin_idempotent_and_overlapping_existing_rules_stop(self):
        first=merged_origin(None);self.assertEqual(merged_origin(first),first)
        root=ET.fromstring(first);root.find('OriginRule/OriginCondition/Prefix').text=''
        with self.assertRaises(ValueError):merged_origin(ET.tostring(root))

    def test_origin_other_prefix_preserved(self):
        root=ET.fromstring(merged_origin(None));root.find('OriginRule/OriginCondition/Prefix').text='old/'
        result=ET.fromstring(merged_origin(ET.tostring(root)))
        self.assertEqual(len(result.findall('OriginRule')),2)
        self.assertEqual(result.findtext('OriginRule/OriginCondition/Prefix'),'old/')

    def test_signed_control_put_uses_direct_host_and_required_md5(self):
        with patch.object(requests,'request',return_value=response()) as req:
            cos.control_request(Fixture(),'PUT',params={'origin':''},data=b'<fixture/>')
        kw=req.call_args.kwargs
        self.assertIn('fixture-123456.cos.ap-guangzhou.myqcloud.com',req.call_args.args[1])
        self.assertEqual(kw['headers']['Content-MD5'],base64.b64encode(hashlib.md5(b'<fixture/>').digest()).decode())
        self.assertFalse(kw['allow_redirects']);self.assertNotIn('verify',kw)

    def test_import_trigger_does_not_read_image_body(self):
        r=response(body=b'NEVER_READ')
        with patch.object(requests,'request',return_value=r) as req:cos.trigger_mirror(Fixture(),'images/fixture.png')
        r.iter_content.assert_not_called();self.assertTrue(req.call_args.kwargs['stream'])

    def test_result_url_is_fixed_https_host_prefix_without_query(self):
        good='https://'+MEDIA_HOST+'/images/fixture.png'
        self.assertEqual(cos.mirror_key(good,MEDIA_HOST),'images/fixture.png')
        for bad in [good.replace('https:','http:'),good+'?token=x',good+'#x',good.replace('/images/','/results/'),
                    good.replace('/fixture.png','/../x'),good.replace('/fixture.png','/%2e%2e/x'),
                    good.replace(MEDIA_HOST,MEDIA_HOST+'.other.invalid'),good.replace('https://','https://user:pass@'),
                    good.replace(MEDIA_HOST,MEDIA_HOST+':bad'),good.replace('/fixture.png','/a\\b')]:
            with self.assertRaises(cos.CosError,msg=bad):cos.mirror_key(bad,MEDIA_HOST)

    def test_invalid_audit_xml_and_scene_error_fail_closed(self):
        for body in [b'<RecognitionResult/>',b'<RecognitionResult><Result>0</Result><PornInfo><Code>5</Code></PornInfo></RecognitionResult>']:
            with patch.object(requests,'request',return_value=response(body=body)):
                with self.assertRaises(cos.CosError):cos.audit_object(Fixture(),'results/x.jpg')
        with patch.object(requests,'request',return_value=response(body=b'<RecognitionResult><Result>0</Result><Label>Normal</Label><PornInfo><Code>0</Code></PornInfo></RecognitionResult>')):
            self.assertEqual(cos.audit_object(Fixture(),'results/x.jpg')['result'],0)

    def test_gateway_submit_json_once_and_no_auto_size(self):
        with patch.object(requests,'request',return_value=response(202,b'{"task_id":"imgtask_fixture"}')) as req:
            client=AsyncImages({'base_url':'https://fixture.invalid','api_key':'fixture'})
            self.assertEqual(client.submit('model','prompt','https://cos.invalid/source',size='auto'),'imgtask_fixture')
        req.assert_called_once();body=req.call_args.kwargs['json'];self.assertNotIn('size',body)
        self.assertNotIn('SecretKey',str(body));self.assertEqual(body['n'],1)

    def test_gateway_timeout_is_uncertain_and_not_retried(self):
        with patch.object(requests,'request',side_effect=requests.Timeout()) as req:
            with self.assertRaises(GatewayAsyncError) as e:AsyncImages({'base_url':'https://fixture.invalid','api_key':'fixture'}).submit('m','p')
        req.assert_called_once();self.assertTrue(e.exception.uncertain)

    def test_gateway_large_base64_metadata_is_rejected(self):
        with patch.object(requests,'request',return_value=response(200,b'x'*(256*1024+1))):
            with self.assertRaises(GatewayAsyncError):AsyncImages({'base_url':'https://fixture.invalid','api_key':'fixture'}).poll('imgtask_fixture')

    def test_gateway_path_cannot_redirect_bearer_and_task_id_is_strict(self):
        client=AsyncImages({'base_url':'https://fixture.invalid','api_key':'fixture'})
        for task in ['imgtask_a?token=x','imgtask_a/../x','imgtask_a#x']:
            with self.assertRaises(GatewayAsyncError):client.poll(task)
        with self.assertRaises(GatewayAsyncError):client.submit('m','p',endpoint='//other.invalid')
        with self.assertRaises(GatewayAsyncError):AsyncImages({'base_url':'http://fixture.invalid','api_key':'fixture'})

if __name__=='__main__':
    import os
    output=Path(os.environ.get('REVIEW_OUTPUT',str(Path(__file__).resolve().parents[2]/'audit/cloud-origin-tests')))
    output.mkdir(parents=True,exist_ok=True)
    suite=unittest.defaultTestLoader.loadTestsFromTestCase(OriginTests)
    names=[t._testMethodName for t in suite];result=unittest.TextTestRunner(verbosity=2).run(suite)
    failed={t._testMethodName for t,_ in result.failures+result.errors}
    (output/'cloud_origin_results.json').write_text(json.dumps({'cases':[{'case':n,'passed':n not in failed} for n in names]},indent=2),encoding='utf-8')
    print('CLOUD_ORIGIN_SUMMARY total=%d passed=%d failed=%d'%(result.testsRun,result.testsRun-len(failed),len(failed)))
    raise SystemExit(0 if result.wasSuccessful() else 1)
