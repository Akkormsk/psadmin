// Lightweight DOM regression of the rendered Django script; no browser or real requests.
// Run with NODE_PATH pointing to a temporary linkedom install and a rendered fixture JSON.
const assert = require('node:assert/strict');
const fs = require('node:fs');
const vm = require('node:vm');
const {parseHTML} = require('linkedom');
const fixture = JSON.parse(fs.readFileSync(process.argv[2], 'utf8'));

function mount({state='ready', finance='current', editable=true, result=fixture.result}={}) {
  const {window} = parseHTML(`<html><body>${fixture.script}<div data-analysis-result>
    <script id="ts-product-calculation-data" type="application/json"></script>
    <div data-product-content><section data-product-calculation data-source-id="42" data-route-state="${state}">
      <div data-product-calculation-body>${fixture.route}</div></section>
      <span data-finance-view="footer" data-finance-state="${finance}"></span></div></div></body></html>`);
  const {document} = window;
  const snapshot = {estimate_id:1,editable,fields:{},save_url:'/save',lines:[{
    id:7,name:'Блокнот',quantity:100,nmck_unit:0,requirements:{source_item_id:42,production:result},
  }]};
  document.querySelector('#ts-product-calculation-data').textContent=JSON.stringify(snapshot);
  const calls=[];
  const fetch=async (url,options)=>{
    calls.push({url,options});
    return {ok:true,status:200,json:async()=>url==='/save'?{}:result};
  };
  const context=vm.createContext({window,document,fetch,Intl,FormData,Promise,Map,Set,WeakMap,WeakSet,
    CustomEvent:window.CustomEvent,queueMicrotask,console});
  const source=[...document.querySelectorAll('script')].find(node=>!node.type).textContent;
  new vm.Script(source).runInContext(context);
  const content=document.querySelector('[data-product-content]');
  const routeContext=content.querySelector('[data-product-route-context]');
  routeContext.dataset.preserved='yes';
  document.dispatchEvent(new window.CustomEvent('ps-product-selected',{detail:{content}}));
  return {document,window,calls,content,routeContext};
}

(async()=>{
  const current=mount();
  assert.equal(current.calls.length,0,'Opening a product must not calculate or save');
  assert.ok(current.content.querySelector('[data-product-route-context]')===current.routeContext,
    'Saved route context must remain after opening the calculation');
  const steps=current.content.querySelectorAll('details[data-step-id]');
  assert.equal(steps.length,3);
  assert.match(steps[0].querySelector('summary').textContent,/0,00/,'Saved zero is real');
  assert.match(steps[1].querySelector('summary').textContent,/1[\s\u00a0]250,00/);
  assert.match(steps[2].querySelector('summary').textContent,/—/,'Missing price remains unknown');
  assert.equal(steps[1].querySelector('[data-route-executor]').textContent,'Печатник <тест>');
  assert.equal(steps[1].querySelector('[data-route-price-source]').textContent,'Внутренний калькулятор');
  assert.equal(steps[0].querySelector('[data-route-executor]').textContent,'Поставщик <тест>');
  assert.equal(steps[2].querySelector('[data-route-executor]').textContent,'Не выбран');
  steps[1].setAttribute('open','');
  await steps[1].querySelector('[data-price-production]').onclick();
  assert.equal(current.calls.length,2,'Only explicit calculation and its existing save send requests');
  assert.equal(current.content.querySelector('[data-step-id="print"]').open,true);
  assert.ok(current.content.querySelector('[data-product-route-context]')===current.routeContext,
    'Saved route context must remain after an explicit calculation');
  const footer=current.content.querySelector('[data-finance-view]');
  footer.dataset.financeState='stale';
  current.document.dispatchEvent(new current.window.CustomEvent('ps-card-snapshot'));
  await Promise.resolve();
  assert.match(current.content.querySelector('[data-route-price-label]').textContent,/проверить/);

  const stale=mount({state:'stale',finance:'stale',editable:false});
  assert.equal(stale.calls.length,0);
  assert.match(stale.content.querySelector('[data-route-price-label]').textContent,/проверить/);
  assert.equal(stale.content.querySelector('[data-price-production]').disabled,true);
  assert.ok(stale.content.querySelector('[data-product-route-context]')===stale.routeContext,
    'Saved route context must remain in read-only mode');

  const old=mount({editable:false,result:{stage:'training_dialogue',route:{steps:['Печать','Упаковка']}}});
  assert.equal(old.content.querySelectorAll('details[data-step-id]').length,2);
  assert.equal(old.content.querySelectorAll('[data-route-executor]').length,2);
  assert.equal(old.calls.length,0);
  assert.doesNotMatch(old.content.querySelector('[data-product-calculation-body]').textContent,/0,00/);
  console.log('PASS: route state, saved prices, unknown values, metadata, controls, collapse state, readonly and legacy steps');
})().catch(error=>{console.error(error);process.exitCode=1;});
