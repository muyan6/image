"""Deterministic, public-only content-versioned assets and precompressed delivery."""
from pathlib import Path
import argparse,gzip,hashlib,json,os,re,shutil,tempfile,time

BASE=Path(__file__).resolve().parents[1]
ROOT=BASE.parent
SOURCE=BASE/'static/web'
PUBLIC_ASSET=re.compile(r'^[A-Za-z0-9][A-Za-z0-9_.-]*\.(js|css|svg)$')
NGINX_LOCATIONS=r'''# Include inside the EXISTING HTTPS server block; certificates/proxy settings stay unchanged.
# API/account/admin requests are never served from this public-only directory.
location ^~ /web-assets/ {
    alias "__PUBLIC_ROOT__/web-assets/";
    if ($uri !~ "^/web-assets/([0-9a-f]{16}/)?[A-Za-z0-9][A-Za-z0-9_.-]*\.(js|css|svg)$") { return 404; }
    autoindex off;
    types { text/javascript js; text/css css; image/svg+xml svg; }
    default_type application/octet-stream;
    etag on;
    set $photo_static_cache "public, max-age=0, must-revalidate";
    if ($uri ~ "^/web-assets/[0-9a-f]{16}/") { set $photo_static_cache "public, max-age=31536000, immutable"; }
    add_header Cache-Control $photo_static_cache always;
    add_header Vary "Accept-Encoding" always;
    add_header X-Content-Type-Options nosniff always;
    add_header X-Static-Delivery nginx always;
    __GZIP_STATIC__
    gzip on;
    gzip_comp_level 3;
    gzip_vary on;
    gzip_types text/javascript text/css image/svg+xml;
    limit_except GET { deny all; }
}
location = /favicon.svg {
    alias "__PUBLIC_ROOT__/favicon.svg";
    default_type image/svg+xml;
    add_header Cache-Control "public, max-age=86400" always;
    add_header X-Content-Type-Options nosniff always;
    add_header X-Static-Delivery nginx always;
    limit_except GET { deny all; }
}
# Root-path WeChat verification query/POST must still reach the existing backend.
location = / {
    root "__PUBLIC_ROOT__";
    default_type text/html;
    error_page 418 = @photo_rescue_root_api;
    if ($request_method != GET) { return 418; }
    if ($arg_echostr != "") { return 418; }
    try_files /index.html =404;
    add_header Cache-Control "no-cache, must-revalidate" always;
    add_header X-Static-Delivery nginx always;
    __GZIP_STATIC__
}
location @photo_rescue_root_api {
    proxy_pass http://127.0.0.1:__PORT__;
    proxy_set_header Host $host;
    proxy_set_header X-Real-IP $remote_addr;
    proxy_set_header X-Forwarded-For $proxy_add_x_forwarded_for;
    proxy_set_header X-Forwarded-Proto $scheme;
    proxy_cache off;
}
'''


def atomic_write(file,data):
    file.parent.mkdir(parents=True,exist_ok=True)
    temp=file.with_name(file.name+'.tmp-'+str(os.getpid()))
    try:
        temp.write_bytes(data);os.replace(temp,file)
    finally:
        if temp.exists():temp.unlink()


def public_path(value):
    if any(c in value for c in ('"','\n','\r','\0','$',';','{','}')):raise ValueError('Public root path contains invalid config characters')
    return value.replace('\\','/').rstrip('/')


def build(output,source=SOURCE,index=None):
    output=Path(output).resolve();source=Path(source).resolve();index=Path(index or BASE/'index.html')
    files=sorted(p for p in source.iterdir() if p.is_file() and PUBLIC_ASSET.fullmatch(p.name) and p.resolve().is_relative_to(source))
    if not files or not (source/'boot.js').is_file():raise ValueError('Public web source is incomplete')
    digest=hashlib.sha256()
    for file in files:digest.update(file.name.encode()+b'\0'+file.read_bytes()+b'\0')
    version=digest.hexdigest()[:16];release=output/'web-assets'/version;release.mkdir(parents=True,exist_ok=True)
    manifest=[]
    for file in files:
        data=file.read_bytes();compressed=gzip.compress(data,compresslevel=9,mtime=0)
        target=release/file.name
        if target.exists() and target.read_bytes()!=data:raise ValueError('Immutable release collision')
        atomic_write(target,data);atomic_write(target.with_name(file.name+'.gz'),compressed)
        # Matching mtimes permit conditional gzip delivery with both Nginx and ASGI.
        os.utime(target.with_name(file.name+'.gz'),ns=(target.stat().st_atime_ns,target.stat().st_mtime_ns))
        manifest.append({'name':file.name,'bytes':len(data),'gzip_bytes':len(compressed),'sha256':hashlib.sha256(data).hexdigest()})
    html=index.read_text(encoding='utf-8')
    html=html.replace('/web-assets/',f'/web-assets/{version}/')
    html=html.replace('href="/favicon.svg"',f'href="/web-assets/{version}/favicon.svg"')
    # Keep unversioned public URLs working for previous in-memory modules/tabs.
    for file in files:
        atomic_write(output/'web-assets'/file.name,file.read_bytes())
        atomic_write(output/'web-assets'/(file.name+'.gz'),gzip.compress(file.read_bytes(),mtime=0))
    atomic_write(output/'favicon.svg',(source/'favicon.svg').read_bytes())
    atomic_write(output/'index.html',html.encode('utf-8'));atomic_write(output/'index.html.gz',gzip.compress(html.encode('utf-8'),mtime=0))
    os.utime(output/'index.html.gz',ns=((output/'index.html').stat().st_atime_ns,(output/'index.html').stat().st_mtime_ns))
    result={'version':version,'files':manifest,'total_bytes':sum(x['bytes'] for x in manifest),'gzip_bytes':sum(x['gzip_bytes'] for x in manifest)}
    atomic_write(output/'build-manifest.json',json.dumps(result,ensure_ascii=False,indent=2).encode('utf-8'))
    return result


def nginx_config(root,port=8000,gzip_static=True):
    if not 1<=int(port)<=65535:raise ValueError('Backend port is invalid')
    return NGINX_LOCATIONS.replace('__PUBLIC_ROOT__',public_path(str(root))).replace('__PORT__',str(port)).replace('__GZIP_STATIC__','gzip_static on;' if gzip_static else '# gzip_static module unavailable; text gzip remains enabled')


def main():
    parser=argparse.ArgumentParser();parser.add_argument('--output',type=Path,default=BASE/'static/web-dist')
    parser.add_argument('--public-root');parser.add_argument('--nginx-output',type=Path);parser.add_argument('--port',type=int,default=8000);parser.add_argument('--no-gzip-static',action='store_true')
    args=parser.parse_args();result=build(args.output)
    if args.nginx_output:atomic_write(args.nginx_output,nginx_config(args.public_root or args.output.resolve(),args.port,not args.no_gzip_static).encode('utf-8'))
    print('WEB_ASSETS_BUILT version=%s files=%d raw_bytes=%d gzip_bytes=%d'%(result['version'],len(result['files']),result['total_bytes'],result['gzip_bytes']))
    return 0


if __name__=='__main__':raise SystemExit(main())
