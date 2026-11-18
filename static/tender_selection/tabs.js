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
    window.addEventListener('hashchange', revealAnchor);
    revealAnchor();
  });
})();
