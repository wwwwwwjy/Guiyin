const fs=require('fs'),vm=require('vm'),assert=require('assert');
const src=fs.readFileSync('app/static/studio.js','utf8');
let submit, requests=[], closed=0,loaded=0;
const status={textContent:''};
const ctx={document:{addEventListener:(n,fn)=>{if(n==='submit')submit=fn}},FormData:class{constructor(f){return Object.entries(f.values)}},api:async(...args)=>{requests.push(args);return {ok:true,message:'连接正常'}},closeModal:()=>closed++,loadSources:async()=>loaded++,toast:()=>{},$:()=>status};
vm.createContext(ctx);vm.runInContext(src.slice(src.indexOf("document.addEventListener('submit'"),src.indexOf("document.addEventListener('click'",src.indexOf("document.addEventListener('submit'"))),ctx);
function form(type,values){return {id:{value:values.id},getAttribute:()=>type,values,querySelector:()=>null,querySelectorAll:()=>[{value:'qq'}],elements:{enabled:{checked:true}}};}
(async()=>{
 const button={disabled:false,textContent:'保存'};
 await submit({target:form('source-form',{id:'',name:'test',url:'https://example.com',token:''}),submitter:button,preventDefault(){}});
 assert.equal(requests[0][0],'/api/hub/sources');assert.equal(requests[0][1],'PUT');assert.equal(closed,1);assert.equal(loaded,1);assert.equal(button.disabled,false);
 requests=[];
 await submit({target:form('source-test-form',{id:'builtin-qq',platform:'qq',song_id:''}),submitter:button,preventDefault(){}});
 assert.equal(requests[0][0],'/api/hub/sources/test');assert.match(status.textContent,/测试通过/);
 console.log('Source form checks passed: named id shadow, save dispatch, test dispatch, feedback.');
})().catch(e=>{console.error(e);process.exitCode=1});
