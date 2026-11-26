// The page's behaviour. All text from the model is inserted with textContent, never as HTML,
// so a reply cannot run scripts in this page.
"use strict";

const byId = (id) => document.getElementById(id);
const LOAD_LIMIT_SECONDS = 120; // downloading and starting Python
const RUN_LIMIT_SECONDS = 30; // running the tests
let providers = [];
let hosted = false; // true on a shared server
let waiting = false; // a model request is in progress
let testing = false; // a test run is in progress

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
    if (hosted) help = "Enter your own key. It is sent to this server and not stored.";
  }
  byId("key-help").textContent = help;
}

function updateButtons() {
  const python = chosenKind() === "python";
  byId("review").disabled = waiting;
  // Writing and running tests need whole functions, which only pasted code provides.
  byId("gentests").disabled = waiting || !python;
  byId("run-tests").disabled = testing || !python;
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
  // Put into the box, not run: you can read and change them first.
  byId("tests").value = data.test_code;
  return [element("p", "The tests are in the box below. Read them, then run them.")];
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
  waiting = true;
  updateButtons();
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
    waiting = false;
    updateButtons();
  }
}

function showTestStatus(message, isError = false) {
  byId("test-status").textContent = message;
  byId("test-status").className = isError ? "error" : "";
}

function showTestReport(report) {
  const count = (outcome) => report.results.filter((r) => r.outcome === outcome).length;
  const summary = ["passed", "failed", "error", "skipped"]
    .filter(count)
    .map((outcome) => `${count(outcome)} ${outcome}`)
    .join(", ");
  showTestStatus(summary || "No tests were found. Test names must start with test_.");

  const list = element("ul");
  for (const result of report.results) {
    const item = element("li");
    item.append(
      element("span", result.outcome, `severity ${result.outcome}`),
      element("strong", result.name),
    );
    if (result.message) item.append(element("pre", result.message));
    list.append(item);
  }
  const details = element("details");
  details.append(element("summary", "Full pytest output"), element("pre", report.output));
  byId("test-results").replaceChildren(list, details);
}

async function runTests() {
  const code = byId("text").value;
  const tests = byId("tests").value;
  if (!code.trim() || !tests.trim()) {
    showTestStatus("Both the Python code and the tests are needed.", true);
    return;
  }
  testing = true;
  updateButtons();
  byId("test-results").replaceChildren();
  showTestStatus("Starting Python in the browser…");

  let harness;
  try {
    const response = await fetch("/static/harness.py");
    if (!response.ok) throw new Error(`status ${response.status}`);
    harness = await response.text();
  } catch (error) {
    showTestStatus(`Could not load the test runner: ${error.message}`, true);
    testing = false;
    updateButtons();
    return;
  }

  // A new worker for every run, so nothing is left over from the last one.
  const worker = new Worker("/static/runner.js", { type: "module" });
  let timer;
  const finish = () => {
    clearTimeout(timer);
    // Stopping the worker is the only way to end an endless loop.
    worker.terminate();
    testing = false;
    updateButtons();
  };
  const limit = (seconds, message) => {
    clearTimeout(timer);
    timer = setTimeout(() => {
      finish();
      showTestStatus(message, true);
    }, seconds * 1000);
  };

  worker.onmessage = (event) => {
    if (event.data.type === "running") {
      showTestStatus("Running the tests…");
      limit(RUN_LIMIT_SECONDS, `Stopped: the tests ran for over ${RUN_LIMIT_SECONDS} seconds.`);
      return;
    }
    finish();
    if (event.data.type === "done") {
      showTestReport(JSON.parse(event.data.report));
    } else {
      showTestStatus(`The tests could not run: ${event.data.message}`, true);
    }
  };
  worker.onerror = () => {
    finish();
    showTestStatus("Python could not be loaded. Check your internet connection.", true);
  };
  limit(LOAD_LIMIT_SECONDS, "Stopped: Python took too long to download and start.");
  worker.postMessage({ harness, code, tests });
}

async function start() {
  byId("job").addEventListener("submit", (event) => {
    event.preventDefault();
    run("/api/review", showComments);
  });
  byId("gentests").addEventListener("click", () => run("/api/gentests", showTests));
  byId("run-tests").addEventListener("click", runTests);
  byId("provider").addEventListener("change", showProvider);
  for (const radio of document.querySelectorAll("input[name=kind]")) {
    radio.addEventListener("change", () => updateButtons());
  }
  try {
    hosted = (await callServer("/api/config")).hosted;
    providers = await callServer("/api/providers");
  } catch (error) {
    showStatus(`Could not load the providers: ${error.message}`, true);
    return;
  }
  byId("privacy").hidden = !hosted;
  byId("provider").replaceChildren(
    ...providers.map((provider) => element("option", provider.name)),
  );
  showProvider();
  updateButtons();
}

start();
