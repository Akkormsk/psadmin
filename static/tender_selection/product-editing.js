(() => {
  let editor, original, draft, saving = false;
  const root = document.querySelector('#ts-panel-products');
  if (!root) return;
  const escape = value => {
    const node = document.createElement('span');
    node.textContent = value ?? '';
    return node.innerHTML.replaceAll('"', '&quot;');
  };
  function status(message) { editor.querySelector('[data-product-edit-status]').textContent = message; }
  function field(row, name, label, numeric = false) {
    return `<label><span>${label}</span><input data-field="${name}" value="${escape(row[name])}" ${numeric ? 'inputmode="decimal"' : ''}></label>`;
  }
  function rowHtml(row, child = false) {
    return `<div class="ts-product-edit-row ${child ? 'ts-product-edit-component' : ''}" data-edit-key="${escape(row.key)}">
      <div class="ts-product-edit-fields">
        ${field(row, 'name', 'Наименование')}
        ${field(row, 'quantity', child ? 'Количество в составе' : 'Количество', true)}
        ${field(row, 'unit', 'Ед.')}
        <button type="button" class="ts-quiet" data-remove-product aria-label="Исключить ${escape(row.name)}">Исключить</button>
      </div>
      ${child ? '' : `<details class="ts-product-edit-options"><summary>Состав и группа</summary>
        ${field(row, 'group', 'Группа')}
        <div data-component-rows>${(row.components || []).map(item => rowHtml(item, true)).join('')}</div>
        <button type="button" class="ts-quiet" data-add-component>+ Добавить компонент</button>
      </details>`}
    </div>`;
  }
  function render() {
    editor.querySelector('[data-product-edit-table]').innerHTML = draft.items.map(row => rowHtml(row)).join('');
  }
  function toggle(editing) {
    editor.querySelector('[data-product-view-actions]').hidden = editing;
    editor.querySelector('[data-product-edit-actions]').hidden = !editing;
    editor.querySelector('[data-product-edit-table]').hidden = !editing;
    root.querySelectorAll('[data-products-workspace],.ts-products-total,.ts-finance-explanation').forEach(node => node.hidden = editing);
    root.toggleAttribute('data-products-editing', editing);
  }
  function start() {
    if (draft) return;
    editor = root.querySelector('[data-product-editor]');
    original = JSON.parse(root.querySelector('#ts-product-editor-data').textContent);
    draft = structuredClone(original);
    render(); toggle(true); status('');
  }
  function newRow() {
    return {key:'new-' + crypto.randomUUID(), name:'', quantity:'1', unit:'шт', mode:'route', nmck:'', material:'', application:'', logistics:'', group:'', cost_entry:'total', total:'', components:[]};
  }
  function find(key) {
    for (const row of draft.items) {
      if (row.key === key) return row;
      const child = row.components?.find(item => item.key === key);
      if (child) return child;
    }
  }
  function dirty() { return draft && JSON.stringify(draft) !== JSON.stringify(original); }
  function cancel() { draft = null; toggle(false); status(''); }
  root.addEventListener('input', event => {
    const input = event.target.closest('[data-field]');
    if (!draft || !input) return;
    const rowNode = input.closest('[data-edit-key]');
    find(rowNode.dataset.editKey)[input.dataset.field] = input.value;
    if (input.dataset.field === 'cost_entry') {
      const priceNode = input.closest('.ts-product-edit-price');
      priceNode.querySelector('[data-manual-total]').hidden = input.value !== 'total';
      priceNode.querySelector('[data-manual-parts]').hidden = input.value !== 'parts';
    }
    if (input.dataset.field === 'mode') {
      const priceNode = input.closest('.ts-product-edit-price');
      priceNode.querySelector('[data-manual-price]').hidden = input.value !== 'manual';
      priceNode.querySelector('[data-route-price-note]').hidden = input.value !== 'route';
    }
  });
  root.addEventListener('click', async event => {
    const button = event.target.closest('button');
    if (!button || saving) return;
    if (button.matches('[data-product-edit]')) { start(); editor.querySelector('input')?.focus(); }
    if (button.matches('[data-product-add]')) {
      start(); const row = newRow(); draft.items.push(row); render();
      editor.querySelector(`[data-edit-key="${row.key}"] input`).focus();
    }
    if (button.matches('[data-product-cancel]')) cancel();
    if (button.matches('[data-add-component]')) {
      const row = find(button.closest('[data-edit-key]').dataset.editKey);
      const child = newRow(); row.components.push(child);
      button.previousElementSibling.insertAdjacentHTML('beforeend', rowHtml(child, true));
      button.previousElementSibling.lastElementChild.querySelector('input').focus();
    }
    if (button.matches('[data-remove-product]')) {
      const node = button.closest('[data-edit-key]'), key = node.dataset.editKey;
      draft.items = draft.items.filter(row => row.key !== key);
      draft.items.forEach(row => row.components = row.components.filter(child => child.key !== key));
      node.remove();
    }
    if (button.matches('[data-product-save]')) {
      saving = true; status('Сохраняю…');
      editor.querySelectorAll('button,input,select').forEach(node => node.disabled = true);
      try {
        const response = await fetch(editor.dataset.saveUrl, {method:'POST', headers:{'Content-Type':'application/json', 'X-CSRFToken':document.querySelector('[name="csrfmiddlewaretoken"]').value}, body:JSON.stringify(draft)});
        const data = await response.json();
        if (!response.ok) throw new Error(data.error || 'Не удалось сохранить товары.');
        original = data;
        draft = null;
        root.querySelector('#ts-product-editor-data').textContent = JSON.stringify(data);
        const url = new URL(location.href); url.searchParams.set('analysis_snapshot', '1'); url.searchParams.set('full_card', '1');
        const fresh = await fetch(url, {cache:'no-store'});
        if (!fresh.ok || fresh.redirected) throw new Error('Товары сохранены. Перезагрузите карточку для просмотра.');
        const documentSnapshot = new DOMParser().parseFromString(await fresh.text(), 'text/html');
        const block = documentSnapshot.querySelector('[data-analysis-result]');
        if (!block) throw new Error('Товары сохранены. Перезагрузите карточку для просмотра.');
        root.querySelector('[data-analysis-result]').replaceWith(block);
        root.removeAttribute('data-products-editing');
        document.querySelectorAll('[data-card-refresh^="tab-badge-"]').forEach(node => {
          const updated = documentSnapshot.querySelector(`[data-card-refresh="${node.dataset.cardRefresh}"]`);
          if (updated) node.replaceChildren(...updated.childNodes);
        });
        editor = root.querySelector('[data-product-editor]'); status('Сохранено');
        document.dispatchEvent(new CustomEvent('ps-card-snapshot', {detail:{document:documentSnapshot}}));
      } catch (error) {
        if (!draft) toggle(false);
        status(draft ? error.message : 'Товары сохранены. Обновите страницу для просмотра.');
      }
      finally { saving = false; editor.querySelectorAll('button,input,select').forEach(node => node.disabled = false); }
    }
  });
  window.addEventListener('beforeunload', event => {
    if (!dirty() && !saving) return;
    event.preventDefault(); event.returnValue = '';
  });
  window.psConfirmProductLeave = () => {
    if (saving) { alert('Дождитесь завершения сохранения.'); return false; }
    if (!dirty()) return true;
    if (!confirm('Есть несохранённые изменения. Выйти и отменить их?')) return false;
    return true;
  };
  document.addEventListener('click', event => {
    if (!dirty() || !event.target.closest('a[href],[role="tab"],[data-workspace-close]')) return;
    if (!window.psConfirmProductLeave()) {
      event.preventDefault(); event.stopImmediatePropagation();
    } else cancel();
  }, true);
})();

document.addEventListener('submit', async event => {
  const form = event.target.closest('[data-manual-step-price]');
  if (!form) return;
  event.preventDefault();
  const button = form.querySelector('button'), status = form.querySelector('[role="status"]');
  const data = Object.fromEntries(new FormData(form));
  button.disabled = true;
  try {
    const response = await fetch(form.action, {method:'POST', headers:{'Content-Type':'application/json','X-CSRFToken':data.csrfmiddlewaretoken}, body:JSON.stringify(data)});
    const result = await response.json();
    if (!response.ok) throw new Error(result.error || 'Не удалось сохранить цену.');
    const url = new URL(location.href); url.hash = 'product-source-' + data.source_id;
    location.href = url.href; location.reload();
  } catch (error) { status.textContent = error.message; button.disabled = false; }
});
