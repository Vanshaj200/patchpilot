// A background worker that runs tests with Pyodide, which is Python compiled for browsers.
//
// The code runs inside the browser's sandbox: it cannot touch your files. The server sends this
// worker a content policy that lets it connect to the Pyodide download address only, so code
// under test cannot call this app (and your saved API keys) or send data elsewhere.

const PYODIDE = "https://cdn.jsdelivr.net/pyodide/v314.0.8/full/";

self.onmessage = async (event) => {
  const { harness, code, tests } = event.data;
  try {
    // Imported here, not at the top of the file, so that this worker's own content policy
    // governs the download and the page's policy can stay limited to this app.
    const { loadPyodide } = await import(`${PYODIDE}pyodide.mjs`);
    const python = await loadPyodide({ indexURL: PYODIDE });
    await python.loadPackage("pytest");
    // The page starts its time limit now, so a slow download does not count against the tests.
    self.postMessage({ type: "running" });
    python.runPython(harness);
    const run = python.globals.get("run");
    self.postMessage({ type: "done", report: run(code, tests, "/work") });
  } catch (error) {
    self.postMessage({ type: "error", message: String(error) });
  }
};
