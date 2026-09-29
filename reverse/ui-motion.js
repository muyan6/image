"use strict";
(() => {
  const preference = window.matchMedia?.("(prefers-reduced-motion: reduce)");
  const active = new Map();
  const ease = "cubic-bezier(.22,1,.36,1)";
  const reduced = () => !!preference?.matches;
  function cancel(element) { active.get(element)?.stop(false); }
  function animate(element, frames, options = {}, complete = () => {}) {
    cancel(element);
    if (!element?.animate || reduced()) { complete(); return; }
    const animation = element.animate(frames, { duration: 240, easing: ease, ...options });
    let done = false;
    const record = { stop(finish) {
      if (done) return;
      done = true;
      active.delete(element);
      animation?.cancel?.();
      if (finish) complete();
    } };
    active.set(element, record);
    if (animation?.finished) animation.finished.then(() => record.stop(true), () => record.stop(false));
    else record.stop(true);
  }
  function enter(element, { duration = 240, y = 10, scale = 1 } = {}) {
    if (!element || element.hidden) return;
    const previous = active.has(element) ? getComputedStyle(element) : null;
    const from = previous ? { opacity: previous.opacity, transform: previous.transform } :
      { opacity: 0, transform: `translateY(${y}px) scale(${scale})` };
    animate(element, [from, { opacity: 1, transform: "none" }], { duration });
  }
  function cancelAll(finish = false) {
    for (const record of [...active.values()]) record.stop(finish);
  }
  function tween(key, from, to, duration, update) {
    cancel(key);
    if (reduced()) { update(to); return; }
    let frame, started;
    const record = { stop(finish) {
      cancelAnimationFrame(frame);
      active.delete(key);
      if (finish) update(to);
    } };
    active.set(key, record);
    function step(now) {
      started ??= now;
      const progress = Math.min(1, (now - started) / duration);
      update(from + (to - from) * (1 - Math.pow(1 - progress, 3)));
      if (progress < 1) frame = requestAnimationFrame(step);
      else record.stop(true);
    }
    frame = requestAnimationFrame(step);
  }
  preference?.addEventListener?.("change", () => { if (reduced()) cancelAll(true); });
  window.addEventListener("pagehide", () => cancelAll(true));

  function dialog(panel, { backdropClose = false } = {}) {
    let invoker = null, invokerKey = null, restoring = true, closing = false;
    let measuredHeight = 0, resizing = false;
    const lock = () => document.documentElement.classList.toggle("modal-open", !!document.querySelector("dialog[open]"));
    const offscreen = () => window.innerWidth <= 600 ? "translateY(105%)" : "translateX(105%)";
    function close({ immediate = false, restoreFocus = true } = {}) {
      if (!panel.open || (closing && !immediate)) return;
      restoring = restoreFocus;
      if (!closing) panel.dispatchEvent(new Event("uiclose"));
      closing = true;
      panel.dataset.motion = "closing";
      panel.inert = true;
      const finish = () => { panel.close(); };
      if (immediate) { cancel(panel); finish(); }
      else animate(panel, [
        { transform: getComputedStyle(panel).transform, opacity: getComputedStyle(panel).opacity },
        { transform: offscreen(), opacity: 0.8 },
      ], { duration: 260 }, finish);
    }
    function open() {
      const interrupted = panel.open;
      const from = interrupted ? getComputedStyle(panel).transform : offscreen();
      cancel(panel);
      if (!interrupted) {
        invoker = document.activeElement;
        invokerKey = invoker?.dataset.focusKey;
        panel.showModal();
      }
      closing = false;
      resizing = false;
      restoring = true;
      panel.inert = false;
      panel.dataset.motion = "open";
      lock();
      if (!interrupted || from !== "none") animate(panel, [
        { transform: from, opacity: 0.85 },
        { transform: window.innerWidth <= 600 ? "translateY(-3px)" : "translateX(-3px)", opacity: 1, offset: 0.82 },
        { transform: "none", opacity: 1 },
      ], { duration: 420 });
      panel.querySelectorAll('[role="tablist"]').forEach(nav => nav.dispatchEvent(new Event("uitabsresize")));
    }
    panel.addEventListener("cancel", event => { event.preventDefault(); close(); });
    panel.addEventListener("close", () => {
      // Native close events are queued; an older close may arrive after a reopen.
      if (panel.open) return;
      cancel(panel);
      if (!closing) panel.dispatchEvent(new Event("uiclose"));
      closing = false;
      panel.inert = false;
      delete panel.dataset.motion;
      lock();
      if (restoring) {
        const replacement = invokerKey && [...document.querySelectorAll('[data-focus-key]')].find(e => e.dataset.focusKey === invokerKey);
        const target = invoker?.isConnected && !invoker.closest('[hidden], [inert]') ? invoker : replacement || document.getElementById("account-toggle");
        if (target?.getClientRects().length) target.focus({ preventScroll: true });
      }
    });
    let outside = false;
    const isOutside = event => {
      const r = panel.getBoundingClientRect();
      return event.clientX < r.left || event.clientX > r.right || event.clientY < r.top || event.clientY > r.bottom;
    };
    if (backdropClose) {
      panel.addEventListener("pointerdown", event => { outside = isOutside(event); });
      panel.addEventListener("pointerup", event => { if (outside && isOutside(event)) close(); outside = false; });
    }
    if (window.ResizeObserver) new ResizeObserver(entries => {
      const height = entries[0].contentRect.height, previous = measuredHeight;
      measuredHeight = height;
      if (!panel.open || closing || window.innerWidth > 600 || !previous || !height || Math.abs(height - previous) < 2) return;
      if (active.has(panel) && !resizing) return;
      const current = getComputedStyle(panel).transform;
      const offset = current === "none" ? 0 : new DOMMatrixReadOnly(current).m42;
      resizing = true;
      animate(panel, [
        { transform: `translateY(${height - previous + offset}px)` },
        { transform: "none" },
      ], { duration: 320 }, () => { resizing = false; });
    }).observe(panel);
    return { open, close, get closing() { return closing; } };
  }

  let tabId = 0;
  function tabs(nav, selector, panelFor, activate) {
    const buttons = [...nav.querySelectorAll(selector)];
    nav.setAttribute("role", "tablist");
    const indicator = document.createElement("span");
    indicator.className = "tab-indicator";
    indicator.setAttribute("aria-hidden", "true");
    nav.append(indicator);
    let selected = buttons.find(b => b.classList.contains("selected")) || buttons[0];
    function position() {
      if (!selected || !nav.getClientRects().length) return;
      if (nav.getAttribute("aria-orientation") === "vertical") {
        indicator.style.width = "3px";
        indicator.style.height = `${selected.offsetHeight}px`;
        indicator.style.transform = `translateY(${selected.offsetTop}px)`;
        return;
      }
      const old = indicator.getBoundingClientRect();
      cancel(indicator);
      indicator.style.width = `${selected.offsetWidth}px`;
      indicator.style.transform = `translateX(${selected.offsetLeft}px)`;
      const next = indicator.getBoundingClientRect();
      if (old.width && next.width) animate(indicator, [
        { transformOrigin: "left", transform: `translateX(${selected.offsetLeft + old.left - next.left}px) scaleX(${old.width / next.width})` },
        { transformOrigin: "left", transform: `translateX(${selected.offsetLeft}px) scaleX(1)` },
      ]);
    }
    function select(button, motion = true) {
      const changed = selected !== button;
      selected = button;
      for (const b of buttons) {
        const on = b === button;
        b.classList.toggle("selected", on);
        b.setAttribute("aria-selected", String(on));
        b.tabIndex = on ? 0 : -1;
      }
      const target = panelFor(button);
      target?.setAttribute("aria-labelledby", button.id);
      position();
      if (changed && motion) enter(target);
    }
    for (const b of buttons) {
      b.id ||= `ui-tab-${++tabId}`;
      b.setAttribute("role", "tab");
      const target = panelFor(b);
      if (target) {
        target.id ||= `ui-pane-${++tabId}`;
        b.setAttribute("aria-controls", target.id);
        target.setAttribute("role", "tabpanel");
        target.tabIndex = 0;
      }
      b.addEventListener("click", () => activate(b));
      b.addEventListener("keydown", event => {
        const i = buttons.indexOf(b);
        const key = nav.getAttribute("aria-orientation") === "vertical" ? ({ ArrowDown: "ArrowRight", ArrowUp: "ArrowLeft", Home: "Home", End: "End" }[event.key]) : event.key;
        const next = { ArrowRight: (i + 1) % buttons.length, ArrowLeft: (i + buttons.length - 1) % buttons.length, Home: 0, End: buttons.length - 1 }[key];
        if (next === undefined) return;
        event.preventDefault();
        buttons[next].focus({ preventScroll: true });
        buttons[next].scrollIntoView?.({ block: "nearest", inline: "nearest", behavior: "instant" });
        activate(buttons[next]);
      });
    }
    new (window.ResizeObserver || class { observe() {} })(position).observe(nav);
    nav.addEventListener("uitabsresize", position);
    select(selected, false);
    return { select };
  }
  function loading(target, label = "正在读取…") {
    target.setAttribute("aria-busy", "true");
    const box = document.createElement("div");
    box.className = "list-loading";
    const caption = document.createElement("p");
    caption.className = "subtle";
    caption.textContent = label;
    box.append(caption);
    for (let i = 0; i < 3; i++) {
      const line = document.createElement("span");
      line.className = "skeleton-line";
      line.setAttribute("aria-hidden", "true");
      box.append(line);
    }
    target.replaceChildren(box);
  }
  function loadError(target, message, retry) {
    target.removeAttribute("aria-busy");
    const box = document.createElement("div");
    box.className = "list-error";
    const p = document.createElement("p");
    p.textContent = message;
    const button = document.createElement("button");
    button.className = "button secondary";
    button.textContent = "重新读取";
    button.onclick = retry;
    box.append(p, button);
    target.replaceChildren(box);
  }
  function busy(form, value, label = "正在提交…") {
    form.setAttribute("aria-busy", String(value));
    const buttons = form.matches("button") ? [form] : [...form.querySelectorAll("button")];
    for (const button of buttons) {
      if (value) {
        if (button.dataset.idleLabel === undefined) {
          button.dataset.idleLabel = button.textContent;
          button.dataset.wasDisabled = String(button.disabled);
        }
        button.disabled = true;
        button.classList.add("is-pending");
        button.textContent = label;
      } else if (button.dataset.idleLabel !== undefined) {
        button.textContent = button.dataset.idleLabel;
        button.disabled = button.dataset.wasDisabled === "true";
        button.classList.remove("is-pending");
        delete button.dataset.idleLabel;
        delete button.dataset.wasDisabled;
      }
    }
  }
  const viewport = () => {
    const v = window.visualViewport;
    document.documentElement.style.setProperty("--visual-height", `${v?.height || window.innerHeight}px`);
    document.documentElement.style.setProperty("--visual-bottom", `${v ? Math.max(0, window.innerHeight - v.height - v.offsetTop) : 0}px`);
    const header = document.querySelector(".site-header");
    if (header) document.documentElement.style.setProperty("--site-header-height", `${Math.ceil(header.getBoundingClientRect().height)}px`);
  };
  const header = document.querySelector(".site-header");
  if (header && window.ResizeObserver) new ResizeObserver(viewport).observe(header);
  window.visualViewport?.addEventListener("resize", viewport);
  window.visualViewport?.addEventListener("scroll", viewport);
  window.addEventListener("resize", viewport);
  viewport();
  window.UI = { animate, tween, enter, cancel, cancelAll, reduced, dialog, tabs, loading, loadError, busy, ease };
})();
