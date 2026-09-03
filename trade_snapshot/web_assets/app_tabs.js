"use strict";

window.AppTabs = (() => {
  const tabs = [...document.querySelectorAll('[role="tab"][data-app-surface]')];

  function activate(tab, {focus = false} = {}) {
    for (const candidate of tabs) {
      const selected = candidate === tab;
      candidate.setAttribute("aria-selected", String(selected));
      candidate.tabIndex = selected ? 0 : -1;
      const panel = document.getElementById(candidate.getAttribute("aria-controls"));
      panel.hidden = !selected;
    }
    if (focus) tab.focus();
    window.dispatchEvent(new CustomEvent("appsurfacechange", {
      detail: {surface: tab.dataset.appSurface}
    }));
  }

  function move(current, offset) {
    const index = tabs.indexOf(current);
    activate(tabs[(index + offset + tabs.length) % tabs.length], {focus: true});
  }

  for (const tab of tabs) {
    tab.addEventListener("click", () => activate(tab));
    tab.addEventListener("keydown", event => {
      if (event.key === "ArrowLeft") move(tab, -1);
      else if (event.key === "ArrowRight") move(tab, 1);
      else if (event.key === "Home") activate(tabs[0], {focus: true});
      else if (event.key === "End") activate(tabs[tabs.length - 1], {focus: true});
      else return;
      event.preventDefault();
    });
  }

  const selected = tabs.find(tab => tab.getAttribute("aria-selected") === "true");
  if (selected) activate(selected);
  return {activate};
})();
