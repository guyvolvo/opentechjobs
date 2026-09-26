// The landing page's Resources and About menus. One open at a time;
// a click outside, Escape, or opening the other closes it. Opens on
// click rather than hover, so it behaves the same with a mouse, a
// keyboard and a finger.
(function () {
  const menus = [...document.querySelectorAll(".site-menu")];
  if (!menus.length) return;

  function set(menu, open) {
    const btn = menu.querySelector(".site-menu-btn");
    const panel = menu.querySelector(".site-menu-panel");
    btn.setAttribute("aria-expanded", String(open));
    panel.hidden = !open;
    menu.classList.toggle("open", open);
  }
  const closeAll = (except) => menus.forEach((m) => { if (m !== except) set(m, false); });

  menus.forEach((menu) => {
    menu.querySelector(".site-menu-btn").addEventListener("click", (e) => {
      e.stopPropagation();
      const open = !menu.classList.contains("open");
      closeAll(menu);
      set(menu, open);
      if (open) menu.querySelector(".site-menu-item")?.focus({ preventScroll: true });
    });
  });
  document.addEventListener("click", (e) => { if (!e.target.closest(".site-menu")) closeAll(); });
  document.addEventListener("keydown", (e) => {
    if (e.key !== "Escape") return;
    const open = menus.find((m) => m.classList.contains("open"));
    if (!open) return;
    set(open, false);
    open.querySelector(".site-menu-btn").focus();
  });
})();
