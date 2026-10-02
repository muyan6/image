"""Insert one managed include into an existing domain HTTPS server, test, rollback on failure."""
from pathlib import Path
import argparse,os,re,subprocess,time


def uncomment(text):
    """Mask comments without changing offsets or quoted # characters."""
    out=[];quote=None;escape=False;comment=False
    for char in text:
        if comment:
            if char=='\n':comment=False;out.append(char)
            else:out.append(' ')
            continue
        out.append(char)
        if escape:escape=False;continue
        if quote:
            if char=='\\':escape=True
            elif char==quote:quote=None
        elif char in ('"',"'"):quote=char
        elif char=='#':comment=True;out[-1]=' '
    return ''.join(out)


def blocks(text):
    """Lex braces outside quoted strings/comments, retaining offsets."""
    text=uncomment(text)
    depth=0;quote=None;escape=False;comment=False;start=None;previous=0
    for i,char in enumerate(text):
        if comment:
            if char=='\n':comment=False
            continue
        if escape:escape=False;continue
        if quote:
            if char=='\\':escape=True
            elif char==quote:quote=None
            continue
        if char in ('"',"'"):quote=char;continue
        if char=='#':comment=True;continue
        if char=='{':
            if depth==0:
                prefix=text[previous:i]
                if re.search(r'\bserver\s*$',prefix):start=i
            depth+=1
        elif char=='}':
            depth-=1
            if depth<0:raise ValueError('Site config has mismatched braces')
            if depth==0:
                if start is not None:yield start,i;start=None
                previous=i+1
    if depth or quote:raise ValueError('Site config is incomplete')


def insert(text,domain,include):
    matches=[]
    for start,end in blocks(text):
        body=uncomment(text[start+1:end])
        names=re.findall(r'\bserver_name\s+([^;]+);',body)
        if not any(domain in [name.strip('\"\'') for name in namestr.split()] for namestr in names):continue
        if not re.search(r'\blisten\s+[^;]*\b443\b|\bssl_certificate\s+',body):continue
        matches.append((start,end))
    if len(matches)!=1:raise ValueError('Expected exactly one matching HTTPS server block')
    start,end=matches[0];body=text[start+1:end]
    marker='# photo-rescue-managed-static'
    if marker in body:
        pattern=re.compile(r'(?m)^\s*# photo-rescue-managed-static\n\s*include\s+[^;]+;\s*\n?')
        if len(pattern.findall(body))!=1:raise ValueError('Managed include is ambiguous')
        body=pattern.sub('',body)
        return text[:start+1]+body.rstrip()+f'\n    {marker}\n    include "{include}";\n'+text[end:]
    return text[:end]+f'    {marker}\n    include "{include}";\n'+text[end:]


def atomic_write(path,data):
    temp=path.with_name(path.name+'.photo-static.tmp')
    try:
        temp.write_bytes(data);os.chmod(temp,path.stat().st_mode);os.replace(temp,path)
    finally:
        if temp.exists():temp.unlink()


def main():
    p=argparse.ArgumentParser();p.add_argument('--site',type=Path,required=True);p.add_argument('--include',type=Path,required=True);p.add_argument('--domain',required=True);p.add_argument('--nginx',default='nginx');p.add_argument('--dry-run',action='store_true');a=p.parse_args()
    site=a.site.resolve(strict=True);include=a.include.resolve(strict=True)
    if not re.fullmatch(r'[A-Za-z0-9.-]+',a.domain):raise ValueError('Domain is invalid')
    if any(c in str(include) for c in ('"','\n','\r','$',';')):raise ValueError('Include path is invalid')
    original=site.read_bytes();changed=insert(original.decode('utf-8'),a.domain,str(include).replace('\\','/')).encode('utf-8')
    if a.dry_run:print(changed.decode());return 0
    if changed==original:print('NGINX_STATIC_INCLUDE_ALREADY_PRESENT');return 0
    backup=site.with_name(site.name+'.photo-static-backup-'+str(time.time_ns()));backup.write_bytes(original)
    atomic_write(site,changed)
    try:
        test=subprocess.run([a.nginx,'-t'],capture_output=True,text=True)
        if test.returncode:raise RuntimeError(test.stdout+test.stderr)
        reload=subprocess.run([a.nginx,'-s','reload'],capture_output=True,text=True)
        if reload.returncode:raise RuntimeError(reload.stdout+reload.stderr)
    except Exception:
        atomic_write(site,original);print('NGINX_CONFIG_RESTORED backup='+str(backup));raise
    print('NGINX_STATIC_ENABLED site='+str(site)+' backup='+str(backup));return 0


if __name__=='__main__':raise SystemExit(main())
