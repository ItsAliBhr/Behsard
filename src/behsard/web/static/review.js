// Keyboard-driven review. Uses event.code so it works on the Persian layout.
(function () {
  let typed = "";
  function card() { return document.getElementById("card"); }
  function items() { return Array.from(document.querySelectorAll("#card .cands li")); }
  // Highlight candidates matching the typed prefix; return the matches.
  function render() {
    const box = document.getElementById("typed");
    if (box) box.value = typed;
    const matches = [];
    items().forEach(li => {
      const ok = typed === "" || li.dataset.code.startsWith(typed);
      li.classList.toggle("dim", !ok);
      li.classList.toggle("hl", typed.length > 0 && ok);
      if (typed && ok) matches.push(li);
    });
    const hint = document.getElementById("typed-hint");
    if (hint) hint.textContent = (typed.length === 3 && matches.length === 0)
      ? `مهر ${typed} در این شیفت روی این نقاله فعال نیست (Esc برای پاک کردن)` : "";
    return matches;
  }
  function submit(decision, stamp) {
    const f = document.getElementById("resolve-form");
    if (!f) return;
    f.querySelector("[name=decision]").value = decision;
    f.querySelector("[name=stamp]").value = stamp || "";
    typed = "";
    htmx.trigger(f, "submit");
  }
  window.behsardPick = (code) => submit("stamp", code);
  window.behsardDecide = (d) => submit(d, "");
  document.addEventListener("htmx:afterSwap", () => { typed = ""; render(); });
  document.addEventListener("keydown", (e) => {
    if (!card() || !document.getElementById("resolve-form")) {
      return;
    }
    if (e.ctrlKey || e.altKey || e.metaKey) return;
    const c = e.code;
    let ch = null;
    if (c.startsWith("Key")) ch = c.slice(3);
    else if (c.startsWith("Digit")) ch = c.slice(5);
    else if (c.startsWith("Numpad") && /\d$/.test(c)) ch = c.slice(6);
    if (ch !== null) {
      e.preventDefault();
      if (typed.length === 3 && render().length === 0) typed = "";  // dead input: start over
      if (typed === "" && /\d/.test(ch)) {
        if (ch === "0") { submit("no_stamp"); return; }
        const li = document.querySelector(`#card .cands li[data-key="${ch}"]`);
        if (li) submit("stamp", li.dataset.code);
        return;
      }
      if (typed.length < 3) typed += ch;
      render();  // a full code still needs Enter, so a typo can be fixed
      return;
    }
    if (c === "Backspace") { e.preventDefault(); typed = typed.slice(0, -1); render(); return; }
    if (c === "Escape") { typed = ""; render(); return; }
    if (c === "Enter" || c === "NumpadEnter") {
      e.preventDefault();
      if (typed === "") {  // empty: confirm the system's guess
        const g = document.getElementById("resolve-form").dataset.guess;
        if (g && document.querySelector(`#card .cands li[data-code="${g}"]`)) submit("stamp", g);
        return;
      }
      const m = render();
      if (m.length === 1) submit("stamp", m[0].dataset.code);
      return;
    }
    if (c === "Period" || c === "NumpadDecimal") { e.preventDefault(); submit("not_carton"); return; }
    if (c === "Space") {
      e.preventDefault();
      const skip = document.getElementById("skip-link");
      if (skip) htmx.trigger(skip, "click");
      return;
    }
  });
})();
