"""CI renders template text from metadata; no host-side raster decoding."""
import base64
import unicodedata


def encoded(value):
    return base64.urlsafe_b64encode(value.encode('utf-8')).decode().rstrip('=')


def processing_batches(rule, limit=10):
    """CI accepts at most ten sequential operations in one persistence request."""
    layers = [layer for layer in rule.split('|') if layer]
    return ['|'.join(layers[i:i+limit]) for i in range(0, len(layers), limit)]


def _wrapped_lines(text, max_units):
    """Bound watermark width, retaining explicit paragraphs and every glyph."""
    for paragraph in text.splitlines():
        line='';units=0
        for ch in paragraph.strip():
            unit=1 if unicodedata.east_asian_width(ch) in 'WF' else .65
            if line and units+unit>max_units:
                yield line
                line='';units=0
            line+=ch;units+=unit
        if line:yield line


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
        base_size=max(8,int(width*.020)) if role=='body' else 8
        for line in _wrapped_lines(text,max(1,width*.85*.65/base_size)):
            lines.append((role,line))
    rules = []; measured=[]
    for role, line in lines:
        units = sum(1 if unicodedata.east_asian_width(ch) in 'WF' else .65 for ch in line)
        size = max(8, min(int(width * (.032 if role == 'title' else .020)),
                          int(width * .85 / max(1, units) * .65)))
        measured.append((role,line,size))
    offset=int(height*.04)
    # Fit complete multiline fields rather than failing after paid generation.
    # Keep normal layouts unchanged; dense paragraphs use smaller type/leading.
    gap=8
    available=max(1, int(height*.45)-offset)
    while measured and sum(size*2+gap for _,_,size in measured)>available:
        if any(size>8 for _,_,size in measured):
            measured=[(role,line,max(8,int(size*.9))) for role,line,size in measured]
        elif gap>2:
            gap-=1
        else:
            available=max(available,int(height*.85)-offset)
            if sum(size*2+gap for _,_,size in measured)>available:
                raise ValueError('文字过多，超出云端排版区域')
            break
    ordered=measured if layout=='stamp_corner' else list(reversed(measured))
    for role,line,size in ordered:
        gravity = 'NorthEast' if layout == 'stamp_corner' else ('South' if layout == 'poster_center' else 'SouthWest')
        dy=offset
        offset+=size*2+gap
        color = '#FFFFFF' if layout == 'poster_center' else '#1F2937'
        rules.append(f'watermark/2/text/{encoded(line)}/font/{encoded("simhei.ttf")}'
                     f'/fontsize/{size}/fill/{encoded(color)}/dissolve/100/gravity/{gravity}'
                     f'/dx/{int(width*.055)}/dy/{dy}/shadow/50')
    return '|'.join(rules)
