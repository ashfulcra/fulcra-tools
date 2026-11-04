// Read inert HTML data rather than interpolating plugin IDs into executable code.
if (window.opener) {
  window.opener.postMessage(
    {type: "oauth_complete", plugin_id: document.body.dataset.pluginId},
    window.location.origin
  );
  setTimeout(() => window.close(), 2000);
}
