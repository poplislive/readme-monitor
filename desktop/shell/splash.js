// The shell shows its state here through the fragment (#starting, #reconnecting, #failed:<message>);
// the page has no Tauri access at all. "Try again" is a plain link the shell recognises.
(function () {
  var status = document.getElementById("status"), detail = document.getElementById("detail"),
      spinner = document.getElementById("spinner"), retry = document.getElementById("retry");
  function show() {
    var h = decodeURIComponent(location.hash.slice(1) || "starting");
    var failed = h.indexOf("failed:") === 0;
    status.textContent = failed ? "ReadmeMonitor could not start its background helper." :
      h === "reconnecting" ? "Reconnecting to the background helper…" : "Starting…";
    detail.hidden = !failed;
    detail.textContent = failed ? h.slice(7) : "";
    spinner.hidden = failed;
    retry.hidden = !failed;
    if (failed) retry.focus();
  }
  window.addEventListener("hashchange", show);
  show();
})();
