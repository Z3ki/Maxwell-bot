"use strict";
// Loaded in <head> without defer so the js class lands before first paint.
// Everything below is garnish. If this file never loads, the site still reads fine;
// that is the whole point, and the last version forgot it.
document.documentElement.classList.add("js");

(() => {
  const reduceMotion = window.matchMedia("(prefers-reduced-motion: reduce)");

  function header() {
    const bar = document.querySelector(".site-header");
    if (!bar) return;
    const onScroll = () => bar.classList.toggle("scrolled", window.scrollY > 8);
    onScroll();
    window.addEventListener("scroll", onScroll, { passive: true });

    const toggle = bar.querySelector(".menu-toggle");
    const nav = bar.querySelector(".site-nav");
    if (!toggle || !nav) return;
    const setOpen = (open) => {
      bar.classList.toggle("open", open);
      toggle.setAttribute("aria-expanded", open ? "true" : "false");
      toggle.setAttribute("aria-label", open ? "Close menu" : "Open menu");
    };
    toggle.addEventListener("click", () => setOpen(!bar.classList.contains("open")));
    nav.addEventListener("click", (event) => { if (event.target.closest("a")) setOpen(false); });
    document.addEventListener("keydown", (event) => {
      if (event.key !== "Escape" || !bar.classList.contains("open")) return;
      setOpen(false);
      toggle.focus();
    });
    document.addEventListener("click", (event) => {
      if (bar.classList.contains("open") && !bar.contains(event.target)) setOpen(false);
    });
    // Rotating a phone to landscape should not leave a ghost menu pinned open.
    window.matchMedia("(min-width: 921px)").addEventListener("change", (event) => { if (event.matches) setOpen(false); });
  }

  function demoTabs() {
    document.querySelectorAll("[role=tablist]").forEach((list) => {
      const tabs = Array.from(list.querySelectorAll("[role=tab]"));
      if (!tabs.length) return;
      let touched = false;
      let timer = 0;

      const select = (tab, focus) => {
        tabs.forEach((other) => {
          const on = other === tab;
          other.setAttribute("aria-selected", on ? "true" : "false");
          other.tabIndex = on ? 0 : -1;
          const panel = document.getElementById(other.getAttribute("aria-controls"));
          if (!panel) return;
          panel.hidden = !on;
          panel.classList.remove("play");
          if (on) {
            void panel.offsetWidth; // restart the CSS animation; yes, really, this is the API
            panel.classList.add("play");
          }
        });
        if (focus) tab.focus();
      };
      const stop = () => { touched = true; window.clearInterval(timer); };

      tabs.forEach((tab, index) => {
        tab.addEventListener("click", () => { stop(); select(tab, false); });
        tab.addEventListener("keydown", (event) => {
          const moves = { ArrowRight: 1, ArrowLeft: -1, Home: -index, End: tabs.length - 1 - index };
          if (!(event.key in moves)) return;
          event.preventDefault();
          stop();
          select(tabs[(index + moves[event.key] + tabs.length) % tabs.length], true);
        });
      });

      const current = tabs.find((tab) => tab.getAttribute("aria-selected") === "true") || tabs[0];
      select(current, false);

      // Gentle auto-advance while the demo is on screen and nobody has touched it.
      const frame = list.closest("[data-autoplay]");
      if (!frame || reduceMotion.matches || !("IntersectionObserver" in window)) return;
      const advance = () => {
        const index = tabs.findIndex((tab) => tab.getAttribute("aria-selected") === "true");
        select(tabs[(index + 1) % tabs.length], false);
      };
      frame.addEventListener("pointerenter", () => window.clearInterval(timer));
      frame.addEventListener("focusin", stop);
      new IntersectionObserver((entries) => {
        window.clearInterval(timer);
        if (!touched && entries[0].isIntersecting) timer = window.setInterval(advance, 7000);
      }, { threshold: 0.5 }).observe(frame);
      frame.addEventListener("pointerleave", () => {
        window.clearInterval(timer);
        if (!touched) timer = window.setInterval(advance, 7000);
      });
    });
  }

  function copyButtons() {
    document.querySelectorAll("[data-copy-target]").forEach((button) => {
      const command = document.getElementById(button.dataset.copyTarget);
      const status = document.getElementById(button.dataset.copyStatus || "copy-status");
      if (!command) return;
      const say = (text) => { if (status) status.textContent = text; };
      button.addEventListener("click", async () => {
        const label = button.textContent;
        try {
          if (!navigator.clipboard || !window.isSecureContext) throw new Error("no clipboard");
          await navigator.clipboard.writeText(command.textContent.trim());
          button.textContent = "Copied";
          say("Copied. Read the script first, then paste it into a terminal.");
        } catch {
          const range = document.createRange();
          range.selectNodeContents(command);
          const selection = window.getSelection();
          selection.removeAllRanges();
          selection.addRange(range);
          say("Selected. Use your device's copy action.");
        }
        window.setTimeout(() => { button.textContent = label; }, 1800);
      });
    });
  }

  function toc() {
    const box = document.querySelector(".toc");
    if (!box) return;
    const narrow = window.matchMedia("(max-width: 900px)");
    const summary = box.querySelector("summary");
    // On wide screens the list is a sidebar and should never fold shut.
    if (summary) summary.addEventListener("click", (event) => { if (!narrow.matches) event.preventDefault(); });
    const sync = () => { box.open = !narrow.matches; };
    sync();
    narrow.addEventListener("change", sync);

    const links = Array.from(box.querySelectorAll('a[href^="#"]'));
    links.forEach((link) => link.addEventListener("click", () => { if (narrow.matches) box.open = false; }));
    const targets = links.map((link) => document.getElementById(decodeURIComponent(link.hash.slice(1)))).filter(Boolean);
    if (!targets.length || !("IntersectionObserver" in window)) return;

    const mark = (id) => links.forEach((link) => {
      if (link.hash.slice(1) === id) link.setAttribute("aria-current", "true");
      else link.removeAttribute("aria-current");
    });
    const visible = new Set();
    const observer = new IntersectionObserver((entries) => {
      entries.forEach((entry) => { if (entry.isIntersecting) visible.add(entry.target); else visible.delete(entry.target); });
      const first = targets.find((target) => visible.has(target));
      if (first) { mark(first.id); return; }
      // Between headings: keep whichever one we last scrolled past.
      const passed = targets.filter((target) => target.getBoundingClientRect().top < 120);
      mark((passed[passed.length - 1] || targets[0]).id);
    }, { rootMargin: "-80px 0px -55% 0px" });
    targets.forEach((target) => observer.observe(target));
  }

  function start() {
    header();
    demoTabs();
    copyButtons();
    toc();
  }
  if (document.readyState === "loading") document.addEventListener("DOMContentLoaded", start);
  else start();
})();
