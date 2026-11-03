/* Read the saved route in the existing requirements drawer; opening never runs AI. */
(() => {
  let overlay, body, title, opener, previousOverflow, request, current, lastRevision;
  function close() {
    if (!overlay?.classList.contains('is-open')) return;
    request?.abort(); overlay.classList.remove('is-open');
    document.body.style.overflow = previousOverflow;
    const target = opener?.isConnected ? opener : [...document.querySelectorAll('[data-source-route]')].find(node => node.dataset.sourceRoute === opener?.dataset.sourceRoute);
    target?.focus({preventScroll:true});
  }
  function mount() {
    if (overlay) return;
    overlay = document.createElement('div'); overlay.className = 'tender-requirements-overlay';
    overlay.innerHTML = '<section class="tender-requirements-drawer" role="dialog" aria-modal="true" aria-labelledby="item-route-title"><header class="modal-header"><div><small>Маршрут производства</small><h2 id="item-route-title"></h2></div><button type="button" class="modal-close" aria-label="Закрыть маршрут">×</button></header><div class="item-route-body"></div></section>';
    document.body.append(overlay);
    title = overlay.querySelector('h2'); body = overlay.querySelector('.item-route-body');
    overlay.querySelector('.modal-close').addEventListener('click', close);
    overlay.addEventListener('click', event => { if (event.target === overlay) close(); });
    document.addEventListener('keydown', event => {
      if (!overlay.classList.contains('is-open')) return;
      if (event.key === 'Escape') { event.stopImmediatePropagation(); event.preventDefault(); close(); }
      if (event.key === 'Tab') { const nodes = [...overlay.querySelectorAll('button:not(:disabled),a[href],input:not([type=hidden]):not(:disabled),textarea:not(:disabled)')], first = nodes[0], last = nodes.at(-1); if (event.shiftKey && document.activeElement === first) { last.focus(); event.preventDefault(); } else if (!event.shiftKey && document.activeElement === last) { first.focus(); event.preventDefault(); } }
    }, true);
  }
  function paragraph(text) { const node = document.createElement('p'); node.className = 'ts-sub'; node.textContent = text; body.append(node); }
  async function openRoute(button, refresh = false) {
    mount(); if (!refresh) lastRevision = null;
    current = {tender_id:Number(button.dataset.routeTender), source_item_id:Number(button.dataset.routeSource)};
    if (!overlay.classList.contains('is-open')) previousOverflow = document.body.style.overflow;
    opener = button; request?.abort(); request = new AbortController(); const active = request;
    title.textContent = 'Загружаем маршрут…'; body.replaceChildren();
    overlay.classList.add('is-open'); document.body.style.overflow = 'hidden'; overlay.querySelector('button').focus();
    try {
      const url = new URL(button.dataset.sourceRoute, location.origin); if (url.origin !== location.origin) throw new Error('Некорректная ссылка');
      const response = await fetch(url, {cache:'no-store', signal:active.signal});
      if (!response.ok || response.redirected) throw new Error('Маршрут недоступен');
      const data = await response.json(); if (active !== request) return;
      title.textContent = data.name;
      if (data.state !== 'ready') paragraph(({needs_review:'В маршруте есть неопределённость.', building:'Маршрут строится в фоне.', stale:'ТЗ или этапы изменились. Сохранённый маршрут ниже устарел.', pending:'Маршрут ещё не построен. Нажмите «Обновить» в карточке тендера.'})[data.state] || 'Маршрут недоступен.');
      if (data.pending_changes) paragraph('Есть правки ТЗ на согласовании. Маршрут относится к текущим принятым данным.');
      if (data.reason && !data.assumption) paragraph(data.reason);
      for (const step of data.steps || []) {
        const article = document.createElement('article'); article.className = 'item-route-step';
        const heading = document.createElement('strong'); heading.textContent = step.position + '. ' + step.name; article.append(heading);
        if (step.description) { const description = document.createElement('p'); description.textContent = step.description; article.append(description); }
        body.append(article);
      }
      if (data.missing_inputs?.length) paragraph('Для точного подбора исполнения не хватает данных: ' + data.missing_inputs.join(', ') + '. Требования заказчика не изменены.');
      if (data.assumption || data.interaction) {
        const details = document.createElement('details'), summary = document.createElement('summary'); details.className = 'item-route-assumption';
        summary.textContent = data.interaction?.selected ? 'Рабочий вариант · ' + data.interaction.selected : 'Допущение · можно изменить'; details.append(summary);
        const explanation = document.createElement('p'); explanation.textContent = data.assumption || data.reason || 'Сохранено ваше рабочее решение.'; details.append(explanation);
        if (data.source_conflicts?.length) { const warning = document.createElement('p'); warning.className = 'ts-sub'; warning.textContent = 'В источниках есть расхождения: ' + data.source_conflicts.join(', ') + '. Рабочий выбор не меняет ТЗ заказчика.'; details.append(warning); }
        if (data.interaction) {
          function makeForm() {
            const form = document.createElement('form'); form.method = 'post'; form.action = data.interaction.url; form.dataset.ownerAnswer = ''; form.dataset.tenderId = data.tender_id;
            const csrf = document.createElement('input'); csrf.type = 'hidden'; csrf.name = 'csrfmiddlewaretoken'; csrf.value = document.querySelector('[name="csrfmiddlewaretoken"]')?.value || ''; form.append(csrf); return form;
          }
          function statusFor(form) { const status = document.createElement('span'); status.dataset.ownerAnswerStatus = ''; status.setAttribute('role', 'status'); form.append(status); }
          if (data.interaction.options?.length >= 2) {
            const form = makeForm(), options = document.createElement('div'); options.className = 'ts-question-options';
            for (const value of data.interaction.options) { const label = document.createElement('label'), radio = document.createElement('input'), text = document.createElement('span'); radio.type = 'radio'; radio.name = 'answer'; radio.value = value; radio.required = true; radio.checked = value === data.interaction.selected; text.textContent = value; label.append(radio, text); options.append(label); }
            form.append(options); statusFor(form); details.append(form);
          }
          const custom = makeForm(), input = document.createElement('textarea'), apply = document.createElement('button'); input.name = 'answer'; input.required = true; input.rows = 2; input.className = 'form-control'; input.setAttribute('aria-label', 'Своя правка маршрута'); input.placeholder = 'Своя правка маршрута…';
          if (data.interaction.selected && !data.interaction.options?.includes(data.interaction.selected)) input.value = data.interaction.selected;
          apply.type = 'submit'; apply.className = 'button-secondary'; apply.textContent = 'Применить'; custom.append(input, apply); statusFor(custom); details.append(custom);
        }
        body.append(details);
      }
    } catch (error) { if (error.name !== 'AbortError' && active === request) { title.textContent = 'Маршрут'; paragraph(error.message || 'Не удалось загрузить маршрут.'); } }
  }
  document.addEventListener('click', event => { const button = event.target.closest('[data-source-route]'); if (!button) return; event.preventDefault(); event.stopPropagation(); openRoute(button); });
  document.addEventListener('ps-intake-status', event => { const data = event.detail; if (!overlay?.classList.contains('is-open') || data.processing || data.tender_id !== current?.tender_id || data.revision === lastRevision) return; lastRevision = data.revision; const button = [...document.querySelectorAll('[data-source-route]')].find(node => node.dataset.sourceRoute === opener?.dataset.sourceRoute) || opener; if (button) openRoute(button, true); });
})();
