"""Owner-bound QR flows and durable, server-only platform accounts."""
import json
import os
import secrets
import threading
import time
from pathlib import Path

KINDS = {'liked':'我的喜欢','created':'我创建的歌单','favorited':'我收藏的歌单'}
NAMES = {'netease':'网易云','qq':'QQ 音乐','kugou':'酷狗'}


class Accounts:
    def __init__(self, engine):
        self.engine = engine
        self.path = engine.store.home/'hub-accounts.json'
        self.lock = threading.RLock()
        self.flows = {}

    def read(self):
        with self.lock:
            return json.loads(self.path.read_text()) if self.path.exists() else {}

    def write(self, data):
        temp = self.path.with_suffix('.tmp')
        fd = os.open(temp,os.O_CREAT|os.O_WRONLY|os.O_TRUNC,0o600)
        with os.fdopen(fd,'w') as f:
            json.dump(data,f,ensure_ascii=False)
        temp.replace(self.path)

    def recover(self):
        # A crash after writing credentials but before creating all rules is repairable.
        for platform,account in self.read().items():
            if any(not self.engine.store.rows('SELECT id FROM rules WHERE id=?',
                   ('account-'+platform+'-'+account['uid']+'-'+kind,)) for kind in KINDS):
                self.apply_options(platform,account['options'])

    def private(self, platform):
        return self.read().get(platform,{})

    def public(self):
        data = self.read()
        return [{'platform':p,'connected':bool(data.get(p,{}).get('uid')),
                 **{k:v for k,v in data.get(p,{}).items() if k in ('uid','nickname','connected_at','options')}}
                for p in NAMES]

    def bind(self, platform, uid, credential, options, nickname=''):
        if not uid or not credential:
            raise ValueError('登录确认未返回有效账号')
        with self.lock:
            data = self.read()
            old = data.get(platform,{})
            # Pause rules for a previously bound identity; do not run them with a new account.
            if old.get('uid') and old['uid']!=uid:
                with self.engine.store.connect() as db:
                    db.execute('UPDATE rules SET enabled=0 WHERE platform=? AND ref=?',(platform,'account:'+old['uid']))
                    db.execute("UPDATE jobs SET status='cancelled' WHERE kind='sync' AND status IN ('queued','running') AND json_extract(payload,'$.rule_id') IN (SELECT id FROM rules WHERE platform=? AND ref=?)",(platform,'account:'+old['uid']))
            data[platform] = {'uid':uid,'nickname':nickname or uid,'credential':credential,
                              'connected_at':time.time(),'options':options}
            self.write(data)
            self.apply_options(platform,options)

    def apply_options(self, platform, options):
        with self.lock:
            data = self.read()
            account = data.get(platform)
            if not account:
                raise ValueError('请先登录该平台')
            account['options'] = options
            self.write(data)
            for kind,label in KINDS.items():
                rid = 'account-'+platform+'-'+account['uid']+'-'+kind
                existing=self.engine.store.rows('SELECT * FROM rules WHERE id=?',(rid,))
                old=existing[0] if existing else {}
                self.engine.store.save_rule({'id':rid,'name':NAMES[platform]+' · '+label,
                    'platform':platform,'kind':kind,'ref':'account:'+account['uid'],
                    'group_name':old.get('group_name','待分类'),'interval_hours':options['interval_hours'],
                    'enabled':bool(old.get('enabled')) and kind=='liked' and kind in options['kinds'],
                    'auto_download':options['auto_download'],'quality':options['quality']})
                with self.engine.store.connect() as db:
                    db.execute("UPDATE jobs SET status='cancelled' WHERE dedupe=? AND status IN ('queued','running')",('sync:'+rid,))
            import hub_folders
            hub_folders.initialize(self.engine)
        self.engine.export()

    def disconnect(self, platform):
        with self.lock:
            active=[(fid,f['owner']) for fid,f in self.flows.items() if f['platform']==platform and f['state'] in ('pending','scanned')]
        for fid,owner in active:
            self.cancel(fid,owner)
        with self.lock:
            data = self.read()
            account = data.pop(platform,None)
            self.write(data)
            if account:
                if platform=='kugou':
                    import kugou
                    kugou.save_session(account['uid'],None)
                with self.engine.store.connect() as db:
                    db.execute('UPDATE rules SET enabled=0 WHERE platform=? AND ref=?',(platform,'account:'+account['uid']))
                    db.execute("UPDATE jobs SET status='cancelled' WHERE kind='sync' AND status IN ('queued','running') AND json_extract(payload,'$.rule_id') IN (SELECT id FROM rules WHERE platform=? AND ref=?)",(platform,'account:'+account['uid']))

    def start(self, platform, owner, options, method='mobile'):
        with self.lock:
            previous=[fid for fid,f in self.flows.items() if f['owner']==owner and f['platform']==platform and f['state'] in ('pending','scanned')]
        for fid in previous:
            self.cancel(fid,owner)
        with self.lock:
            now=time.monotonic()
            for old in self.flows.values():
                if old['expires']<=now and old.get('watcher'): old['watcher'].cancel()
            self.flows={k:v for k,v in self.flows.items() if v['expires']>now}
            if len(self.flows)>=20:
                raise ValueError('扫码会话过多，请稍后再试')
            # Reserve before issuing to cap concurrent starts.
            fid=secrets.token_urlsafe(32)
            f={'owner':owner,'platform':platform,'options':options,'expires':now+180,
               'lock':threading.Lock(),'state':'pending'}
            self.flows[fid]=f
        try:
            if platform=='qq' and method=='mobile':
                from hub_mobile_qr import MobileQR
                def result(state, credential):
                    with f['lock']:
                        if f['state'] in ('complete','cancelled','expired','failed'): return
                        if f['expires']<=time.monotonic():
                            f['state']='expired'; return
                        if state=='complete':
                            if not credential or not credential.get('musicid'):
                                f['state']='failed'; return
                            self.bind('qq',str(credential['musicid']),credential,options)
                        f['state']=state
                f['mobile']=True
                f['watcher']=MobileQR(result)
                qr=f['watcher'].image()
            elif platform=='qq':
                import hub_qq
                f['key'], qr = hub_qq.run(hub_qq.start_qr(method))
            elif platform=='netease':
                from netease_provider import QRLoginManager
                from kugou_qr import _qr_svg
                f['key']=QRLoginManager().generate_qr_key()
                if not f['key']:
                    raise ValueError('网易二维码签发失败')
                qr=_qr_svg('https://music.163.com/login?codekey='+f['key'])
            else:
                import kugou_qr
                r=kugou_qr.create(owner)
                f['key'],qr=r['flow_id'],r['qr']
            return {'flow_id':fid,'qr':qr,'expires_in':180,'state':'pending'}
        except Exception:
            if f.get('watcher'): f['watcher'].cancel()
            with self.lock:
                self.flows.pop(fid,None)
            raise

    def flow(self, fid, owner):
        with self.lock:
            f=self.flows.get(fid)
            if not f or f['owner']!=owner:
                raise ValueError('扫码会话不存在或无权访问')
            return f

    def cancel(self, fid, owner):
        f=self.flow(fid,owner)
        with f['lock']:
            if f['state']!='complete':
                f['state']='cancelled'
                if f.get('watcher'): f['watcher'].cancel()
                if f['platform']=='kugou' and f.get('key'):
                    import kugou_qr
                    kugou_qr.cancel(f['key'],owner)
                f.pop('key',None)
        return {'state':f['state']}

    def poll(self, fid, owner):
        f=self.flow(fid,owner)
        with f['lock']:
            if f['expires']<=time.monotonic() and f['state'] not in ('complete','cancelled'):
                f['state']='expired'; f.pop('key',None)
                if f.get('watcher'): f['watcher'].cancel()
            if f['state'] in ('complete','expired','cancelled','failed'):
                return {'state':f['state']}
            if f.get('mobile'):
                return {'state':f['state']}
            if time.monotonic()-f.get('last_poll',0)<2:
                return {'state':f['state']}
            f['last_poll']=time.monotonic()
            p=f['platform']; credential=None; uid=''; nickname=''
            if p=='qq':
                import hub_qq
                state,credential=hub_qq.run(hub_qq.poll_qr(f['key']))
                if credential:
                    uid=str(credential['musicid'])
            elif p=='netease':
                from netease_provider import QRLoginManager
                import requests
                code,cookies=QRLoginManager().check_qr_login(f['key'])
                state={800:'expired',801:'pending',802:'scanned',803:'complete'}.get(code,'failed')
                if state=='complete':
                    r=requests.get('https://music.163.com/api/nuser/account/get',cookies=cookies,
                        headers={'Referer':'https://music.163.com/','User-Agent':'Mozilla/5.0'},timeout=20)
                    r.raise_for_status(); profile=r.json().get('profile') or {}
                    if not profile.get('userId'):
                        raise ValueError('网易账号身份验证失败，请重新扫码')
                    uid=str(profile['userId']); nickname=profile.get('nickname','')
                    credential={'cookie':'; '.join(k+'='+v for k,v in cookies.items())}
            else:
                import kugou_qr,kugou
                r=kugou_qr.poll(f['key'],owner); state=r['state']
                if state=='complete':
                    uid=str(r['userid']); credential={'session':True}
            if state=='complete' and f['expires']<=time.monotonic():
                state='expired'
            if state=='complete':
                self.bind(p,uid,credential,f['options'],nickname)
            f['state']=state
            if state in ('complete','expired','failed','cancelled'):
                f.pop('key',None)
            return {'state':state}

    def close(self):
        with self.lock:
            flows=list(self.flows.values())
        for flow in flows:
            with flow['lock']:
                if flow['state']!='complete': flow['state']='cancelled'
                if flow.get('watcher'): flow['watcher'].cancel()
