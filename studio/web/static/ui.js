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
