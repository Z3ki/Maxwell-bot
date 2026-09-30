"use strict";
(() => {
  const button = document.getElementById("copy-install");
  const command = document.getElementById("install-command");
  const status = document.getElementById("copy-status");
  if (!button || !command || !status) return;
  button.addEventListener("click", async () => {
    const text = command.textContent.trim();
    button.disabled = true;
    try {
      if (!navigator.clipboard) throw new Error("Clipboard unavailable");
      await navigator.clipboard.writeText(text);
      status.textContent = "Copied. Paste it into your terminal when you're ready.";
    } catch {
      const range = document.createRange();
      range.selectNodeContents(command);
      const selection = window.getSelection();
      selection.removeAllRanges();
      selection.addRange(range);
      status.textContent = "Command selected. Copy it manually with your device's copy action.";
    } finally {
      button.disabled = false;
    }
  });
})();
