// Shared Manager theme, applied before first paint. localStorage can throw,
// so every access is guarded and an unstored choice still holds for this page.
(function () {
  var KEY = "sc-theme";
  var root = document.documentElement;
  var media = window.matchMedia ? window.matchMedia("(prefers-color-scheme: light)") : null;
  var held = null;   // this page's choice when storage is unavailable

  function valid(value) {
    return value === "light" || value === "dark" ? value : "system";
  }
  function choice() {
    if (held) return held;
    try { return valid(window.localStorage.getItem(KEY)); }
    catch (e) { return "system"; }
  }
  function resolved() {
    var picked = choice();
    if (picked !== "system") return picked;
    return media && media.matches ? "light" : "dark";
  }
  function apply() {
    var theme = resolved();
    root.setAttribute("data-theme", theme);
    root.setAttribute("data-theme-choice", choice());
    return theme;
  }
  function set(next) {
    var picked = valid(next);
    held = null;
    try {
      if (picked === "system") window.localStorage.removeItem(KEY);
      else window.localStorage.setItem(KEY, picked);
    } catch (e) { held = picked; }
    return apply();
  }

  apply();
  // System follows the OS live; a saved Light or Dark ignores the change.
  if (media) {
    if (media.addEventListener) media.addEventListener("change", apply);
    else if (media.addListener) media.addListener(apply);
  }
  // Another tab of this GUI changed the choice.
  window.addEventListener("storage", function (event) {
    if (event.key === KEY || event.key === null) apply();
  });
  window.scTheme = { choice: choice, resolved: resolved, set: set };

  function bind() {
    var buttons = Array.from(document.querySelectorAll("button[data-theme-choice]"));
    if (!buttons.length) return;
    function paint() {
      var selected = choice();
      for (var button of buttons) button.setAttribute("aria-pressed", String(button.dataset.themeChoice === selected));
    }
    for (const button of buttons) button.addEventListener("click", function () {
      set(button.dataset.themeChoice);
      paint();
    });
    window.addEventListener("storage", paint);
    paint();
  }
  if (document.readyState === "loading") document.addEventListener("DOMContentLoaded", bind);
  else bind();
})();
