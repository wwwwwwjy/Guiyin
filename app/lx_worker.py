"""Short-lived, credential-free QuickJS process with a bounded HTTP broker.

JavaScript receives no Python objects/callbacks, filesystem or process APIs.
Network requests are pinned to validated public addresses, including redirects.
"""
import concurrent.futures
import http.client
import ipaddress
import json
import os
from pathlib import Path
import socket
import ssl
import sys
import time
from urllib.parse import urlencode, urljoin, urlsplit

MAX_BODY = 1024 * 1024


def destination(url):
    if not isinstance(url, str) or len(url)>8192 or any(ord(c)<32 for c in url):
        raise ValueError('音源请求地址无效')
    p=urlsplit(url)
    if p.scheme not in ('http','https') or not p.hostname or p.username or p.password:
        raise ValueError('仅支持公网 HTTP(S) 请求')
    port=p.port or (443 if p.scheme=='https' else 80)
    if port not in (80,443):raise ValueError('音源请求仅允许 80/443 端口')
    addresses=socket.getaddrinfo(p.hostname,port,type=socket.SOCK_STREAM)
    if not addresses or any(not ipaddress.ip_address(a[4][0]).is_global for a in addresses):
        raise ValueError('音源不能访问本机、NAS 或内网地址')
    return p,port,addresses[0][4][0]


def request(item):
    import certifi
    opts=item.get('options') or {}
    if not isinstance(opts,dict):raise ValueError('请求选项无效')
    method=str(opts.get('method','GET')).upper()
    if method not in ('GET','POST','HEAD'):raise ValueError('不支持的请求方法')
    headers=opts.get('headers') or {}
    if not isinstance(headers,dict) or len(json.dumps(headers))>16384:raise ValueError('请求头无效')
    forbidden={'host','content-length','transfer-encoding','connection','proxy-authorization','accept-encoding'}
    headers={str(k):str(v) for k,v in headers.items() if str(k).lower() not in forbidden}
    if any('\r' in k+v or '\n' in k+v for k,v in headers.items()):raise ValueError('请求头无效')
    headers['Accept-Encoding']='identity'
    headers.setdefault('User-Agent','Guiyin-LX/0.9.14')
    body=opts.get('body')
    if opts.get('form') is not None:
        body=urlencode(opts['form']);headers.setdefault('Content-Type','application/x-www-form-urlencoded')
    elif isinstance(body,(dict,list)):
        body=json.dumps(body,ensure_ascii=False);headers.setdefault('Content-Type','application/json')
    if body is not None:
        if not isinstance(body,str):raise ValueError('请求内容无效')
        body=body.encode()
        if len(body)>262144:raise ValueError('请求内容超过 256 KB')
    timeout=max(1,min(12,float(opts.get('timeout') or 12000)/1000))
    url=item['url']
    for redirect in range(4):
        p,port,address=destination(url)
        conn=http.client.HTTPConnection(p.hostname,port,timeout=timeout)
        sock=socket.create_connection((address,port),timeout=timeout)
        try:
            if p.scheme=='https':
                sock=ssl.create_default_context(cafile=certifi.where()).wrap_socket(sock,server_hostname=p.hostname)
            conn.sock=sock
            conn.request(method,(p.path or '/')+('?' + p.query if p.query else ''),body,headers)
            response=conn.getresponse()
            if response.status in (301,302,303,307,308) and response.getheader('Location'):
                nxt=urljoin(url,response.getheader('Location'))
                if urlsplit(nxt).netloc!=p.netloc:
                    headers={'Accept-Encoding':'identity','User-Agent':'Guiyin-LX/0.9.14'}
                    body=None;method='GET' if method!='HEAD' else 'HEAD'
                elif response.status==303:body=None;method='GET'
                url=nxt;continue
            data=response.read(MAX_BODY+1)
            if len(data)>MAX_BODY:raise ValueError('音源响应超过 1 MB')
            encoding=response.getheader('Content-Encoding','').lower()
            if encoding in ('gzip','deflate'):
                import zlib
                decoder=zlib.decompressobj(31 if encoding=='gzip' else 15)
                data=decoder.decompress(data,MAX_BODY+1)
                if len(data)>MAX_BODY or decoder.unconsumed_tail:raise ValueError('解压响应超过 1 MB')
            elif encoding and encoding!='identity':raise ValueError('不支持的响应压缩格式')
            text=data.decode('utf-8',errors='replace')
            try:value=json.loads(text)
            except ValueError:value=text
            return {'statusCode':response.status,'status':response.status,'headers':dict((k.lower(),v) for k,v in response.getheaders()),'body':value,'url':url}
        finally:
            conn.close();sock.close()
    raise ValueError('音源重定向次数过多')


