// Deterministic UI state/race tests. No browser, network, token or database needed.
import assert from 'node:assert/strict';
import fs from 'node:fs';
import vm from 'node:vm';
import {fileURLToPath} from 'node:url';
import path from 'node:path';
const root=path.resolve(path.dirname(fileURLToPath(import.meta.url)),'..');
const dataset=JSON.parse(fs.readFileSync(path.join(root,'fixtures/demo/site.json'),'utf8'));
class Node {
 constructor(tag='div'){this.tagName=tag;this.children=[];this.listeners={};this.hidden=true;this.textContent='';this.attrs={};this.dataset={};this.className='';this.classList={toggle(){}};}
 get firstChild(){return this.children[0];}
 appendChild(child){this.children.push(child);return child;}
 removeChild(child){this.children.splice(this.children.indexOf(child),1);}
 addEventListener(event,callback){this.listeners[event]=callback;}
 setAttribute(key,value){this.attrs[key]=value;}
 getAttribute(key){return this.attrs[key];}
 querySelectorAll(){return this.children;}
 closest(){return this;}
 focus(){}
 pause(){this.paused=true;}
}
function app(filename='demo-site/app.js'){
 const nodes=new Map(); const get=id=>{if(!nodes.has(id))nodes.set(id,new Node());return nodes.get(id);};
 const buttons=['schedule','homework','events','useful'].map(key=>{const n=new Node('button');n.attrs={'data-endpoint':key,'data-section':key};return n;});
 get('main-tabs').children=buttons;
 const document={getElementById:get,createElement:tag=>new Node(tag),querySelectorAll:()=>[],documentElement:{dataset:{}},addEventListener(){}};
 const pending=[];
 const sandbox={document,window:{matchMedia:()=>({matches:false})},AbortController,fetch:(url,options)=>new Promise((resolve,reject)=>pending.push({url,options,resolve,reject}))};
 vm.runInNewContext(fs.readFileSync(path.join(root,filename),'utf8'),sandbox);
 const select=key=>get('main-tabs').listeners.click({target:buttons.find(b=>b.attrs['data-section']===key)});
 return {get,pending,select};
}
const flush=async()=>{for(let i=0;i<12;i++)await Promise.resolve();};
const resolve=(request,data=dataset)=>request.resolve({ok:true,status:200,json:async()=>structuredClone(data)});
let passed=0;
async function test(name,run){await run();passed++;console.log('PASS',name);}
await test('static site uses only public JSON, without credentials',async()=>{
 const ui=app();assert.equal(ui.pending[0].url,'./data/demo.json');assert.equal(ui.pending[0].options.headers,undefined);
 resolve(ui.pending[0]);await flush();assert.equal(ui.get('cards-container').children.length,6);assert.equal(ui.get('state-error').hidden,true);
});
await test('last selected tab wins when responses arrive out of order',async()=>{
 const ui=app();ui.select('events');const [old,current]=ui.pending;assert.equal(old.options.signal.aborted,true);
 resolve(current);await flush();assert.equal(ui.get('cards-container').children.length,3);
 resolve(old);await flush();assert.equal(ui.get('cards-container').children.length,3);
 assert.match(ui.get('cards-container').children[0].children[0].children[0].textContent,/хакатон/);
});
await test('network error has retry and recovers without login',async()=>{
 const ui=app();ui.pending[0].reject(new Error('offline'));await flush();assert.equal(ui.get('state-error').hidden,false);assert.equal(ui.get('retry-button').hidden,false);
 ui.get('retry-button').listeners.click();resolve(ui.pending[1]);await flush();assert.equal(ui.get('state-error').hidden,true);
});
await test('four sections, useful category and past report are navigable',async()=>{
 const ui=app();resolve(ui.pending[0]);await flush();
 for(const [key,count] of [['homework',4],['events',3],['useful',32]]){ui.select(key);await flush();assert.equal(ui.get('cards-container').children.length,count);}
 ui.get('subfilters').children.find(b=>b.textContent==='Кабинеты').listeners.click();assert.equal(ui.get('cards-container').children.length,8);
 ui.select('events');await flush();ui.get('subfilters').children[1].listeners.click();assert.equal(ui.get('cards-container').children.length,1);
 ui.get('cards-container').children[0].listeners.click();assert.equal(ui.get('detail-report-box').hidden,false);assert.equal(ui.get('detail-media').children.length,2);
});
await test('demo signup remains local, toggles cleanly and survives section navigation',async()=>{
 const ui=app();resolve(ui.pending[0]);await flush();ui.select('events');await flush();
 ui.get('cards-container').children[0].listeners.click();ui.get('join-button').onclick();assert.match(ui.get('join-button').textContent,/Ты идёшь/);
 ui.select('homework');await flush();ui.select('events');await flush();ui.get('cards-container').children[0].listeners.click();assert.match(ui.get('join-button').textContent,/Ты идёшь/);
 ui.get('join-button').onclick();assert.match(ui.get('join-button').textContent,/Пойду/);assert.equal(ui.pending.length,1);
});
await test('empty dataset has honest empty state',async()=>{
 const ui=app();const empty=structuredClone(dataset);empty.sections.schedule.cards=[];resolve(ui.pending[0],empty);await flush();
 assert.match(ui.get('cards-container').children[0].textContent,/нет опубликованных/);
});
await test('light/dark theme can be switched with no identity storage',async()=>{
 const ui=app();ui.get('theme-toggle').listeners.click.call(ui.get('theme-toggle'));assert.match(ui.get('theme-toggle').textContent,/Светлая/);
});
await test('protected interface also ignores stale API responses',async()=>{
 const ui=app('web/app.js');ui.select('events');assert.equal(ui.pending[0].options.signal.aborted,true);
 ui.pending[1].resolve({ok:true,status:200,json:async()=>dataset.sections.events});await flush();
 ui.pending[0].resolve({ok:true,status:200,json:async()=>dataset.sections.schedule});await flush();assert.equal(ui.get('cards-container').children.length,3);
});
console.log(`Frontend: ${passed} passed, 0 skipped, 0 failed`);
