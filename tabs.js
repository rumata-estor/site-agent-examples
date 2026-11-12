(() => {
  'use strict';
  const tabs = [...document.querySelectorAll('[role="tab"]')];
  const activate = (tab, focus = false) => {
    for (const target of tabs) {
      const selected = target === tab;
      target.setAttribute('aria-selected', String(selected));
      target.tabIndex = selected ? 0 : -1;
      document.getElementById(target.getAttribute('aria-controls')).hidden = !selected;
    }
    if (focus) tab.focus();
    const name = tab.id === 'tab-changeset' ? 'changeset' : tab.id === 'tab-state' ? 'state' : 'task';
    history.replaceState(null, '', '#' + name);
    document.querySelectorAll('[data-language-link]').forEach(link => { link.hash = '#' + name; });
    window.dispatchEvent(new CustomEvent('siteagent-tab-selected', {detail: {name}}));
  };
  tabs.forEach((tab, index) => {
    tab.addEventListener('click', () => activate(tab));
    tab.addEventListener('keydown', e => {
      const offset = e.key === 'ArrowRight' || e.key === 'ArrowDown' ? 1 : e.key === 'ArrowLeft' || e.key === 'ArrowUp' ? -1 : 0;
      if (!offset) return;
      e.preventDefault();
      activate(tabs[(index + offset + tabs.length) % tabs.length], true);
    });
  });
  const initial = tabs.find(tab => tab.id === 'tab-' + location.hash.slice(1)) || tabs[0];
  activate(initial);
})();
