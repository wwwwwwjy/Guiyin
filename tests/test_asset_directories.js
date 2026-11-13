const fs=require('fs'),vm=require('vm'),assert=require('assert');
const src=fs.readFileSync('app/static/studio.js','utf8');
let calls=[],submit,closed=0;const content={innerHTML:''};
const ctx={Set,Math,Number,Object,setInterval:()=>{},document:{addEventListener:(n,f)=>{if(n==='submit')submit=f}},$:()=>content,
 num:x=>String(x||0),esc:x=>String(x||'').replaceAll('<','&lt;'),
 api:async(path,...args)=>{calls.push([path,...args]);return {directories:[{id:'a',path:'/music/<A>',enabled:false,running:false,scanned:5,total:2,processed:1,completed:0,unmatched:1}],rows:[],total:0}},
 FormData:class{constructor(f){return Object.entries(f.values)}},closeModal:()=>closed++,toast:()=>{}};
vm.createContext(ctx);vm.runInContext(src.slice(src.indexOf('let assetOffset='),src.indexOf('let sourceRows=')),ctx);
vm.runInContext(src.slice(src.indexOf("document.addEventListener('submit'"),src.indexOf("document.addEventListener('click'",src.indexOf("document.addEventListener('submit'"))),ctx);
(async()=>{await ctx.loadAssets();assert(content.innerHTML.includes('&lt;A>'));assert(content.innerHTML.includes('data-control="resume"'));assert(content.innerHTML.includes('value="50"'));assert(content.innerHTML.includes('未完全匹配 1'));
vm.runInContext("assetSelectedDirs=new Set(['/music/A','/music/B'])",ctx);const button={disabled:false,textContent:'添加'};
await submit({target:{getAttribute:()=> 'asset-directories-form',values:{},querySelector:()=>null},submitter:button,preventDefault(){}});
assert.deepEqual(calls.filter(x=>x[1]==='POST').map(x=>x[2].path),['/music/A','/music/B']);assert.equal(closed,1);assert.equal(button.disabled,false);console.log('Asset directory UI passed');})().catch(e=>{console.error(e);process.exitCode=1});
