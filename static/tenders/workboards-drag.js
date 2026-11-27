(() => {
  const board = document.querySelector('[data-workboard]');
  if (!board) return;
  const status = document.querySelector('[data-board-move-status]');
  let dragged = null, busy = false, suppressClick = false;
  function clearTargets() { board.querySelectorAll('.is-drop-target').forEach(n => n.classList.remove('is-drop-target')); }
  board.addEventListener('click', e => {
    const card = e.target.closest('[data-board-card]');
    if (!card) return;
    if (busy || suppressClick) { e.preventDefault(); return; }
    if (!e.target.closest('a,button,form')) location.assign(card.querySelector('.wb-card-open').href);
  });
  board.addEventListener('dragstart', e => {
    const card = e.target.closest('[data-board-card]');
    if (!card || busy || e.target.closest('button,form')) { e.preventDefault(); return; }
    dragged = card; suppressClick = true;
    e.dataTransfer.effectAllowed = 'move';
    e.dataTransfer.setData('text/plain', card.dataset.moveUrl);
    card.classList.add('is-dragging');
  });
  board.addEventListener('dragend', () => {
    dragged?.classList.remove('is-dragging');
    dragged = null; clearTargets();
    setTimeout(() => { suppressClick = false; }, 100);
  });
  board.addEventListener('dragover', e => {
    const column = e.target.closest('[data-board-column]');
    if (!dragged || busy || !column || column === dragged.closest('[data-board-column]')) return;
    e.preventDefault(); e.dataTransfer.dropEffect = 'move';
    clearTargets(); column.classList.add('is-drop-target');
  });
  board.addEventListener('dragleave', e => {
    const column = e.target.closest('[data-board-column]');
    if (column && !column.contains(e.relatedTarget)) column.classList.remove('is-drop-target');
  });
  board.addEventListener('drop', async e => {
    const column = e.target.closest('[data-board-column]');
    if (!dragged || busy || !column) return;
    e.preventDefault();
    const card = dragged; clearTargets();
    if (column === card.closest('[data-board-column]')) return;
    busy = true; status.hidden = true;
    try {
      const token = card.querySelector('[name=csrfmiddlewaretoken]').value;
      const response = await fetch(card.dataset.moveUrl, {
        method:'POST', credentials:'same-origin',
        headers:{'X-CSRFToken':token,'Accept':'application/json'},
        body:new URLSearchParams({column_id:column.dataset.boardColumn})
      });
      const result = await response.json();
      if (!response.ok || !result.ok) throw new Error(result.error || 'Не удалось переместить карточку.');
      location.reload();
    } catch(error) {
      status.textContent = error.message || 'Не удалось переместить карточку.';
      status.hidden = false; busy = false; card.classList.remove('is-dragging');
    }
  });
})();
