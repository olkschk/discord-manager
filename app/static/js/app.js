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
      // Show the server's detail message when available (e.g. wrong-format warning)
      const detail = r.data?.detail ?? `Error ${r.status}`;
      asResult(result, false, detail);
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

// ── Inbox modal ──────────────────────────────────────────────────────────
const inboxModal = document.getElementById("inboxModal");
const inboxBody = document.getElementById("inboxBody");
const inboxTitle = document.getElementById("inboxTitle");
let inboxAccountId = null;

function escapeHtml(s) {
  return String(s).replace(/[&<>"']/g, c => ({"&":"&amp;","<":"&lt;",">":"&gt;",'"':"&quot;","'":"&#39;"}[c]));
}

function renderInbox(data) {
  if (!data.ok) {
    const note =
      data.error === "no_imap_host" ? `No IMAP host mapped for "${data.domain || "?"}". Set IMAP_DEFAULT_HOST in .env or extend PROVIDER_HOSTS.` :
      data.error?.startsWith("imap_login_failed") ? "IMAP login failed — check the email password is correct." :
      data.error;
    inboxBody.innerHTML = `<p class="messages__empty">${escapeHtml(note)}</p>`;
    return;
  }
  if (!data.entries.length) {
    inboxBody.innerHTML = `<p class="messages__empty">No matching messages</p>`;
    return;
  }
  inboxBody.innerHTML = data.entries.map(e => `
    <article class="msg">
      <header class="msg__head">
        <span class="msg__from">${escapeHtml(e.from_ || "?")}</span>
        <span class="msg__time">${escapeHtml(e.date || "")}</span>
        ${e.code ? `<button type="button" class="small primary inbox-copy" data-code="${escapeHtml(e.code)}">Copy ${escapeHtml(e.code)}</button>` : ""}
      </header>
      <p class="msg__text"><strong>${escapeHtml(e.subject || "(no subject)")}</strong></p>
      <p class="msg__text" style="color: var(--text-secondary)">${escapeHtml(e.snippet || "")}</p>
      ${e.link ? `<p class="msg__text"><a href="${escapeHtml(e.link)}" target="_blank" rel="noopener">Open verification link →</a></p>` : ""}
    </article>
  `).join("");
  inboxBody.querySelectorAll(".inbox-copy").forEach(b => {
    b.addEventListener("click", async () => {
      try {
        await navigator.clipboard.writeText(b.dataset.code);
        b.textContent = "Copied ✓";
        setTimeout(() => { b.textContent = `Copy ${b.dataset.code}`; }, 1500);
      } catch { /* clipboard denied */ }
    });
  });
}

async function loadInbox() {
  if (!inboxAccountId) return;
  inboxBody.innerHTML = '<p class="messages__empty">Loading…</p>';
  const r = await fetch(`/api/accounts/${inboxAccountId}/inbox?limit=10`);
  const data = await r.json().catch(() => ({ ok: false, error: "parse_error" }));
  renderInbox(data);
}

function openInbox(id, label) {
  inboxAccountId = id;
  inboxTitle.textContent = `Inbox · ${label || ""}`;
  inboxModal.hidden = false;
  loadInbox();
}

function closeInbox() {
  inboxModal.hidden = true;
  inboxAccountId = null;
  inboxBody.innerHTML = "";
}

document.getElementById("inboxClose")?.addEventListener("click", closeInbox);
document.getElementById("inboxRefresh")?.addEventListener("click", loadInbox);
inboxModal?.addEventListener("click", (e) => { if (e.target === inboxModal) closeInbox(); });
document.addEventListener("keydown", (e) => { if (e.key === "Escape" && !inboxModal.hidden) closeInbox(); });

// ── Group selector ────────────────────────────────────────────────────────
document.querySelectorAll(".group-select").forEach(sel => {
  sel.addEventListener("change", async () => {
    await fetch(`/api/accounts/${sel.dataset.id}/group`, {
      method: "PATCH",
      headers: {"Content-Type": "application/json"},
      body: JSON.stringify({ group: sel.value }),
    });
  });
});

// ── Row actions ──────────────────────────────────────────────────────────
document.querySelectorAll("table.accounts tbody tr[data-id]").forEach((row) => {
  const id = row.dataset.id;
  const email = row.querySelector(".email")?.textContent.trim() || "";
  row.querySelector(".row-validate")?.addEventListener("click", async () => {
    const r = await postForm(`/api/accounts/${id}/validate`, new FormData());
    if (r.ok) location.reload();
  });
  // row-verify handled via delegation below
  row.querySelector(".row-relogin")?.addEventListener("click", async (e) => {
    const btn = e.target;
    btn.disabled = true; btn.textContent = "…";
    const resp = await fetch(`/api/accounts/${id}/login-by-mail`, { method: "POST" });
    const data = await resp.json().catch(() => ({}));
    btn.disabled = false; btn.textContent = "Login";
    if (data.ok) location.reload();
    else alert("Login failed: " + (data.error || resp.status));
  });
  row.querySelector(".row-inbox")?.addEventListener("click", () => openInbox(id, email));
  row.querySelector(".row-reset")?.addEventListener("click", async (e) => {
    const newPw = prompt(`New password for ${email}? (min 8 chars)\n\nDiscord will mail a reset link; we'll fetch it via IMAP and apply.`);
    if (!newPw || newPw.length < 8) return;
    if (!confirm(`This will RESET ${email}'s password to your input. Continue?`)) return;
    const btn = e.target;
    btn.disabled = true; btn.textContent = "…";
    const resp = await fetch(`/api/accounts/${id}/reset-password`, {
      method: "POST",
      headers: {"Content-Type": "application/json"},
      body: JSON.stringify({ new_password: newPw }),
    });
    const data = await resp.json().catch(() => ({}));
    btn.disabled = false; btn.textContent = "Reset pw";
    if (data.ok) {
      alert(`Password reset OK${data.rotated_token ? " (Discord also rotated the token)" : ""}.`);
      location.reload();
    } else {
      alert("Reset failed: " + (data.error || resp.status));
    }
  });
  row.querySelector(".row-delete")?.addEventListener("click", async () => {
    if (!confirm("Remove this account?")) return;
    const resp = await fetch(`/api/accounts/${id}`, { method: "DELETE" });
    if (resp.ok) location.reload();
  });
});

// Event delegation for .row-verify (more reliable than direct querySelector)
document.addEventListener("click", async (e) => {
  const btn = e.target.closest(".row-verify");
  if (!btn) return;
  const row = btn.closest("tr[data-id]");
  if (!row) return;
  const id = row.dataset.id;
  btn.disabled = true;
  btn.textContent = "Verifying…";
  try {
    const resp = await fetch(`/api/accounts/${id}/verify-email`, { method: "POST" });
    const data = await resp.json().catch(() => ({}));
    if (data.ok) { alert("Email verified ✓"); location.reload(); }
    else alert("Verify failed: " + (data.error || resp.status));
  } catch (err) {
    alert("Network error: " + err.message);
  } finally {
    btn.disabled = false;
    btn.textContent = "Verify";
  }
});
