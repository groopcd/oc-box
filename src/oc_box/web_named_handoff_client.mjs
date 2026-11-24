// SPDX-License-Identifier: GPL-3.0-or-later
// Source only. Trusted account page transfers an opaque PAKE ticket once.
const HANDOFF_PATH = "/oc-box/handoff";
const REFUSAL = "Named sign-in refused.";
const TOKEN = /^[A-Za-z0-9_-]{53}[AQgw]$/;

function port(origin, host) {
  const parsed = new URL(origin);
  const port = Number(parsed.port);
  if (parsed.protocol !== "https:" || parsed.origin !== origin || parsed.hostname !== host ||
      port < 1024 || port > 65535) throw new Error(REFUSAL);
  return port;
}

export function createWorkspaceTransfer({ accountOrigin, workspaceOrigin }) {
  const page = document;
  const current = () => window === top && page === document && isSecureContext === true &&
    location.origin === accountOrigin && !navigator.serviceWorker?.controller;
  try {
    if (!current() || port(accountOrigin, "accounts.ocbox.test") ===
      port(workspaceOrigin, "workspace.ocbox-app.test")) throw new Error(REFUSAL);
  } catch {
    throw new Error(REFUSAL);
  }
  const destination = workspaceOrigin + HANDOFF_PATH;
  return carrier => {
    let value, field, form;
    try {
      if (!current()) throw new Error(REFUSAL);
      value = carrier.take();
      if (typeof value !== "string" || value.length !== 54 || !TOKEN.test(value)) throw new Error(REFUSAL);
      form = page.createElement("form");
      form.method = "post";
      form.action = destination;
      form.enctype = "application/x-www-form-urlencoded";
      form.target = "_self";
      form.hidden = true;
      field = page.createElement("input");
      field.type = "hidden";
      field.name = "ticket";
      field.value = value;
      form.append(field);
      page.body.append(form);
      HTMLFormElement.prototype.submit.call(form);
    } catch {
      throw new Error(REFUSAL);
    } finally {
      value = undefined;
      if (field) field.value = "";
      form?.remove();
    }
  };
}
