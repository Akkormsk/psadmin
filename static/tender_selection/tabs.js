(function () {
  document.querySelectorAll('[data-tender-tabs]').forEach(function (workspace) {
    var tabs = Array.from(workspace.querySelectorAll('[role="tab"]'));
    function activate(tab, focus) {
      tabs.forEach(function (candidate) {
        var selected = candidate === tab;
        candidate.setAttribute('aria-selected', String(selected));
        candidate.tabIndex = selected ? 0 : -1;
        document.getElementById(candidate.getAttribute('aria-controls')).hidden = !selected;
      });
      if (focus) tab.focus();
    }
    tabs.forEach(function (tab, index) {
      tab.addEventListener('click', function () { activate(tab, false); });
      tab.addEventListener('keydown', function (event) {
        var target;
        switch (event.key) {
          case 'ArrowRight': target = tabs[(index + 1) % tabs.length]; break;
          case 'ArrowLeft': target = tabs[(index + tabs.length - 1) % tabs.length]; break;
          case 'Home': target = tabs[0]; break;
          case 'End': target = tabs[tabs.length - 1]; break;
          default: return;
        }
        event.preventDefault();
        activate(target, true);
      });
    });
    function revealAnchor() {
      var target = document.getElementById(window.location.hash.slice(1));
      var panel = target && target.closest('.ts-tab-panel');
      if (!panel || !workspace.contains(panel)) return;
      var tab = tabs.find(function (candidate) {
        return candidate.getAttribute('aria-controls') === panel.id;
      });
      if (tab) activate(tab, false);
    }
    var badgeRequest;
    function resultNotice(label) {
      var container = workspace.querySelector('[data-card-refresh="tab-badge-results"]');
      if (!container) return;
      var badge = document.createElement('small');
      badge.className = 'ts-tab-badge';
      badge.textContent = label;
      container.replaceChildren(badge);
    }
    document.addEventListener('ps-product-calculation-saved', async function () {
      if (badgeRequest) badgeRequest.abort();
      var request = new AbortController();
      badgeRequest = request;
      resultNotice('Расчёт обновлён');
      try {
        var url = new URL(window.location.href);
        url.searchParams.set('analysis_snapshot', '1');
        url.searchParams.set('full_card', '1');
        var response = await fetch(url, {cache: 'no-store', signal: request.signal});
        if (!response.ok || response.redirected) throw new Error('Navigation unavailable');
        var parsed = new DOMParser().parseFromString(await response.text(), 'text/html');
        if (badgeRequest !== request) return;
        workspace.querySelectorAll('[data-card-refresh^="tab-badge-"]').forEach(function (container) {
          var fresh = parsed.querySelector('[data-card-refresh="' + container.dataset.cardRefresh + '"]');
          if (fresh) container.innerHTML = fresh.innerHTML;
        });
      } catch (error) {
        if (error.name !== 'AbortError' && badgeRequest === request) resultNotice('Сводка не обновлена');
      }
    });
    window.addEventListener('hashchange', revealAnchor);
    revealAnchor();
  });
})();