def run(payload, transport=request):
    import quickjs
    context=quickjs.Context()
    context.set_memory_limit(48*1024*1024)
    context.set_max_stack_size(1024*1024)
    context.set_time_limit(0.5)
    context.eval((Path(__file__).with_name('lx_runtime.js')).read_text())
    context.eval(payload['code'])
    def jobs():
        for _ in range(256):
            if not context.execute_pending_job():return
        raise ValueError('音源异步任务数量超限')
    def state():return json.loads(context.eval('__lxBridge.state()'))
    jobs();s=state()
    if not isinstance(s.get('init'),dict) or not s.get('registered'):
        raise ValueError('未识别到 LX 音源接口；需要 lx.on(request) 和 lx.send(inited)')
    if payload['operation']=='inspect':return s['init']
    method='assets' if payload['operation']=='assets' else 'start'
    context.eval('__lxBridge.'+method+'('+json.dumps(payload['request'])+')');jobs()
    deadline=time.monotonic()+40
    executor=concurrent.futures.ThreadPoolExecutor(max_workers=4)
    pending={};count=0;received=0
    try:
        while time.monotonic()<deadline:
            s=state()
            if s.get('done'):
                if s.get('error'):raise ValueError(s['error'])
                return s.get('result')
            raw=context.eval('__lxBridge.requests()')
            if len(raw)>1024*1024:raise ValueError('音源请求队列过大')
            for item in json.loads(raw):
                count+=1
                if count>40:raise ValueError('单次音源调用超过 40 次请求')
                pending[executor.submit(transport,item)]=item['id']
            for future in list(pending):
                if not future.done():continue
                identity=pending.pop(future)
                try:
                    response=future.result();error=None
                    received+=len(json.dumps(response))
                except Exception as exc:
                    response=None;error=str(exc)[:300]
                if received>8*MAX_BODY:raise ValueError('音源总响应超过 8 MB')
                context.eval('__lxBridge.reply('+json.dumps(identity)+','+json.dumps(error)+','+json.dumps(response)+')')
                jobs()
            context.eval('__lxBridge.tick()');jobs()
            time.sleep(0.02)
        raise ValueError('JS 音源解析超时，已切换后续音源')
    finally:
        executor.shutdown(wait=False,cancel_futures=True)


def main():
    import resource
    import quickjs, certifi  # Load trusted dependencies before dropping privileges.
    resource.setrlimit(resource.RLIMIT_CPU,(8,8))
    resource.setrlimit(resource.RLIMIT_CORE,(0,0))
    resource.setrlimit(resource.RLIMIT_NOFILE,(64,64))
    if sys.platform=='linux':
        resource.setrlimit(resource.RLIMIT_AS,(384*1024*1024,384*1024*1024))
        if os.getuid()==0:
            os.setgroups([]);os.setgid(65534);os.setuid(65534)
    try:
        raw=sys.stdin.buffer.readline(600000)
        if len(raw)>=600000:raise ValueError('音源输入过大')
        payload=json.loads(raw)
        result={'ok':True,'value':run(payload)}
    except Exception as exc:
        result={'ok':False,'error':str(exc).splitlines()[0][:300]}
    output=json.dumps(result,ensure_ascii=False)
    if len(output.encode())>262144:output=json.dumps({'ok':False,'error':'音源结果过大'})
    sys.stdout.write(output);sys.stdout.flush()
    os._exit(0)  # Abandon outstanding network threads; the invocation is finished.


if __name__=='__main__':main()
