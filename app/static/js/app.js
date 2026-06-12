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
document.addEventListener("keydown", (e) => { if (e.key === "Escape" && !inboxModal?.hidden) closeInbox(); });

// ── Inventory: checkbox selection ────────────────────────────────────────
function getSelectedIds() {
  return [...document.querySelectorAll(".row-check:checked")].map(cb => cb.value);
}

function updateBulkState() {
  const ids = getSelectedIds();
  const n = ids.length;
  const selCount = document.getElementById("selCount");
  if (selCount) selCount.textContent = `${n} selected`;
  ["bulkValidateBtn","bulkLoginBtn","bulkVerifyBtn","bulk2faSetupBtn","bulkGroupBtn","bulkDeleteBtn"].forEach(id => {
    const btn = document.getElementById(id);
    if (btn) btn.disabled = n === 0;
  });
}

document.getElementById("checkAll")?.addEventListener("change", (e) => {
  document.querySelectorAll(".row-check").forEach(cb => { cb.checked = e.target.checked; });
  updateBulkState();
});

document.querySelectorAll(".row-check").forEach(cb => {
  cb.addEventListener("change", () => {
    const all = document.getElementById("checkAll");
    if (all) {
      const boxes = document.querySelectorAll(".row-check");
      all.checked = [...boxes].every(b => b.checked);
      all.indeterminate = !all.checked && [...boxes].some(b => b.checked);
    }
    updateBulkState();
  });
});

document.getElementById("selectAllBtn")?.addEventListener("click", () => {
  document.querySelectorAll(".row-check").forEach(cb => { cb.checked = true; });
  const all = document.getElementById("checkAll");
  if (all) { all.checked = true; all.indeterminate = false; }
  updateBulkState();
});

document.getElementById("clearSelBtn")?.addEventListener("click", () => {
  document.querySelectorAll(".row-check").forEach(cb => { cb.checked = false; });
  const all = document.getElementById("checkAll");
  if (all) { all.checked = false; all.indeterminate = false; }
  updateBulkState();
});

// ── Inventory: bulk actions ──────────────────────────────────────────────
const bulkResult = document.getElementById("bulkResult");

async function runBulkSequential(ids, urlFn, btnEl, label) {
  if (!ids.length) return;
  btnEl.disabled = true;
  const orig = btnEl.textContent;
  let done = 0;
  for (const id of ids) {
    btnEl.textContent = `${label} ${++done}/${ids.length}…`;
    await fetch(urlFn(id), { method: "POST" });
  }
  btnEl.textContent = orig;
  btnEl.disabled = false;
  location.reload();
}

document.getElementById("bulkValidateBtn")?.addEventListener("click", async (e) => {
  const ids = getSelectedIds();
  await runBulkSequential(ids, id => `/api/accounts/${id}/validate`, e.target, "Validating");
});

document.getElementById("bulkLoginBtn")?.addEventListener("click", async (e) => {
  const ids = getSelectedIds();
  await runBulkSequential(ids, id => `/api/accounts/${id}/login-by-mail`, e.target, "Logging in");
});

document.getElementById("bulkVerifyBtn")?.addEventListener("click", async (e) => {
  const ids = getSelectedIds();
  await runBulkSequential(ids, id => `/api/accounts/${id}/verify-email`, e.target, "Verifying");
});

document.getElementById("bulkDeleteBtn")?.addEventListener("click", async (e) => {
  const ids = getSelectedIds();
  if (!ids.length) return;
  if (!confirm(`Remove ${ids.length} account(s)? This cannot be undone.`)) return;
  e.target.disabled = true;
  const orig = e.target.textContent;
  let done = 0;
  for (const id of ids) {
    e.target.textContent = `Removing ${++done}/${ids.length}…`;
    await fetch(`/api/accounts/${id}`, { method: "DELETE" });
  }
  e.target.textContent = orig;
  location.reload();
});

document.getElementById("bulkGroupBtn")?.addEventListener("click", async (e) => {
  const ids = getSelectedIds();
  const group = document.getElementById("bulkGroupSel")?.value;
  if (!ids.length || !group) return;
  e.target.disabled = true;
  const orig = e.target.textContent;
  e.target.textContent = "Setting…";
  const resp = await fetch("/api/accounts/bulk-group", {
    method: "POST",
    headers: {"Content-Type": "application/json"},
    body: JSON.stringify({ account_ids: ids, group }),
  });
  const data = await resp.json().catch(() => ({}));
  e.target.textContent = orig;
  e.target.disabled = false;
  if (resp.ok) {
    if (bulkResult) asResult(bulkResult, true, `Group set to ${group} for ${data.updated} account(s)`);
    setTimeout(() => location.reload(), 600);
  } else {
    if (bulkResult) asResult(bulkResult, false, `Error ${resp.status}`);
  }
});

// ── Inventory: bulk 2FA Setup ────────────────────────────────────────────
document.getElementById("bulk2faSetupBtn")?.addEventListener("click", async (e) => {
  const ids = getSelectedIds();
  if (!ids.length) return;
  e.target.disabled = true;
  const orig = e.target.textContent;
  let done = 0, ok = 0;
  for (const id of ids) {
    e.target.textContent = `2FA ${++done}/${ids.length}…`;
    const resp = await fetch(`/api/utils/2fa/setup`, {
      method: "POST",
      headers: {"Content-Type": "application/json"},
      body: JSON.stringify({ account_id: id }),
    });
    if (resp.ok) ok++;
  }
  e.target.textContent = orig;
  if (bulkResult) asResult(bulkResult, ok > 0, `2FA setup: ${ok}/${ids.length} succeeded`);
  setTimeout(() => location.reload(), 800);
});

