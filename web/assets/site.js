"use strict";
// Loaded without defer on purpose: the js class has to land before first paint
// or every .reveal block flashes in, vanishes, then fades back. Ask me how I know.
document.documentElement.classList.add("js");

(() => {
  function revealOnScroll() {
    const items = Array.from(document.querySelectorAll(".reveal"));
    if (!items.length) return;
    if (!("IntersectionObserver" in window)) {
      items.forEach((item) => item.classList.add("seen"));
      return;
    }
    // A small per-sibling delay so a grid of cards ripples in instead of thudding.
    items.forEach((item) => {
      const siblings = Array.from(item.parentElement.children).filter((el) => el.classList.contains("reveal"));
      const index = siblings.indexOf(item);
      if (index > 0) item.style.transitionDelay = Math.min(index % 4, 3) * 70 + "ms";
    });
    const observer = new IntersectionObserver((entries) => {
      entries.forEach((entry) => {
        if (!entry.isIntersecting) return;
        entry.target.classList.add("seen");
        observer.unobserve(entry.target);
      });
    }, { rootMargin: "0px 0px -40px 0px", threshold: 0.08 });
    items.forEach((item) => observer.observe(item));
  }

  function demoTabs() {
    document.querySelectorAll("[role=tablist]").forEach((list) => {
      const tabs = Array.from(list.querySelectorAll("[role=tab]"));
      const select = (tab, focus) => {
        tabs.forEach((other) => {
          const on = other === tab;
          other.setAttribute("aria-selected", on ? "true" : "false");
          other.tabIndex = on ? 0 : -1;
          const panel = document.getElementById(other.getAttribute("aria-controls"));
          if (panel) panel.hidden = !on;
        });
        if (focus) tab.focus();
      };
      tabs.forEach((tab, index) => {
        tab.addEventListener("click", () => select(tab, false));
        tab.addEventListener("keydown", (event) => {
          const moves = { ArrowRight: 1, ArrowLeft: -1, Home: -index, End: tabs.length - 1 - index };
          if (!(event.key in moves)) return;
          event.preventDefault();
          select(tabs[(index + moves[event.key] + tabs.length) % tabs.length], true);
        });
      });
    });
  }

  function copyButtons() {
    document.querySelectorAll(".copy-button").forEach((button) => {
      const block = button.closest(".command-block");
      const command = document.getElementById(button.dataset.copyTarget || "") || (block && block.querySelector("pre code"));
      const status = (block && block.querySelector(".copy-status")) || document.getElementById("copy-status");
      if (!command) return;
      const say = (text) => { if (status) status.textContent = text; };
      button.addEventListener("click", async () => {
        button.disabled = true;
        try {
          if (!navigator.clipboard) throw new Error("Clipboard unavailable");
          await navigator.clipboard.writeText(command.textContent.trim());
          say("Copied. Paste it into your terminal when you're ready.");
        } catch {
          const range = document.createRange();
          range.selectNodeContents(command);
          const selection = window.getSelection();
          selection.removeAllRanges();
          selection.addRange(range);
          say("Command selected. Copy it with your device's copy action.");
        } finally {
          button.disabled = false;
        }
      });
    });
  }

  function start() {
    revealOnScroll();
    demoTabs();
    copyButtons();
  }
  if (document.readyState === "loading") document.addEventListener("DOMContentLoaded", start);
  else start();
})();
