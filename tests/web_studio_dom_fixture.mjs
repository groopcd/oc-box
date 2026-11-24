// Minimal Studio controls for tests focused on native routing or attachments.
// The markup-backed layout test exercises the real toolbars and wide panes.
export function addStudioControls(elements, create, window) {
  for (const id of ["layouts", "layout-name", "layout-description", "manager-nav", "search", "library",
    "sessions-toggle", "sessions-close", "inspector-toggle", "inspector-close", "inspector-link", "inspector-context",
    "open-tabs", "history-tabs", "archived-tabs", "open-group", "history-group", "archived-group",
    "history-list", "archived-list", "content", "divider-left", "divider-right", "chat", "overview",
    "overview-title", "search-status", "conversation-title", "conversation-source", "current-session"]) {
    elements["workspace-" + id] = create("workspace-" + id);
  }
  elements["fallback-links"] = create("fallback-links");
  elements["workspace-search"].value = "";
  elements["workspace-layouts"].querySelectorAll = () => [];
  elements["workspace-manager-nav"].querySelectorAll = () => [];
  window.matchMedia = () => ({ matches: false, addEventListener() {} });
}
