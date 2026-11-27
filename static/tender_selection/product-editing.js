(() => {
  let editor, original, draft, editorMode = 'edit', addedKey = '', saving = false;
  const root = document.querySelector('#ts-panel-products');
  if (!root) return;
  editor = root.querySelector('[data-product-editor]');
  const escape = value => { const node=document.createElement('span');node.textContent=value??'';return node.innerHTML.replaceAll('"','&quot;'); };
  const csrf = () => document.querySelector('[name="csrfmiddlewaretoken"]')?.value || '';
  function status(message){editor.querySelector('[data-product-edit-status]').textContent=message}
  function field(row,name,label,numeric=false,locked=false,unlockable=true){return `<label><span>${label}${locked&&unlockable?` <button type="button" class="ts-product-lock" data-unlock="${name}" title="Разблокировать ${label.toLowerCase()}" aria-label="Разблокировать ${label.toLowerCase()}">🔒</button>`:''}</span><input data-field="${name}" value="${escape(row[name])}" ${numeric?'inputmode="decimal"':''} ${locked?'disabled':''}></label>`}
  function rowHtml(row,child=false){
    const locked=Boolean(row.source_locked&&!child);
    return `<div class="ts-product-edit-row ${child?'ts-product-edit-component':''}" data-edit-key="${escape(row.key)}">
      <div class="ts-product-edit-source"><strong>${escape(row.name||'Новый товар')}</strong><small>${escape(row.source_label||(child?'Компонент рабочего состава':'Добавлен вручную'))}</small></div>
      <div class="ts-product-edit-fields">
        ${child||String(row.key).startsWith('new-')?field(row,'name','Наименование'):field(row,'name','Наименование',false,true,false)}
        ${field(row,'quantity',child?'Количество в составе':'Количество',true,locked)}
        ${field(row,'unit','Ед.')}
        ${child?'':field(row,'nmck','НМЦК / базовая цена, ₽',true,locked)}
        <button type="button" class="ts-product-remove" data-remove-product title="${locked?'Исключить исходный товар':'Удалить товар'}" aria-label="${locked?'Исключить исходный товар':'Удалить товар'}">×</button>
      </div>
      ${child?'':`<details class="ts-product-edit-options"><summary>Состав и группа</summary>${field(row,'group','Группа')}<div data-component-rows>${(row.components||[]).map(item=>rowHtml(item,true)).join('')}</div><button type="button" class="ts-quiet" data-add-component>+ Добавить компонент</button></details>`}
    </div>`;
  }
  function render(){const rows=editorMode==='add'?draft.items.filter(row=>row.key===addedKey):draft.items;editor.querySelector('[data-product-edit-table]').innerHTML=rows.map(row=>rowHtml(row)).join('')}
  function toggle(editing){editor.querySelector('[data-product-view-actions]').hidden=editing;editor.querySelector('[data-product-edit-actions]').hidden=!editing;editor.querySelector('[data-product-edit-table]').hidden=!editing;root.querySelectorAll('[data-products-workspace],.ts-products-total,.ts-finance-explanation').forEach(node=>node.hidden=editing);root.toggleAttribute('data-products-editing',editing)}
  function readState(){return JSON.parse(root.querySelector('#ts-product-editor-data').textContent)}
  function newRow(){return {key:'new-'+crypto.randomUUID(),name:'',quantity:'1',unit:'шт',mode:'route',nmck:'',material:'',application:'',logistics:'',group:'',cost_entry:'total',total:'',components:[],source_locked:false,source_label:'Добавлен вручную'};}
  function start(mode='edit'){if(draft)return null;editor=root.querySelector('[data-product-editor]');original=readState();draft=structuredClone(original);draft.confirmations={quantity:[],nmck:[],exclusions:[]};editorMode=mode;addedKey='';let row=null;if(mode==='add'){row=newRow();draft.items.push(row);addedKey=row.key}render();toggle(true);status('');return row}
  function find(key){for(const row of draft.items){if(row.key===key)return row;const child=row.components?.find(item=>item.key===key);if(child)return child}}
  function dirty(){return draft&&JSON.stringify(draft)!==JSON.stringify(original)}
  function cancel(){draft=null;editorMode='edit';addedKey='';toggle(false);status('')}
  async function postState(state){const response=await fetch(editor.dataset.saveUrl,{method:'POST',headers:{'Content-Type':'application/json','X-CSRFToken':csrf()},body:JSON.stringify(state)});let data;try{data=await response.json()}catch{throw new Error('Сервер вернул некорректный ответ. Данные не изменены.')}if(!response.ok)throw new Error(data.error||'Не удалось сохранить товары.');return data}
  async function refreshCard(data){root.querySelector('#ts-product-editor-data').textContent=JSON.stringify(data);const url=new URL(location.href);url.searchParams.set('analysis_snapshot','1');url.searchParams.set('full_card','1');const fresh=await fetch(url,{cache:'no-store'});if(!fresh.ok||fresh.redirected)throw new Error('Изменения сохранены. Обновите карточку для просмотра.');const snapshot=new DOMParser().parseFromString(await fresh.text(),'text/html'),block=snapshot.querySelector('[data-analysis-result]');if(!block)throw new Error('Изменения сохранены. Обновите карточку для просмотра.');root.querySelector('[data-analysis-result]').replaceWith(block);root.removeAttribute('data-products-editing');document.querySelectorAll('[data-card-refresh^="tab-badge-"]').forEach(node=>{const updated=snapshot.querySelector(`[data-card-refresh="${node.dataset.cardRefresh}"]`);if(updated)node.replaceChildren(...updated.childNodes)});editor=root.querySelector('[data-product-editor]');document.dispatchEvent(new CustomEvent('ps-card-snapshot',{detail:{document:snapshot}}))}
  root.addEventListener('input',event=>{const input=event.target.closest('[data-field]');if(!draft||!input)return;find(input.closest('[data-edit-key]').dataset.editKey)[input.dataset.field]=input.value});
  root.addEventListener('click',async event=>{
    const button=event.target.closest('button');if(!button||saving)return;
    if(button.matches('[data-product-edit]')){start('edit');editor.querySelector('input:not(:disabled)')?.focus()}
    if(button.matches('[data-product-add]')){const row=start('add');if(row)editor.querySelector(`[data-edit-key="${row.key}"] input:not(:disabled)`)?.focus()}
    if(button.matches('[data-product-cancel]'))cancel();
    if(button.matches('[data-unlock]')){const node=button.closest('[data-edit-key]'),row=find(node.dataset.editKey),fieldName=button.dataset.unlock;if(!confirm(`Разблокировать ${fieldName==='nmck'?'НМЦК':'количество'}? Рабочее значение будет отличаться от извещения, а маршрут и расчёт потребуют проверки.`))return;draft.confirmations[fieldName].push(row.key);const input=button.closest('label').querySelector('input');input.disabled=false;button.textContent='🔓';input.focus()}
    if(button.matches('[data-add-component]')){const row=find(button.closest('[data-edit-key]').dataset.editKey),child=newRow();child.source_label='Компонент рабочего состава';row.components.push(child);render()}
    if(button.matches('[data-remove-product]')){const node=button.closest('[data-edit-key]'),key=node.dataset.editKey,row=find(key);if(row.source_locked&&!confirm('Исключить исходный товар из рабочего перечня? Исходные данные и история сохранятся.'))return;if(row.source_locked)draft.confirmations.exclusions.push(key);draft.items=draft.items.filter(item=>item.key!==key);draft.items.forEach(item=>item.components=item.components.filter(child=>child.key!==key));if(editorMode==='add'&&key===addedKey)cancel();else node.remove()}
    if(button.matches('[data-product-rename]')){const form=root.querySelector(`[data-product-rename-form][data-commercial-id="${button.dataset.commercialId}"]`);form.hidden=false;form.querySelector('input').focus()}
    if(button.matches('[data-product-rename-cancel]'))button.closest('form').hidden=true;
    if(button.matches('[data-product-save]')){if(draft.confirmations.quantity.length||draft.confirmations.nmck.length||draft.confirmations.exclusions.length){if(!confirm('Сохранить отклонения от извещения? Исходные данные останутся неизменными, зависимые маршруты будут проверены.'))return}saving=true;status('Сохраняю…');editor.querySelectorAll('button,input,select').forEach(node=>node.disabled=true);try{const data=await postState(draft);original=data;draft=null;await refreshCard(data);status('Сохранено')}catch(error){if(!draft)toggle(false);status(error.message)}finally{saving=false;editor.querySelectorAll('button,input,select').forEach(node=>node.disabled=false)}}
  });
  root.addEventListener('submit',async event=>{const form=event.target.closest('[data-product-rename-form]');if(!form)return;event.preventDefault();const state=readState(),row=state.items.find(item=>item.key===`good-${form.dataset.commercialId}`),message=form.querySelector('[data-product-rename-status]');if(!row){message.textContent='Товар изменился. Обновите карточку.';return}row.name=form.elements.name.value;form.querySelectorAll('button,input').forEach(node=>node.disabled=true);message.textContent='Сохраняю…';try{const data=await postState(state);await refreshCard(data)}catch(error){message.textContent=error.message;form.querySelectorAll('button,input').forEach(node=>node.disabled=false)}});
  window.addEventListener('beforeunload',event=>{if(!dirty()&&!saving)return;event.preventDefault();event.returnValue=''});
  window.psConfirmProductLeave=()=>{if(saving){alert('Дождитесь завершения сохранения.');return false}if(!dirty())return true;if(!confirm('Есть несохранённые изменения. Выйти и отменить их?'))return false;return true};
  document.addEventListener('click',event=>{if(!dirty()||!event.target.closest('a[href],[role="tab"],[data-workspace-close]'))return;if(!window.psConfirmProductLeave()){event.preventDefault();event.stopImmediatePropagation()}else cancel()},true);
})();

document.addEventListener('submit',async event=>{
  const form=event.target.closest('[data-manual-step-price]');if(!form)return;event.preventDefault();
  const button=form.querySelector('button'),status=form.querySelector('[role="status"]'),data=Object.fromEntries(new FormData(form));button.disabled=true;
  try{const response=await fetch(form.action,{method:'POST',headers:{'Content-Type':'application/json','X-CSRFToken':data.csrfmiddlewaretoken},body:JSON.stringify(data)}),result=await response.json();if(!response.ok)throw new Error(result.error||'Не удалось сохранить цену.');const url=new URL(location.href);url.hash='product-source-'+data.source_id;location.href=url.href;location.reload()}catch(error){status.textContent=error.message;button.disabled=false}
});
