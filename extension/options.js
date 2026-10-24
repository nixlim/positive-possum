// Options page: backend URL and demo mode, kept in chrome.storage.sync.
const backendInput = document.getElementById("backend");
const demoInput = document.getElementById("demo");
const savedMark = document.getElementById("saved");

loadSettings().then((s) => {
  backendInput.value = s.backend;
  demoInput.checked = s.demo;
});

document.getElementById("save").addEventListener("click", async () => {
  const backend = (backendInput.value.trim() || DEFAULT_SETTINGS.backend).replace(/\/+$/, "");
  try {
    new URL(backend);
  } catch {
    savedMark.textContent = "not a URL";
    return;
  }
  await chrome.storage.sync.set({ backend, demo: demoInput.checked });
  backendInput.value = backend;
  savedMark.textContent = "saved";
});
