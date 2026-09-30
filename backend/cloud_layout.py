"""CI renders template text from metadata; no host-side raster decoding."""
import base64
import unicodedata


def encoded(value):
    return base64.urlsafe_b64encode(value.encode('utf-8')).decode().rstrip('=')


def text_rule(width, height, template, values):
    layout = template.get('layout') or 'postcard_bottom'
    if layout not in ('postcard_bottom', 'poster_center', 'stamp_corner'):
        raise ValueError('该文字排版尚未配置云端处理')
    roles = {f.get('role', 'body'): values[f['key']] for f in template.get('text_fields', [])
             if values.get(f.get('key'))}
    lines = []
    order = ('title', 'subtitle', 'body', 'date', 'sign')
    for role in order:
        text = roles.get(role, '')
        for line in text.splitlines():
            if line.strip(): lines.append((role, line.strip()))
    rules = []; measured=[]
    for role, line in lines:
        units = sum(1 if unicodedata.east_asian_width(ch) in 'WF' else .65 for ch in line)
        size = max(8, min(int(width * (.032 if role == 'title' else .020)),
                          int(width * .85 / max(1, units) * .65)))
        measured.append((role,line,size))
    offset=int(height*.04)
    ordered=measured if layout=='stamp_corner' else list(reversed(measured))
    for role,line,size in ordered:
        gravity = 'NorthEast' if layout == 'stamp_corner' else ('South' if layout == 'poster_center' else 'SouthWest')
        dy=offset
        offset+=size*2+8
        if dy > height*.45: raise ValueError('文字过多，超出云端排版区域')
        color = '#FFFFFF' if layout == 'poster_center' else '#1F2937'
        rules.append(f'watermark/2/text/{encoded(line)}/font/{encoded("simhei.ttf")}'
                     f'/fontsize/{size}/fill/{encoded(color)}/dissolve/100/gravity/{gravity}'
                     f'/dx/{int(width*.055)}/dy/{dy}/shadow/50')
    return '|'.join(rules)
