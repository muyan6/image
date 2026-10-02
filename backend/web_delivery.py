"""Public-only web asset delivery; no account responses or image bytes are cached."""
from pathlib import Path
import re
from starlette.datastructures import Headers
from starlette.responses import FileResponse, Response
from starlette.staticfiles import StaticFiles
from starlette.exceptions import HTTPException

ASSET=re.compile(r'^[A-Za-z0-9][A-Za-z0-9_.-]*\.(js|css|svg)$')
VERSION=re.compile(r'^[0-9a-f]{16}$')
TYPES={'.js':'text/javascript; charset=utf-8','.css':'text/css; charset=utf-8','.svg':'image/svg+xml'}


def web_home_path(base_dir):
    built=Path(base_dir)/'static/web-dist/index.html'
    return str(built if built.is_file() else Path(base_dir)/'index.html')


def favicon_path(base_dir):
    return str(Path(base_dir)/'static/web/favicon.svg')


def accepts_gzip(value):
    accepted={}
    for item in (value or '').lower().split(','):
        parts=item.strip().split(';');name=parts[0].strip()
        if not name:continue
        quality=1.0
        for part in parts[1:]:
            if part.strip().startswith('q='):
                raw=part.strip()[2:]
                quality=float(raw) if re.fullmatch(r'(?:0(?:\.\d{0,3})?|1(?:\.0{0,3})?)',raw) else 0.0
        accepted[name]=quality
    return accepted.get('gzip',accepted.get('*',0))>0


class WebAssets(StaticFiles):
    def __init__(self,base_dir):
        self.source=(Path(base_dir)/'static/web').resolve()
        self.built=(Path(base_dir)/'static/web-dist/web-assets').resolve()
        super().__init__(directory=str(self.source),check_dir=False)

    async def get_response(self,path,scope):
        if scope['method'] not in ('GET','HEAD'):raise HTTPException(405)
        # StaticFiles.get_path uses the host OS separator (backslash on Windows).
        path=path.replace('\\','/')
        parts=path.split('/')
        immutable=len(parts)==2 and bool(VERSION.fullmatch(parts[0]))
        if len(parts)==1 and ASSET.fullmatch(parts[0]):root=self.source;file=root/parts[0]
        elif immutable and ASSET.fullmatch(parts[1]):root=self.built;file=root/parts[0]/parts[1]
        else:raise HTTPException(404)
        if not file.resolve().is_relative_to(root) or not file.is_file():raise HTTPException(404)
        headers={'Cache-Control':'public, max-age=31536000, immutable' if immutable else 'public, max-age=0, must-revalidate',
                 'Vary':'Accept-Encoding','X-Content-Type-Options':'nosniff','X-Static-Delivery':'backend-public-assets'}
        selected=file;request=Headers(scope=scope)
        if accepts_gzip(request.get('accept-encoding')) and file.with_name(file.name+'.gz').is_file():
            selected=file.with_name(file.name+'.gz');headers['Content-Encoding']='gzip'
        if not selected.resolve().is_relative_to(root):raise HTTPException(404)
        response=FileResponse(str(selected),media_type=TYPES[file.suffix],headers=headers)
        response.set_stat_headers(selected.stat())
        if self.is_not_modified(response.headers,request):
            return Response(status_code=304,headers={k:v for k,v in response.headers.items() if k.lower() in ('etag','last-modified','cache-control','vary','content-encoding','x-static-delivery')})
        return response


def create_web_assets(base_dir):
    return WebAssets(base_dir)
