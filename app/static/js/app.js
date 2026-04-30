// Lightweight client glue: form-submit fetch + row actions.

function asResult(el, ok, msg) {
  el.classList.remove("ok", "err");
  el.classList.add(ok ? "ok" : "err");
  el.textContent = msg;
}

async function postForm(url, formData) {
  const resp = await fetch(url, { method: "POST", body: formData });
  const text = await resp.text();
  let data;
  try { data = JSON.parse(text); } catch { data = { _raw: text }; }
  return { ok: resp.ok, status: resp.status, data };
}

function bindAddForm(formId, resultId, url, label) {
  const form = document.getElementById(formId);
  if (!form) return;
  const result = document.getElementById(resultId);
  form.addEventListener("submit", async (e) => {
    e.preventDefault();
    const fd = new FormData(form);
    asResult(result, true, "…");
    const r = await postForm(url, fd);
    if (!r.ok) {
      asResult(result, false, `Error ${r.status}`);
      return;
    }
    const { added = 0, skipped = 0, errors = [] } = r.data;
    asResult(
      result,
      added > 0 || errors.length === 0,
      `${label}: added ${added}, skipped ${skipped}` + (errors.length ? ` — ${errors.length} error(s)` : "")
    );
    if (added > 0) setTimeout(() => location.reload(), 600);
  });
}

bindAddForm("addAccountsForm", "addAccountsResult", "/api/accounts/add", "Accounts");
bindAddForm("addProxiesForm", "addProxiesResult", "/api/proxies/add", "Proxies");

document.getElementById("assignProxiesBtn")?.addEventListener("click", async (e) => {
  e.target.disabled = true;
  const r = await postForm("/api/accounts/assign-proxies", new FormData());
  e.target.disabled = false;
  if (r.ok) location.reload();
  else alert("Assign failed: " + r.status);
});

document.getElementById("validateAllBtn")?.addEventListener("click", async (e) => {
  e.target.disabled = true;
  e.target.textContent = "Validating…";
  const r = await postForm("/api/accounts/validate-all", new FormData());
  e.target.disabled = false;
  e.target.textContent = "Validate all";
  if (r.ok) location.reload();
  else alert("Validate failed: " + r.status);
});

document.querySelectorAll("table.accounts tbody tr[data-id]").forEach((row) => {
  const id = row.dataset.id;
  row.querySelector(".row-validate")?.addEventListener("click", async () => {
    const r = await postForm(`/api/accounts/${id}/validate`, new FormData());
    if (r.ok) location.reload();
  });
  row.querySelector(".row-relogin")?.addEventListener("click", async (e) => {
    const btn = e.target;
    btn.disabled = true; btn.textContent = "…";
    const resp = await fetch(`/api/accounts/${id}/login-by-mail`, { method: "POST" });
    const data = await resp.json().catch(() => ({}));
    btn.disabled = false; btn.textContent = "Login";
    if (data.ok) location.reload();
    else alert("Login failed: " + (data.error || resp.status));
  });
  row.querySelector(".row-delete")?.addEventListener("click", async () => {
    if (!confirm("Remove this account?")) return;
    const resp = await fetch(`/api/accounts/${id}`, { method: "DELETE" });
    if (resp.ok) location.reload();
  });
});
