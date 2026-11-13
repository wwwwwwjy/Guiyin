const fs=require('fs'),vm=require('vm'),assert=require('assert');
const src=fs.readFileSync('app/static/studio.js','utf8');
let calls=[],loads=0;
const selectors=[{disabled:false},{disabled:false}];
const ctx={sourceRows:[{id:'native'},{id:'west'},{id:'other'}],sourceOrderBusy:false,
 $$:()=>selectors,api:async(...args)=>calls.push(args),loadSources:async()=>loads++,toast:()=>{},
 esc:s=>String(s).replace(/</g,'&lt;'),names:{qq:'QQ 音乐'}};
vm.createContext(ctx);
vm.runInContext(src.slice(src.indexOf('async function moveSourcePriority('),src.indexOf("document.addEventListener('change',e=>{if(e.target.matches('.source-priority'))")),ctx);
vm.runInContext(src.slice(src.indexOf('function jobSourceDetails('),src.indexOf('async function loadJobs()')),ctx);
(async()=>{
 await ctx.moveSourcePriority('west','1');
 assert.equal(calls[0][0],'/api/hub/sources/order');assert.equal(calls[0][1],'PUT');
 assert.deepEqual([...calls[0][2].ids],['west','native','other']);
 assert.equal(loads,1);assert(selectors.every(el=>!el.disabled));
 await ctx.moveSourcePriority('missing','1');await ctx.moveSourcePriority('west','99');
 assert.equal(calls.length,1);
 ctx.api=async()=>{throw Error('stale order')};await ctx.moveSourcePriority('other',1);
 assert.equal(loads,2);assert.equal(ctx.sourceOrderBusy,false);
 const html=ctx.jobSourceDetails({id:'j',source_attempts:[{name:'<unsafe>',platform:'qq',status:'done',message:'validated'}]},true);
 assert(html.includes('已使用 &lt;unsafe>'));assert(!html.includes('<unsafe>'));assert(html.includes(' open>'));
 assert(ctx.jobSourceDetails({kind:'download',status:'blocked'}).includes('当前音源'));
 console.log('Source priority checks passed: move/save, stale failure recovery, status log escaping and expanded state.');
})().catch(e=>{console.error(e);process.exitCode=1});
