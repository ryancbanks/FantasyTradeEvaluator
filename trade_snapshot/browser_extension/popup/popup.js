(() => {
  "use strict";

  const status = document.querySelector("#status");
  const detail = document.querySelector("#detail");
  const dot = document.querySelector("#status-dot");
  const hint = document.querySelector("#pair-hint");
  const pairActions = document.querySelector("#pair-actions");
  const connectedActions = document.querySelector("#connected-actions");
  const accept = document.querySelector("#accept");
  const reject = document.querySelector("#reject");
  const disconnect = document.querySelector("#disconnect");

  const labels = {
    idle: ["Not paired", "Open Fantasy Trade Evaluator and choose its browser-extension connection."],
    pair_pending: ["Pairing approval needed", "Confirm that the local app address below is the app you opened."],
    pairing: ["Pairing…", "Exchanging the one-time code with the local app."],
    paired: ["Paired", "The bridge is ready for a scan command."],
    waiting: ["Paired", "Waiting for the local app's next scan command."],
    running: ["Scanning", "One dedicated browser tab is being used for the current command."],
    error: ["Connection error", "Return to the local app and create a new pairing code."]
  };

  function render(value) {
    const phase = labels[value?.phase] ? value.phase : "error";
    const [title, description] = labels[phase];
    status.textContent = title;
    detail.textContent = value?.app_origin ? `${description} App: ${value.app_origin}` : description;
    dot.className = "dot";
    if (phase === "pair_pending" || phase === "pairing") dot.classList.add("pending");
    if (["paired", "waiting", "running"].includes(phase)) dot.classList.add("connected");
    if (phase === "error") dot.classList.add("error");
    const pending = phase === "pair_pending";
    hint.hidden = !pending;
    hint.textContent = pending ? `One-time code ends in ${value.pair_hint}` : "";
    pairActions.hidden = !pending;
    connectedActions.hidden = !["paired", "waiting", "running"].includes(phase);
  }

  async function command(kind) {
    for (const button of [accept, reject, disconnect]) button.disabled = true;
    try {
      const response = await chrome.runtime.sendMessage({kind});
      if (!response?.ok) throw new Error(response?.error || "extension_unavailable");
      const current = await chrome.runtime.sendMessage({kind: "fte.popup.status"});
      if (current?.ok) render(current.value);
    } catch (_) {
      render({phase: "error"});
    } finally {
      for (const button of [accept, reject, disconnect]) button.disabled = false;
    }
  }

  accept.addEventListener("click", () => void command("fte.popup.accept"));
  reject.addEventListener("click", () => void command("fte.popup.reject"));
  disconnect.addEventListener("click", () => void command("fte.popup.disconnect"));
  chrome.runtime.onMessage.addListener((message) => {
    if (message?.kind === "fte.worker.status") render(message.status);
  });
  void command("fte.popup.status");
})();
