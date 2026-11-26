// The page's behaviour. All text from the model is inserted with textContent, never as HTML,
// so a reply cannot run scripts in this page.
"use strict";

const byId = (id) => document.getElementById(id);
let providers = [];

async function callServer(path, body) {
  const options = body && {
    method: "POST",
    headers: { "Content-Type": "application/json" },
    body: JSON.stringify(body),
  };
  const response = await fetch(path, options);
  const data = await response.json().catch(() => ({}));
  if (!response.ok) {
    throw new Error(data.detail || `The request failed (${response.status}).`);
  }
  return data;
}

function element(tag, text, className) {
  const node = document.createElement(tag);
  if (text !== undefined) node.textContent = text;
  if (className) node.className = className;
  return node;
}

const chosenProvider = () => providers.find((p) => p.name === byId("provider").value);
const chosenKind = () => document.querySelector("input[name=kind]:checked").value;

function showStatus(message, isError = false) {
  byId("status").textContent = message;
  byId("status").className = isError ? "error" : "";
}

function showProvider() {
  const provider = chosenProvider();
  // A key typed for one provider must never be sent to another.
  byId("api-key").value = "";
  byId("model").value = "";
  byId("models").replaceChildren(
    ...[...new Set([provider.large_model, provider.small_model])].map((name) => {
      const option = element("option");
      option.value = name;
      return option;
    }),
  );
  byId("key-row").hidden = !provider.needs_key;
  let help = "No key is needed. The model runs on this computer.";
  if (provider.needs_key) {
    help = provider.has_saved_key
      ? "A key is saved on this computer. Leave this empty to use it, or type another."
      : "Enter your key. It is used for this request and not saved.";
  }
  byId("key-help").textContent = help;
}

function updateButtons(busy = false) {
  byId("review").disabled = busy;
  // Tests need whole functions, which only pasted code provides.
  byId("gentests").disabled = busy || chosenKind() !== "python";
}

function usageLine(usage) {
  const cost = usage.cost_usd === null ? "unknown" : `$${usage.cost_usd.toFixed(6)}`;
  return (
    `${usage.model} · ${usage.input_tokens} input and ${usage.output_tokens} output tokens` +
    ` · ${usage.seconds} s · cost ${cost}`
  );
}

function showComments(data) {
  if (data.comments.length === 0) {
    return [element("p", "No defects were reported. That does not prove there are none.")];
  }
  const list = element("ul");
  for (const comment of data.comments) {
    const item = element("li");
    item.append(
      element("span", comment.severity, `severity ${comment.severity}`),
      element("strong", `${comment.file}, line ${comment.line}`),
      element("p", comment.message),
    );
    list.append(item);
  }
  return [list];
}

function showTests(data) {
  if (data.test_code === null) {
    return [element("p", "No Python functions were found, so no tests were written.")];
  }
  const copy = element("button", "Copy tests");
  copy.type = "button";
  copy.addEventListener("click", async () => {
    await navigator.clipboard.writeText(data.test_code);
    copy.textContent = "Copied";
  });
  return [
    element("p", "Read these before running them. They import the code as snippet.py."),
    copy,
    element("pre", data.test_code),
  ];
}

async function run(path, render) {
  const provider = chosenProvider();
  if (!byId("text").value.trim()) {
    showStatus("Paste some code or a diff first.", true);
    return;
  }
  if (provider.needs_key && !provider.has_saved_key && !byId("api-key").value.trim()) {
    showStatus("Enter an API key for this provider.", true);
    return;
  }
  updateButtons(true);
  byId("results").replaceChildren();
  showStatus("Waiting for the model…");
  try {
    const data = await callServer(path, {
      provider: provider.name,
      model: byId("model").value,
      api_key: byId("api-key").value,
      kind: chosenKind(),
      text: byId("text").value,
    });
    byId("results").replaceChildren(...render(data));
    if (data.usage) byId("results").append(element("p", usageLine(data.usage), "help"));
    showStatus(data.usage ? "Done." : "Nothing to send: there are no changed lines to check.");
  } catch (error) {
    showStatus(error.message, true);
  } finally {
    updateButtons(false);
  }
}

async function start() {
  byId("job").addEventListener("submit", (event) => {
    event.preventDefault();
    run("/api/review", showComments);
  });
  byId("gentests").addEventListener("click", () => run("/api/gentests", showTests));
  byId("provider").addEventListener("change", showProvider);
  for (const radio of document.querySelectorAll("input[name=kind]")) {
    radio.addEventListener("change", () => updateButtons());
  }
  try {
    providers = await callServer("/api/providers");
  } catch (error) {
    showStatus(`Could not load the providers: ${error.message}`, true);
    return;
  }
  byId("provider").replaceChildren(
    ...providers.map((provider) => element("option", provider.name)),
  );
  showProvider();
  updateButtons();
}

start();
