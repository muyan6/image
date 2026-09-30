"""限定供应商 HTTPS 镜像回源；只合并精确前缀，不改公开读权限。"""
import copy
import json
import xml.etree.ElementTree as ET

MEDIA_HOST = 'media.canvas.worldcodes.online'
IMPORT_PREFIX = 'images/'


def merged_policy(previous, bucket, region):
    policy = copy.deepcopy(previous) if previous is not None else {'version': '2.0', 'Statement': []}
    statements = policy.get('Statement')
    if not isinstance(statements, list):
        raise ValueError('存储桶策略结构不匹配，请先核对原策略')
    appid = bucket.rsplit('-', 1)[-1]
    if not appid.isdigit():
        raise ValueError('COS 桶名缺少 APPID')
    statement = {
        'Principal': {'service': 'cos.qcloud.com'},
        'Effect': 'Allow',
        'Action': ['name/cos:PutObject', 'name/cos:InitiateMultipartUpload',
                   'name/cos:UploadPart', 'name/cos:CompleteMultipartUpload'],
        'Resource': [f'qcs::cos:{region}:uid/{appid}:{bucket}/{IMPORT_PREFIX}*'],
    }
    if statement not in statements:
        statements.append(statement)
    return json.dumps(policy, ensure_ascii=False, separators=(',', ':')).encode('utf-8')


def merged_origin(previous):
    root = ET.fromstring(previous) if previous else ET.Element('OriginConfiguration')
    if root.tag != 'OriginConfiguration':
        raise ValueError('回源配置结构不匹配')
    for rule in root.findall('OriginRule'):
        prefix = rule.findtext('OriginCondition/Prefix') or ''
        if IMPORT_PREFIX.startswith(prefix) or prefix.startswith(IMPORT_PREFIX):
            if (rule.findtext('OriginType') == 'Mirror'
                    and prefix == IMPORT_PREFIX
                    and rule.findtext('OriginCondition/HTTPStatusCode') == '404'
                    and rule.findtext('OriginParameter/Protocol') == 'HTTPS'
                    and rule.findtext('OriginParameter/FollowQueryString') == 'false'
                    and rule.findtext('OriginParameter/FollowRedirection') == 'false'
                    and rule.findtext('OriginParameter/HttpHeader/FollowAllHeaders') == 'false'
                    and len(rule.findall('OriginInfo/HostInfo')) == 1
                    and rule.findtext('OriginInfo/HostInfo/HostName') == MEDIA_HOST
                    and rule.find('OriginInfo/FileInfo') is None
                    and rule.find('OriginParameter/HttpHeader/NewHttpHeaders') is None):
                return ET.tostring(root, encoding='utf-8', xml_declaration=True)
            raise ValueError('现有回源规则与 images/ 重叠，保留原配置并停止写入')
    priorities = [int(r.findtext('RulePriority') or '0') for r in root.findall('OriginRule')]
    rule = ET.SubElement(root, 'OriginRule')
    ET.SubElement(rule, 'RulePriority').text = str(max(priorities, default=0) + 1)
    ET.SubElement(rule, 'OriginType').text = 'Mirror'
    condition = ET.SubElement(rule, 'OriginCondition')
    ET.SubElement(condition, 'HTTPStatusCode').text = '404'
    ET.SubElement(condition, 'Prefix').text = IMPORT_PREFIX
    params = ET.SubElement(rule, 'OriginParameter')
    ET.SubElement(params, 'Protocol').text = 'HTTPS'
    ET.SubElement(params, 'FollowQueryString').text = 'false'
    header = ET.SubElement(params, 'HttpHeader')
    ET.SubElement(header, 'FollowAllHeaders').text = 'false'
    ET.SubElement(params, 'FollowRedirection').text = 'false'
    info = ET.SubElement(rule, 'OriginInfo')
    host = ET.SubElement(info, 'HostInfo')
    ET.SubElement(host, 'HostName').text = MEDIA_HOST
    ET.SubElement(host, 'Weight').text = '1'
    return ET.tostring(root, encoding='utf-8', xml_declaration=True)
