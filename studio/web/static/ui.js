/* Presentation only. All mutations still submit to existing guarded server routes. */
(() => {
  const sidebar = document.getElementById('sidebar');
  const toggle = document.querySelector('[data-menu-toggle]');
  const scrim = document.querySelector('.scrim');
  let menuReturn;
  const menu = open => {
    if (open) menuReturn = document.activeElement;
    sidebar.classList.toggle('open', open);
    scrim.classList.toggle('show', open);
    document.body.classList.toggle('menu-open', open);
    toggle.setAttribute('aria-expanded', String(open));
    if (open) sidebar.querySelector('button, a').focus();
    else menuReturn?.focus();
  };
  toggle?.addEventListener('click', () => menu(!sidebar.classList.contains('open')));
  document.querySelectorAll('[data-menu-close]').forEach(el => el.addEventListener('click', () => menu(false)));
  document.addEventListener('keydown', e => {
    if (!sidebar.classList.contains('open')) return;
    if (e.key === 'Escape') { e.preventDefault(); menu(false); }
    if (e.key === 'Tab') {
      const items = [...sidebar.querySelectorAll('a,button')];
      const first = items[0], last = items.at(-1);
      if (e.shiftKey && document.activeElement === first) { e.preventDefault(); last.focus(); }
      else if (!e.shiftKey && document.activeElement === last) { e.preventDefault(); first.focus(); }
    }
  });
  matchMedia('(min-width:891px)').addEventListener('change', e => { if (e.matches && sidebar.classList.contains('open')) menu(false); });
  document.querySelectorAll('[data-open-dialog]').forEach(opener => {
    const dialog = document.getElementById(opener.dataset.openDialog);
    opener.addEventListener('click', e => {
      if (!dialog?.showModal) return;
      e.preventDefault(); dialog.showModal();
      dialog.addEventListener('close', () => opener.focus(), { once: true });
    });
  });
  document.querySelectorAll('[data-close-dialog]').forEach(el => el.addEventListener('click', e => {
    const dialog = el.closest('dialog');
    if (dialog.open) { e.preventDefault(); dialog.close(); }
  }));
  document.addEventListener('submit', e => {
    const form = e.target;
    const message = e.submitter?.dataset.confirm || form.dataset.confirm;
    if (message && !window.confirm(message)) e.preventDefault();
  });
  document.querySelectorAll('[data-dirty-form]').forEach(form => {
    const initial = new URLSearchParams(new FormData(form)).toString();
    const button = form.querySelector('[data-save]');
    const update = () => { button.disabled = new URLSearchParams(new FormData(form)).toString() === initial || form.dataset.locked === 'true'; };
    form.addEventListener('input', update); form.addEventListener('change', update); update();
  });
  const minutes = document.getElementById('minutes');
  if (minutes) {
    const start = Date.now(); let touched = false;
    minutes.addEventListener('input', () => { touched = true; });
    const tick = () => { if (!touched) minutes.value = (Math.round((Date.now() - start) / 30000) / 2).toFixed(1); };
    tick(); setInterval(tick, 15000);
  }
  document.body.addEventListener('htmx:beforeRequest', e => e.detail.elt.setAttribute('aria-busy', 'true'));
  document.body.addEventListener('htmx:afterRequest', e => {
    e.detail.elt.removeAttribute('aria-busy');
    if (e.detail.failed) {
      const target = e.detail.target;
      if (target?.id === 'offers') {
        const message = document.createElement('p'); message.className = 'flash err'; message.role = 'alert';
        message.textContent = 'Không tải được offer. Kiểm tra kết nối rồi tìm lại.'; target.replaceChildren(message);
      }
    }
  });
})();

/* Tabs remain anchor-linked sections when JavaScript is unavailable. */
document.querySelectorAll('[data-tabs]').forEach(group => {
  const links = [...group.querySelectorAll('[data-tab]')];
  const panels = [...group.querySelectorAll('[data-tab-panel]')];
  const activate = (link, focus = false) => {
    links.forEach(item => {
      const active = item === link;
      item.classList.toggle('active', active);
      item.setAttribute('aria-selected', String(active));
      item.tabIndex = active ? 0 : -1;
    });
    panels.forEach(panel => { panel.hidden = panel.id !== link.dataset.tab; });
    if (focus) link.focus();
  };
  group.querySelector('.tabs').setAttribute('role', 'tablist');
  links.forEach((link, index) => {
    link.id = `${link.dataset.tab}-tab`;
    link.setAttribute('role', 'tab'); link.setAttribute('aria-controls', link.dataset.tab);
    const panel = panels.find(p => p.id === link.dataset.tab);
    panel.setAttribute('role', 'tabpanel'); panel.setAttribute('aria-labelledby', link.id);
    link.addEventListener('click', e => { e.preventDefault(); activate(link); });
    link.addEventListener('keydown', e => {
      const target = e.key === 'ArrowRight' ? links[(index + 1) % links.length] : e.key === 'ArrowLeft' ? links[(index - 1 + links.length) % links.length] : e.key === 'Home' ? links[0] : e.key === 'End' ? links.at(-1) : null;
      if (target) { e.preventDefault(); activate(target, true); }
    });
  });
  activate(links.find(link => link.dataset.tab === location.hash.slice(1)) || links.find(link => link.dataset.tab === group.dataset.initialTab) || links[0]);
});

// A changed task state needs the matching gate/inspector markup from the server.
let taskStatus = document.getElementById('status')?.dataset.taskStatus;
document.body.addEventListener('htmx:afterSwap', e => {
  if (e.detail.target?.id !== 'status') return;
  const current = document.getElementById('status')?.dataset.taskStatus;
  if (taskStatus && current && current !== taskStatus) location.reload();
});
