const fs=require('fs'),vm=require('vm'),assert=require('assert');
const code=fs.readFileSync('app/static/studio.js','utf8');let click,calls=[];const output={innerHTML:''};
const ctx={Set,Math,Number,Object,JSON,setInterval:()=>{},document:{addEventListener:(n,f)=>{if(n==='click')click=f}},$:()=>output,
 names:{qq:'QQ',netease:'网易云'},when:()=>'',num:String,esc:v=>String(v??'').replaceAll('<','&lt;'),toast:()=>{},
 api:async(path,method,body)=>{calls.push([path,method,body]);return {directories:[{id:'x',path:'/music/<likes>',enabled:false,total:2,processed:1,scanned:2,completed:1,unmatched:0}],rows:[],total:0,history:[{id:'change',path:'/music/<song>',before_tags:'{"album":["bad"]}',after_tags:'{"album":["good"]}',proof:'{"album":["qq","netease"]}',created:1,restored:0}]}}};
vm.createContext(ctx);vm.runInContext(code.slice(code.indexOf('let metadataKey=')),ctx);
(async()=>{await ctx.loadMetadata();assert(output.innerHTML.includes('&lt;likes>'));assert(output.innerHTML.includes('开始检查并修复'));assert(output.innerHTML.includes('bad → good'));assert(output.innerHTML.includes('撤销修改'));
const b={dataset:{meta:'control',id:'x',control:'resume'},disabled:false};await click({target:{closest:()=>b}});assert(calls.some(c=>c[0]==='/api/hub/metadata-directories/x'&&c[2].action==='resume'));assert(!b.disabled);console.log('Metadata UI passed');})().catch(e=>{console.error(e);process.exitCode=1});
