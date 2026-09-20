(function () {
  var KEY = "mw_legal_v1";
  if (localStorage.getItem(KEY) === "agreed") return;

  var overlay = document.createElement("div");
  overlay.id = "legal-gate";
  overlay.innerHTML =
    '<div class="legal-card">' +
    '<p class="legal-kicker">Public Alpha</p>' +
    "<h2>Agree to use Maxwell</h2>" +
    "<p>The hosted bot and this site are Public Alpha. Expect bugs, downtime, lost memory, usage limits, and possible paid access later. The software is MIT open source; these terms cover <em>this</em> hosted instance.</p>" +
    '<label class="legal-check"><input id="legal-box" type="checkbox"> I am 13+, I have read and agree to the <a href="/terms/" target="_blank" rel="noopener">Terms of Service</a> and <a href="/privacy/" target="_blank" rel="noopener">Privacy Policy</a>.</label>' +
    '<p id="legal-hint" class="legal-hint"></p>' +
    '<button id="legal-agree" type="button">Agree and continue</button>' +
    "</div>";
  document.body.appendChild(overlay);
  document.body.classList.add("legal-locked");

  overlay.querySelector("#legal-agree").addEventListener("click", function () {
    var box = overlay.querySelector("#legal-box");
    if (!box.checked) {
      overlay.querySelector("#legal-hint").textContent =
        "Check the box after you read the terms.";
      return;
    }
    localStorage.setItem(KEY, "agreed");
    overlay.remove();
    document.body.classList.remove("legal-locked");
  });
})();
