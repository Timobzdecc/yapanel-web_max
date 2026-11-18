import assert from 'node:assert/strict';
import vm from 'node:vm';
import {readFile} from 'node:fs/promises';
const source=await readFile(new URL('../static/panel-context.js',import.meta.url),'utf8');
for(const path of ['/', '/index.html','/speaker/room-a/','/speaker/room-b/index.html']){
  const calls=[],scope={window:{},document:{baseURI:'https://panel.test'+path},URL,fetch:(...args)=>calls.push(args)};
  vm.runInNewContext(source,scope);
  const context=scope.window.PanelContext,base=path.replace(/index.html$/,'');
  assert.equal(context.url('/api/status'),'https://panel.test'+base+'api/status');
  assert.equal(context.url('/static/model.js'),'https://panel.test'+base+'static/model.js');
  assert.equal(context.url('/model.stl'),'https://panel.test'+base+'model.stl');
  assert.equal(context.websocket('/api/term?cols=80'),'wss://panel.test'+base+'api/term?cols=80');
  context.fetch('/api/clock',{method:'POST'});assert.equal(calls[0][0],'https://panel.test'+base+'api/clock');
  assert.equal(context.storageKey('theme'),'theme');
  if(context.deviceId)assert.equal(context.storageKey('fs.path'),'speaker.'+context.deviceId+'.fs.path');
  else assert.equal(context.storageKey('fs.path'),'fs.path');
}
const html=await readFile(new URL('../static/index.html',import.meta.url),'utf8');
const app=await readFile(new URL('../static/app.js',import.meta.url),'utf8');
assert(!/\bfetch\(/.test((html+app).replaceAll('PanelContext.fetch(','')));
assert(!/href="\/(static|ca\.crt|manifest)/.test(html));
assert(app.includes('new WebSocket(PanelContext.websocket('));
// Firefox ignores an import map that appears after the first module script; keep it in the head.
const firstModule=html.search(/<script[^>]*\btype=["']module["']/);
const importMap=html.indexOf('type="importmap"');
assert(importMap>0 && importMap<firstModule,'import map must precede every module script');
const manifest=JSON.parse(await readFile(new URL('../static/manifest.webmanifest',import.meta.url),'utf8'));
assert.equal(manifest.scope,'./');assert.equal(manifest.start_url,'./#overview');
// Exercise real service worker handlers without a browser or any network.
const tick=()=>new Promise(resolve=>setTimeout(resolve,0));
const swSource=await readFile(new URL('../static/sw.js',import.meta.url),'utf8');
assert(swSource.includes("panel-v18:"),'service worker must use panel-v18 namespace');
assert(swSource.includes('static/app.js?v=0.3.30'),'sw.js must preload app.js v=0.3.30');
assert(swSource.includes('static/extmusic.js?v=0.3.32'),'sw.js must preload extmusic.js v=0.3.32');
for(const script of [...swSource.matchAll(/static\/[a-z0-9_.-]+\.js\?v=[^'"]+/g)].map(m=>m[0])){
  assert(html.includes(script),'index.html and sw.js script version must match: '+script);
}
for(const path of ['/','/speaker/room-a/','/speaker/room-b/']){
  const handlers={},puts=[],deleted=[],fetchCalls=[];
  const base='https://panel.test'+path;
  const sw={
    URL,
    self:{location:{href:base+'sw.js'},addEventListener:(name,fn)=>handlers[name]=fn,clients:{claim:async()=>{}}},
    fetch:async (request,init)=>{
      if(request&&typeof request==='object'&&request.mode==='navigate'&&init&&Object.keys(init).length>0){
        throw new TypeError("Cannot construct a Request with a Request whose mode is 'navigate' and a non-empty RequestInit");
      }
      fetchCalls.push({request,init});
      const targetUrl=typeof request==='string'?request:request.url;
      return {ok:true,clone:()=>({url:targetUrl})};
    },
    caches:{
      open:async()=>({put:async(key,response)=>puts.push({key,response})}),
      match:async()=>null,
      keys:async()=>['panel-v17:'+new URL(path,base).pathname+':shell','panel-v18:'+new URL(path,base).pathname+':shell'],
      delete:async key=>deleted.push(key),
    },
    Response:{error:()=>({error:true})},
  };
  vm.runInNewContext(swSource,sw);
  for(const leaf of ['api/status','api/clock','ca.crt','sw.js','static/sw.js'])handlers.fetch({request:{method:'GET',url:base+leaf},respondWith:()=>assert.fail('must not cache '+leaf)});
  // Навигация на саму панель кладётся под ключ оболочки и всегда обходит HTTP-кэш (строковый URL + cache: 'no-store').
  const panel={method:'GET',url:base,mode:'navigate'};let response;
  handlers.fetch({request:panel,respondWith:p=>response=p});const res=await response;await tick();
  assert(!res.error,'panel navigation fetch must not throw Fetch Standard TypeError');
  assert.equal(puts.at(-1).key,base,'panel navigation caches the shell');
  assert.equal(fetchCalls.at(-1).request,base,'panel navigation fetch must pass string URL, not navigate Request');
  assert.equal(fetchCalls.at(-1).init?.cache,'no-store','panel navigation fetch must bypass HTTP cache');
  assert.equal(fetchCalls.at(-1).init?.credentials,'same-origin','panel navigation fetch must pass same-origin credentials');
  assert.equal(Object.keys(fetchCalls.at(-1).init||{}).length,2);
  const other={method:'GET',url:base+'other.html',mode:'navigate'};
  handlers.fetch({request:other,respondWith:p=>response=p});await response;await tick();
  assert.equal(puts.at(-1).key,other,'subpage navigation must not overwrite the shell');
  assert.notEqual(puts.at(-1).key,base);
  // Обновление namespace удаляет старый кэш.
  let activation;handlers.activate({waitUntil:p=>activation=p});await activation;
  assert(deleted.some(key=>key.startsWith('panel-v17:')),'old panel cache is deleted');
  assert(!deleted.some(key=>key.startsWith('panel-v18:')),'current cache is kept');
}
console.log('Page context, scoped storage, WebSocket, manifest, shell-only navigation cache and cache version cases passed');
