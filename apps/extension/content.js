(function injectAddToResumeIQButton() {
  if (document.getElementById("resumeiq-floating-btn")) return;

  const btn = document.createElement("button");
  btn.id = "resumeiq-floating-btn";
  btn.type = "button";
  btn.textContent = "Add to ResumeIQ";
  Object.assign(btn.style, {
    position: "fixed",
    bottom: "20px",
    right: "20px",
    zIndex: "2147483647",
    backgroundColor: "#0F5C6E",
    color: "#FFFFFF",
    border: "1px solid #0B4654",
    borderRadius: "6px",
    padding: "10px 14px",
    fontFamily: "Inter, system-ui, sans-serif",
    fontSize: "13px",
    fontWeight: "600",
    cursor: "pointer",
    boxShadow: "0 2px 8px rgba(31, 41, 51, 0.15)",
  });

  btn.addEventListener("click", async () => {
    chrome.storage.local.get(["resumeiq_api_url", "resumeiq_token"], async (items) => {
      const apiUrl = (items.resumeiq_api_url || "http://localhost:8000/api/v1").replace(/\/$/, "");
      const token = items.resumeiq_token;
      if (!token) {
        alert("Open the ResumeIQ extension popup once to save your session token.");
        return;
      }
      btn.textContent = "Saving to ResumeIQ...";
      btn.disabled = true;
      try {
        const resp = await fetch(`${apiUrl}/extension/capture-job`, {
          method: "POST",
          headers: {
            "Content-Type": "application/json",
            Authorization: `Bearer ${token}`,
          },
          body: JSON.stringify({
            url: window.location.href,
            page_title: document.title,
            raw_text: document.body.innerText.slice(0, 40000),
          }),
        });
        const data = await resp.json();
        if (!resp.ok) {
          throw new Error(data.detail?.message || data.detail || "Capture failed");
        }
        btn.textContent = "Added to ResumeIQ";
        window.open(`http://localhost:3000/applications/new?job_id=${encodeURIComponent(data.id)}`, "_blank");
      } catch (err) {
        alert("ResumeIQ capture failed: " + (err.message || err));
        btn.textContent = "Add to ResumeIQ";
      } finally {
        btn.disabled = false;
      }
    });
  });

  document.body.appendChild(btn);
})();