// ── Inventory: per-row 2FA get-code (event delegation) ──────────────────
document.addEventListener("click", async (e) => {
  const btn = e.target.closest(".mfa-code-btn");
  if (!btn) return;
  const row = btn.closest("tr[data-id]");
  if (!row) return;
  const id = row.dataset.id;
  btn.disabled = true;
  btn.textContent = "…";
  try {
    const resp = await fetch(`/api/utils/2fa/${id}/code`);
    const data = await resp.json().catch(() => ({}));
    btn.disabled = false;
    btn.textContent = "Get code";
    if (data.code) {
      // Show code inline next to button
      const codeEl = btn.nextElementSibling;
      if (codeEl && codeEl.classList.contains("mfa-code")) {
        codeEl.textContent = data.code;
        try { await navigator.clipboard.writeText(data.code); } catch { /* denied */ }
      } else {
        alert("2FA code: " + data.code);
      }
    } else {
      alert("Failed: " + (data.error || resp.status));
    }
  } catch (err) {
    btn.disabled = false;
    btn.textContent = "Get code";
    alert("Network error: " + err.message);
  }
});

// ── Inventory: password show/hide toggle ─────────────────────────────────
document.addEventListener("click", (e) => {
  const btn = e.target.closest(".pw-toggle");
  if (!btn) return;
  const cell = btn.closest(".pw-cell");
  if (!cell) return;
  const dots = cell.querySelector(".pw-dots");
  const text = cell.querySelector(".pw-text");
  if (!dots || !text) return;
  const visible = text.style.display !== "none";
  dots.style.display = visible ? "" : "none";
  text.style.display = visible ? "none" : "";
  btn.textContent = visible ? "show" : "hide";
});

// ── Row actions ──────────────────────────────────────────────────────────
document.querySelectorAll("table.accounts tbody tr[data-id]").forEach((row) => {
  const id = row.dataset.id;
  const email = row.querySelector(".email")?.textContent.trim() || "";
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
});

// ── Status (presence) dropdown ───────────────────────────────────────────
document.addEventListener("change", async (e) => {
  const sel = e.target.closest(".row-status");
  if (!sel) return;
  const id = sel.dataset.id;
  const status = sel.value;
  sel.disabled = true;
  try {
    const resp = await fetch("/api/utils/status", {
      method: "POST",
      headers: {"Content-Type": "application/json"},
      body: JSON.stringify({ account_id: id, status }),
    });
    const data = await resp.json().catch(() => ({}));
    if (!data.ok) {
      alert("Failed to set status: " + (data.error || resp.status));
    }
  } finally {
    sel.disabled = false;
  }
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

// ── Inventory filter + sort ───────────────────────────────────────────────────
(function () {
  const searchInput = document.getElementById("inventorySearch");
  const groupBtns = document.querySelectorAll(".inv-group-btn");
  const countEl = document.getElementById("inventoryCount");
  if (!searchInput) return;

  let activeGroup = "All";

  function applyFilters() {
    const q = searchInput.value.trim().toLowerCase();
    const rows = document.querySelectorAll(".accounts tbody tr[data-id]");
    let visible = 0;
    rows.forEach(row => {
      const matchGroup = activeGroup === "All" || row.dataset.group === activeGroup;
      const matchSearch = !q ||
        (row.dataset.email || "").includes(q) ||
        (row.dataset.username || "").includes(q) ||
        (row.dataset.name || "").includes(q);
      const show = matchGroup && matchSearch;
      row.style.display = show ? "" : "none";
      if (show) visible++;
    });
    if (countEl) countEl.textContent = `${visible} account${visible !== 1 ? "s" : ""}`;
  }

  searchInput.addEventListener("input", applyFilters);

  groupBtns.forEach(btn => {
    btn.addEventListener("click", () => {
      activeGroup = btn.dataset.group;
      groupBtns.forEach(b => {
        const on = b.dataset.group === activeGroup;
        b.style.background = on ? "var(--text-display)" : "transparent";
        b.style.color = on ? "var(--black)" : "var(--text-secondary)";
        b.style.borderColor = on ? "var(--text-display)" : "var(--border-visible)";
      });
      applyFilters();
    });
  });

  applyFilters(); // init count
})();

// ── Token modal ───────────────────────────────────────────────────────────────
(function () {
  const modal = document.getElementById("tokenModal");
  if (!modal) return;
  const valueEl = document.getElementById("tokenModalValue");
  const closeBtn = document.getElementById("tokenModalClose");
  const copyBtn = document.getElementById("tokenCopyBtn");

  document.addEventListener("click", e => {
    const btn = e.target.closest(".row-token");
    if (!btn) return;
    const token = btn.dataset.token || "";
    valueEl.value = token || "(token not available)";
    modal.hidden = false;
  });

  closeBtn.addEventListener("click", () => { modal.hidden = true; });
  modal.addEventListener("click", e => { if (e.target === modal) modal.hidden = true; });

  copyBtn.addEventListener("click", () => {
    navigator.clipboard.writeText(valueEl.value).then(() => {
      copyBtn.textContent = "Copied ✓";
      setTimeout(() => { copyBtn.textContent = "Copy"; }, 1500);
    });
  });
})();
