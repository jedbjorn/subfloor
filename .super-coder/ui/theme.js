// Theme, applied before first paint (spec #270). Loaded from <head> as a
// same-origin classic script because the CSP forbids inline script. It sets
// <html data-theme="light|dark"> — the one hook style.css keys its tokens on —
// from the operator's saved choice, else the OS `prefers-color-scheme`.
//
// window.scTheme is the whole contract for app.js:
//   choice()        -> "system" | "light" | "dark"   (what the operator picked)
//   resolved()      -> "light" | "dark"              (what is painted)
//   set(choice)     -> stores the choice and repaints; returns resolved()
// localStorage can throw (private mode, blocked site data): every access is
// guarded, and a choice that cannot be stored still holds for this page.
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
})();
