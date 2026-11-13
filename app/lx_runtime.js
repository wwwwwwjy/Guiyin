// LX bridge: values cross the process boundary as JSON only. No host callbacks.
(() => {
  const callbacks = new Map(), timers = new Map(), handlers = new Map();
  let serial = 0, pending = [], init = null, done = false, result, error;
  const utf8 = s => Uint8Array.from(unescape(encodeURIComponent(String(s))), c => c.charCodeAt(0));
  const alphabet = 'ABCDEFGHIJKLMNOPQRSTUVWXYZabcdefghijklmnopqrstuvwxyz0123456789+/';
  const base64 = bytes => {
    let out = '';
    for (let i=0;i<bytes.length;i+=3) {
      const n=(bytes[i]<<16)|((bytes[i+1]||0)<<8)|(bytes[i+2]||0);
      out+=alphabet[n>>>18]+alphabet[(n>>>12)&63]+(i+1<bytes.length?alphabet[(n>>>6)&63]:'=')+(i+2<bytes.length?alphabet[n&63]:'=');
    }
    return out;
  };
  const decode64 = s => {
    let bits=0,value=0,out=[];
    for(const c of String(s).replace(/\s/g,'')) {const n=alphabet.indexOf(c);if(n<0)continue;value=(value<<6)|n;bits+=6;if(bits>=8){bits-=8;out.push((value>>>bits)&255);}}
    return Uint8Array.from(out);
  };
  const buffer = {
    from(value, encoding='utf8') {return typeof value==='string' ? (encoding==='base64'?decode64(value):utf8(value)) : Uint8Array.from(value);},
    bufToString(value, encoding='utf8') {return encoding==='base64'?base64(value):decodeURIComponent(escape(Array.from(value,b=>String.fromCharCode(b)).join('')));}
  };
  globalThis.btoa = s => base64(Uint8Array.from(s,c=>c.charCodeAt(0)));
  globalThis.atob = s => Array.from(decode64(s),b=>String.fromCharCode(b)).join('');
  globalThis.Buffer = {from(v,e){const b=buffer.from(v,e);b.toString=enc=>buffer.bufToString(b,enc);return b;},isBuffer:v=>v instanceof Uint8Array};
  globalThis.TextEncoder = class {encode(s){return utf8(s);}};
  globalThis.TextDecoder = class {decode(b){return buffer.bufToString(b);}};
  globalThis.URLSearchParams = class {
    constructor(value={}){this.pairs=typeof value==='string'?value.replace(/^\?/,'').split('&').filter(Boolean).map(v=>v.split('=').map(x=>decodeURIComponent(x.replace(/\+/g,' ')))):Array.isArray(value)?value.map(v=>v.slice()):Object.entries(value);}
    append(k,v){this.pairs.push([String(k),String(v)]);}
    set(k,v){this.delete(k);this.append(k,v);}
    get(k){return this.pairs.find(p=>p[0]===k)?.[1]??null;}
    delete(k){this.pairs=this.pairs.filter(p=>p[0]!==k);}
    toString(){return this.pairs.map(p=>p.map(x=>encodeURIComponent(x).replace(/%20/g,'+')).join('=')).join('&');}
  };
  globalThis.console = Object.freeze({log(){},warn(){},error(){},info(){},debug(){}});
  globalThis.setTimeout = (fn,delay=0,...args) => {if(timers.size>=128)throw Error('定时器数量超限');const id=++serial;timers.set(id,{fn,args,at:Date.now()+Math.max(1,Number(delay)||1)});return id;};
  globalThis.clearTimeout = id => timers.delete(id);
  globalThis.setInterval = () => 0; // No background service survives a source invocation.
  globalThis.clearInterval = () => {};
  if(!Promise.any)Promise.any = iterable => new Promise((resolve,reject)=>{const list=Array.from(iterable),errors=[];let left=list.length;if(!left)return reject(Error('没有可用音源'));list.forEach((p,i)=>Promise.resolve(p).then(resolve,e=>{errors[i]=e;if(!--left)reject(Error('所有请求失败'));}));});
  const events=Object.freeze({request:'request',inited:'inited',updateAlert:'updateAlert'});
  globalThis.lx = Object.freeze({
    EVENT_NAMES:events,env:'desktop',version:'2.12.0',currentScriptInfo:Object.freeze({name:'归音导入音源',version:'1'}),utils:Object.freeze({buffer}),
    on(event,fn){if(typeof fn!=='function')throw Error('事件处理器无效');handlers.set(event,fn);},
    send(event,value){if(event===events.inited)init=value;return Promise.resolve();},
    request(url,options,callback){
      if(typeof options==='function'){callback=options;options={};}
      if(typeof callback!=='function')throw Error('request 缺少回调');
      if(callbacks.size>=64)throw Error('请求数量超限');
      const id=++serial;callbacks.set(id,callback);pending.push({id,url:String(url),options:options||{}});
      return ()=>callbacks.delete(id);
    }
  });
  globalThis.__lxBridge = Object.freeze({
    state(){return JSON.stringify({init,registered:handlers.has(events.request),done,result,error});},
    requests(){const out=pending;pending=[];return JSON.stringify(out);},
    reply(id,err,response){const cb=callbacks.get(id);callbacks.delete(id);if(cb)cb(err?Error(err):null,response,response?.body);},
    tick(){const now=Date.now();for(const [id,t] of timers){if(t.at<=now){timers.delete(id);try{t.fn(...t.args);}catch(_){}}}},
    assets(payload){
      const fn=handlers.get(events.request);
      Promise.resolve().then(async()=>{
        const actions=init?.sources?.[payload.source]?.actions||[];
        const out={};
        if(!(payload.wantLyric&&actions.includes('lyric'))&&!(payload.wantCover&&actions.includes('pic')))return out;
        const ask=action=>fn({...payload,action});
        // Some scripts populate lyric/picture caches only during musicUrl.
        // Resolve metadata only; never fetch the returned audio file.
        if(actions.includes('musicUrl')){try{await ask('musicUrl');}catch(_){} }
        if(payload.wantLyric&&actions.includes('lyric')){try{const v=await ask('lyric');out.lyric=typeof v==='string'?v:v?.lyric;}catch(_){}}
        if(payload.wantCover&&actions.includes('pic')){try{const v=await ask('pic');out.cover_url=typeof v==='string'?v:(v?.url||v?.pic||v?.cover_url);}catch(_){}}
        return out;
      }).then(v=>{result=v;done=true;},e=>{error=String(e?.message||e).slice(0,300);done=true;});
    },
    start(payload){const fn=handlers.get(events.request);if(!fn)throw Error('未注册 musicUrl 处理器');Promise.resolve().then(()=>fn(payload)).then(v=>{result=v;done=true;},e=>{error=String(e?.message||e).slice(0,300);done=true;});}
  });
})();
