/* Move existing content; changing selection never starts analysis or calculation. */
(() => {
  const states = new WeakMap(), selections = new Map();
  function init(workspace) {
    if (states.has(workspace)) return states.get(workspace);
    const entries = new Map([...workspace.querySelectorAll('[data-product-key]')].map(entry => [entry.dataset.productKey, entry]));
    const state = {entries, current:null, inspector:workspace.querySelector('[data-product-inspector]'), tender:workspace.closest('[data-analysis-result]').dataset.analysisResult};
    states.set(workspace, state);
    const saved = selections.get(state.tender);
    if (entries.has(saved)) select(workspace, saved, false);
    return state;
  }
  function select(workspace, key, focus = true) {
    const state = init(workspace), entry = state.entries.get(key);
    if (state.current === entry) return;
    if (state.current) {
      const content = state.inspector.querySelector('[data-product-content]');
      content.hidden = true;
      state.current.append(content);
      state.current.querySelector('[data-product-select]').setAttribute('aria-expanded', 'false');
    }
    state.current = entry || null;
    workspace.classList.toggle('has-selection', Boolean(entry));
    state.inspector.hidden = !entry;
    if (!entry) { selections.delete(state.tender); return; }
    selections.set(state.tender, key);
    const content = entry.querySelector('[data-product-content]');
    content.hidden = false;
    state.inspector.append(content);
    document.dispatchEvent(new CustomEvent('ps-product-selected', {detail:{content}}));
    entry.querySelector('[data-product-select]').setAttribute('aria-expanded', 'true');
    if (focus) content.querySelector('[data-product-close]').focus({preventScroll:true});
  }
  document.addEventListener('click', event => {
    const control = event.target.closest('[data-product-select],[data-product-target],[data-product-close]');
    if (!control) return;
    const workspace = control.closest('[data-products-workspace]');
    if (!workspace) return;
    const state = init(workspace);
    if (control.hasAttribute('data-product-close')) {
      const opener = state.current?.querySelector('[data-product-select]');
      select(workspace, null); opener?.focus({preventScroll:true});
    } else select(workspace, control.dataset.productTarget || control.closest('[data-product-key]').dataset.productKey);
  });
  document.addEventListener('keydown', event => {
    if (event.key !== 'Escape' || event.defaultPrevented) return;
    const workspace = event.target.closest('[data-products-workspace]');
    if (!workspace || !init(workspace).current) return;
    event.preventDefault();
    init(workspace).inspector.querySelector('[data-product-close]')?.click();
  });
  function revealAnchor() {
    const target = document.getElementById(location.hash.slice(1));
    const content = target?.closest('[data-product-content]');
    const workspace = target?.closest('[data-products-workspace]');
    if (!workspace) return;
    const state = init(workspace);
    const entry = target.closest('[data-product-key]') || (content && state.current);
    if (entry) select(workspace, entry.dataset.productKey, false);
  }
  const root = document.querySelector('#ts-panel-products');
  if (!root) return;
  function financeNotice(label) {
    root.querySelectorAll('[data-product-finance]').forEach(node=>{
      node.dataset.financeState='updating';
      const status=node.querySelector('.ts-finance-status');
      if(status)status.textContent=label;
    });
  }
  document.addEventListener('ps-product-finance-refreshing',()=>financeNotice('Обновляется…'));
  document.addEventListener('ps-product-finance-error',()=>financeNotice('Сводка не обновлена'));
  document.addEventListener('ps-card-snapshot',event=>{
    const snapshots=new Map([...event.detail.document.querySelectorAll('[data-product-finance]')].map(node=>[
      node.dataset.productFinance+':'+node.dataset.financeView,node,
    ]));
    root.querySelectorAll('[data-product-finance]').forEach(node=>{
      const fresh=snapshots.get(node.dataset.productFinance+':'+node.dataset.financeView);
      if(!fresh){
        node.dataset.financeState='stale';
        const status=node.querySelector('.ts-finance-status');
        if(status)status.textContent='Состав изменился';
        return;
      }
      node.innerHTML=fresh.innerHTML;
      node.dataset.financeState=fresh.dataset.financeState;
    });
  });
  function refresh() { root.querySelectorAll('[data-products-workspace]').forEach(init); }
  new MutationObserver(refresh).observe(root, {childList:true, subtree:true});
  window.addEventListener('hashchange', revealAnchor);
  refresh(); revealAnchor();
})();
