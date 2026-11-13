const fs=require('fs'),vm=require('vm'),assert=require('assert');
const src=fs.readFileSync('app/static/studio.js','utf8');
let calls=[],submit,closed=0;
const list={innerHTML:''},live={textContent:''},metrics={innerHTML:''};
const ctx={Set,Number,Object,Math,document:{addEventListener:(n,f)=>{if(n==='submit')submit=f}},
 setInterval:()=>{},$:s=>s==='#comment-directory-list'?list:s==='#comment-metrics'?metrics:live,
 metric:(name,value,detail)=>name+':'+value+' '+detail,num:x=>String(x||0),esc:x=>String(x||'').replaceAll('<','&lt;'),names:{qq:'QQ音乐'},
 api:async(path,...args)=>{calls.push([path,...args]);return path.endsWith('progress')?{run_progress:{done:1,total:2,rows_new:22,elapsed_s:8,eta_s:12}}:{directories:[{id:'a',path:'/music/<unsafe>',enabled:1,total:2,done:0,pending:0,running:2,failed:0,completed_inflight:1,progress:{song_name:'<song>',done:1,total:2,rows_new:22,track_results:{outside:{status:'done'},inside:{status:'done'}}}}]};},
 FormData:class{constructor(f){return Object.entries(f.values)}},closeModal:()=>closed++,toast:()=>{}};
vm.createContext(ctx);
vm.runInContext(src.slice(src.indexOf('let commentSelectedDirs=')),ctx);
vm.runInContext(src.slice(src.indexOf("document.addEventListener('submit'"),src.indexOf("document.addEventListener('click'",src.indexOf("document.addEventListener('submit'"))),ctx);
(async()=>{
 await ctx.loadCommentDirectories();
 assert(list.innerHTML.includes('已完成 1 / 2'));assert(list.innerHTML.includes('50%'));
 assert(list.innerHTML.includes('&lt;unsafe>'));assert(!list.innerHTML.includes('<song>'));assert(live.textContent.includes('22'));
 assert(list.innerHTML.includes('data-control="pause"'));
 assert(list.innerHTML.includes('处理中 1'));assert(metrics.innerHTML.includes('本轮进度:1 / 2'));assert(live.textContent.includes('预计剩余 0 秒'));
 vm.runInContext("commentSelectedDirs=new Set(['/music/A','/music/B'])",ctx);
 const button={disabled:false,textContent:'添加'};
 await submit({target:{getAttribute:()=> 'comment-directories-form',values:{},querySelector:()=>null},submitter:button,preventDefault(){}});
 assert.deepEqual(calls.filter(x=>x[1]==='POST').map(x=>x[2].path),['/music/A','/music/B']);assert.equal(closed,1);assert.equal(button.disabled,false);
 console.log('Comment directories UI passed: multi-select submission, scoped progress, live counts and escaping.');
})().catch(e=>{console.error(e);process.exitCode=1});
