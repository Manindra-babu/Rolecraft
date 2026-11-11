const apiUrlInput = document.getElementById("apiUrl");
const tokenInput = document.getElementById("token");
const captureBtn = document.getElementById("captureBtn");
const statusDiv = document.getElementById("status");

chrome.storage.local.get(["resumeiq_api_url", "resumeiq_token"], (items) => {
  if (items.resumeiq_api_url) apiUrlInput.value = items.resumeiq_api_url;
  if (items.resumeiq_token) tokenInput.value = items.resumeiq_token;
});

function showStatus(msg, isError) {
  statusDiv.textContent = msg;
  statusDiv.className = "status " + (isError ? "err" : "ok");
}

captureBtn.addEventListener("click", async () => {
  const apiUrl = apiUrlInput.value.trim().replace(/\/$/, "");
  const token = tokenInput.value.trim();
  if (!token) {
    showStatus("Enter your ResumeIQ session token first.", true);
    return;
  }
  chrome.storage.local.set({ resumeiq_api_url: apiUrl, resumeiq_token: token });
  captureBtn.disabled = true;
  showStatus("Reading active tab...", false);

  try {
    const [tab] = await chrome.tabs.query({ active: true, currentWindow: true });
    const results = await chrome.scripting.executeScript({
      target: { tabId: tab.id },
      func: () => ({
        url: window.location.href,
        page_title: document.title,
        raw_text: document.body.innerText.slice(0, 40000),
      }),
    });

    const payload = results[0].result;
    const resp = await fetch(`${apiUrl}/extension/capture-job`, {
      method: "POST",
      headers: {
        "Content-Type": "application/json",
        Authorization: `Bearer ${token}`,
      },
      body: JSON.stringify(payload),
    });

    const body = await resp.json();
    if (!resp.ok) {
      throw new Error(body.detail?.message || body.detail || "Capture failed");
    }
    showStatus(`Captured: ${body.title || "Job posting"}! Open ResumeIQ -> New Application.`, false);
    chrome.tabs.create({ url: `http://localhost:3000/applications/new?job_id=${encodeURIComponent(body.id)}` });
  } catch (err) {
    showStatus(err.message || "Failed to send job posting.", true);
  } finally {
    captureBtn.disabled = false;
  }
});
