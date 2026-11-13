const fs=require('fs'),vm=require('vm'),assert=require('assert');
const code=fs.readFileSync('app/static/studio.js','utf8');
let requests=[];const feedback={};
const context={api:async(path,method,body)=>{requests.push({path,body});return {added:1};},loadSources:async()=>{},$:()=>feedback,toast:()=>{}};
vm.createContext(context);
vm.runInContext(code.slice(code.indexOf('async function importSourceFiles('),code.indexOf("$('#source-file').onchange=")),context);
const file=(name,text,size=20)=>({name,size,text:async()=>text});
(async()=>{
 const result=await context.importSourceFiles([file('source.JS','\uFEFFlx.on()\r\n'),file('bad.json','{'),file('sources.json','{"version":1,"sources":[]}'),file('oversize.js','',262145)]);
 assert.equal(requests.length,2);
 assert.equal(requests[0].path,'/api/hub/sources/import-js');assert.equal(requests[0].body.code,'lx.on()\r\n');
 assert.equal(requests[1].path,'/api/hub/sources/import');
 assert(result[1].includes('导入失败'));assert(result[3].includes('256 KB'));
 assert(feedback.textContent.includes('source.JS'));assert(feedback.textContent.includes('sources.json'));
 context.api=async()=>({added:0,repaired:true});
 const repaired=await context.importSourceFiles([file('source.js','lx.on()')]);
 assert(repaired[0].includes('文件已修复'));
 console.log('Source import checks passed: JS/JSON routing, BOM, batch partial failures, size limit.');
})().catch(e=>{console.error(e);process.exitCode=1});
