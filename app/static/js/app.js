/* Restream Manager - dashboard behaviour.
 *
 * Everything talks to the JSON API; the HTML is only ever refreshed through
 * HTMX partials, so no full page reload is needed while streams are running.
 */
(() => {
  "use strict";

  // ---------------------------------------------------------------- utils
  const toastHost = () => document.getElementById("toasts");

  function toast(message, variant = "secondary") {
    const host = toastHost();
    if (!host) {
      return;
    }
    const el = document.createElement("div");
    el.className = `toast align-items-center text-bg-${variant} border-0`;
    el.setAttribute("role", "alert");
    el.innerHTML = `
      <div class="d-flex">
        <div class="toast-body">${escapeHtml(message)}</div>
        <button type="button" class="btn-close btn-close-white me-2 m-auto" data-bs-dismiss="toast"></button>
      </div>`;
    host.appendChild(el);
    const instance = new bootstrap.Toast(el, { delay: 5000 });
    instance.show();
    el.addEventListener("hidden.bs.toast", () => el.remove());
  }

  function escapeHtml(value) {
    const div = document.createElement("div");
    div.textContent = value === undefined || value === null ? "" : String(value);
    return div.innerHTML;
  }

  async function api(path, options = {}) {
    const response = await fetch(path, {
      headers: { "Content-Type": "application/json" },
      ...options,
    });
    let payload = null;
    try {
      payload = await response.json();
    } catch (err) {
      payload = null;
    }
    if (!response.ok) {
      const detail =
        (payload && (payload.detail || payload.error)) ||
        `${response.status} ${response.statusText}`;
      throw new Error(typeof detail === "string" ? detail : JSON.stringify(detail));
    }
    return payload;
  }

  function formData(form) {
    const data = {};
    new FormData(form).forEach((value, key) => {
      data[key] = value;
    });
    form.querySelectorAll('input[type="checkbox"]').forEach((box) => {
      data[box.name] = box.checked;
    });
    return data;
  }

  function selectedIds() {
    return Array.from(document.querySelectorAll(".row-select:checked")).map((box) =>
      Number(box.value)
    );
  }

  function json(value) {
    return JSON.stringify(value, null, 2);
  }

  // ------------------------------------------------------- channel actions
  document.addEventListener("click", async (event) => {
    const button = event.target.closest("[data-action]");
    if (!button) {
      return;
    }
    event.preventDefault();
    const { action, id, confirm: confirmText } = button.dataset;
    if (confirmText && !window.confirm(confirmText)) {
      return;
    }
    button.disabled = true;
    try {
      if (action === "delete") {
        await api(`/api/channels/${id}`, { method: "DELETE" });
        toast("Channel deleted", "success");
        window.location.href = "/";
        return;
      }
      if (action === "test") {
        const result = await api(`/api/channels/${id}/test`, { method: "POST" });
        renderTestResult(result);
        toast(result.ok ? "Source is reachable" : `Test failed: ${result.error}`,
              result.ok ? "success" : "danger");
        return;
      }
      const result = await api(`/api/channels/${id}/${action}`, { method: "POST" });
      if (result && result.ok === false) {
        toast(result.error || "Action failed", "danger");
      } else {
        toast(`${action} requested`, "success");
      }
    } catch (err) {
      toast(err.message, "danger");
    } finally {
      button.disabled = false;
    }
  });

  function renderTestResult(result) {
    const host = document.getElementById("test-result");
    if (!host) {
      return;
    }
    const probe = result.probe || {};
    const stream = result.stream || {};
    host.innerHTML = `
      <div class="alert ${result.ok ? "alert-success" : "alert-danger"}">
        <div class="fw-semibold mb-1">
          Source reachable: ${result.ok ? "YES" : "NO"}
        </div>
        <div class="smaller">
          ${result.ok ? `
            Video codec: ${escapeHtml(probe.video_codec || "-")} ·
            Audio codec: ${escapeHtml(probe.audio_codec || "-")} ·
            Resolution: ${escapeHtml(probe.resolution || "-")} ·
            Probe time: ${escapeHtml(probe.elapsed_ms || 0)} ms<br>
            URL: <code>${escapeHtml(stream.url || "")}</code>
          ` : escapeHtml(result.error || "unknown error")}
        </div>
      </div>`;
  }

  // ---------------------------------------------------------- bulk actions
  document.addEventListener("click", async (event) => {
    const button = event.target.closest("[data-bulk]");
    if (!button) {
      return;
    }
    event.preventDefault();
    const kind = button.dataset.bulk;
    const confirmText = button.dataset.confirm;
    if (confirmText && !window.confirm(confirmText)) {
      return;
    }
    button.disabled = true;
    try {
      if (kind === "sync") {
        const providerId = button.dataset.id ? Number(button.dataset.id) : null;
        const result = await api("/api/sync", {
          method: "POST",
          body: json({ provider_id: providerId }),
        });
        const added = (result.added || []).length;
        const missing = (result.missing || []).length;
        const errors = result.errors || [];
        toast(
          `Sync complete: ${added} new, ${(result.updated || []).length} updated, ${missing} missing`,
          errors.length ? "warning" : "success"
        );
        errors.forEach((message) => toast(message, "danger"));
        if (added || missing) {
          setTimeout(() => window.location.reload(), 1200);
        }
        return;
      }

      const map = {
        "start-all": { action: "start", ids: [] },
        "stop-all": { action: "stop", ids: [] },
        "restart-selected": { action: "restart", ids: selectedIds() },
        "refresh-selected": { action: "refresh", ids: selectedIds() },
      };
      const spec = map[kind];
      if (!spec) {
        return;
      }
      if (kind.endsWith("selected") && spec.ids.length === 0) {
        toast("Select at least one channel first", "warning");
        return;
      }
      const result = await api("/api/channels/bulk", {
        method: "POST",
        body: json({ action: spec.action, channel_ids: spec.ids }),
      });
      toast(`Done (${result.started ?? result.stopped ?? result.count ?? 0} channels)`, "success");
    } catch (err) {
      toast(err.message, "danger");
    } finally {
      button.disabled = false;
    }
  });

  // select-all checkbox
  document.addEventListener("change", (event) => {
    if (event.target.id !== "select-all") {
      return;
    }
    document.querySelectorAll(".row-select").forEach((box) => {
      box.checked = event.target.checked;
    });
  });

  // language switcher
  document.addEventListener("click", async (event) => {
    const button = event.target.closest("[data-lang]");
    if (!button) {
      return;
    }
    event.preventDefault();
    try {
      await api("/api/settings", {
        method: "POST",
        body: json({ values: { ui_language: button.dataset.lang } }),
      });
      window.location.reload();
    } catch (err) {
      toast(err.message, "danger");
    }
  });

  // copy-to-clipboard
  document.addEventListener("click", async (event) => {
    const button = event.target.closest("[data-copy]");
    if (!button) {
      return;
    }
    event.preventDefault();
    try {
      await navigator.clipboard.writeText(button.dataset.copy);
      toast("Copied to clipboard", "success");
    } catch (err) {
      toast("Clipboard not available in this browser", "warning");
    }
  });

  // --------------------------------------------------------- channel forms
  const createForm = document.getElementById("channel-form");
  if (createForm) {
    createForm.addEventListener("submit", async (event) => {
      event.preventDefault();
      const payload = formData(createForm);
      payload.provider_id = payload.provider_id ? Number(payload.provider_id) : null;
      try {
        await api("/api/channels", { method: "POST", body: json(payload) });
        toast("Channel created", "success");
        window.location.reload();
      } catch (err) {
        toast(err.message, "danger");
      }
    });
  }

  // ------------------------------------------------------------ bulk add
  const bulkForm = document.getElementById("bulk-form");
  if (bulkForm) {
    const previewHost = document.getElementById("bulk-preview-output");

    function bulkPayload() {
      const data = formData(bulkForm);
      return {
        text: data.text || "",
        provider_id: data.provider_id ? Number(data.provider_id) : null,
        stream_mode: data.stream_mode || "copy",
        stream_key_prefix: data.stream_key_prefix || "",
        enabled: Boolean(data.enabled),
        auto_start: false,
      };
    }

    function renderPreview(result) {
      if (!result.count) {
        previewHost.innerHTML = `<div class="alert alert-warning py-2 mb-0 smaller">
          ${escapeHtml((result.errors || ["nothing to import"]).join(" · "))}
        </div>`;
        return;
      }
      const rows = result.entries
        .map(
          (entry) => `<tr>
            <td>${escapeHtml(entry.name)}</td>
            <td><span class="badge text-bg-${entry.kind === "media" ? "success" : "info"}">${entry.kind}</span></td>
            <td class="text-break smaller">${escapeHtml(entry.resolve_url || entry.input_url)}</td>
            <td class="smaller">${escapeHtml(entry.stream_key || "-")}</td>
          </tr>`
        )
        .join("");
      previewHost.innerHTML = `
        <div class="alert alert-secondary py-2 smaller mb-2">
          ${result.count} channel(s) parsed${result.duplicates ? `, ${result.duplicates} duplicate(s) dropped` : ""}
          ${(result.errors || []).length ? `<br><span class="text-warning">${escapeHtml(result.errors.join(" · "))}</span>` : ""}
        </div>
        <div class="table-responsive" style="max-height: 260px; overflow-y: auto;">
          <table class="table table-sm smaller mb-0">
            <thead><tr><th>Name</th><th>Type</th><th>URL</th><th>Key</th></tr></thead>
            <tbody>${rows}</tbody>
          </table>
        </div>`;
    }

    const previewButton = document.getElementById("bulk-preview");
    if (previewButton) {
      previewButton.addEventListener("click", async () => {
        try {
          renderPreview(
            await api("/api/channels/preview-list", {
              method: "POST",
              body: json(bulkPayload()),
            })
          );
        } catch (err) {
          toast(err.message, "danger");
        }
      });
    }

    const fileButton = document.getElementById("bulk-file-btn");
    const fileInput = document.getElementById("bulk-file");
    if (fileButton && fileInput) {
      fileButton.addEventListener("click", () => fileInput.click());
      fileInput.addEventListener("change", async () => {
        const file = fileInput.files[0];
        if (!file) {
          return;
        }
        try {
          bulkForm.querySelector('[name="text"]').value = await file.text();
          previewButton?.click();
          toast(`Loaded ${file.name}`, "secondary");
        } catch (err) {
          toast(err.message, "danger");
        } finally {
          fileInput.value = "";
        }
      });
    }

    bulkForm.addEventListener("submit", async (event) => {
      event.preventDefault();
      try {
        const result = await api("/api/channels/import-list", {
          method: "POST",
          body: json(bulkPayload()),
        });
        const created = (result.created || []).length;
        if (!created) {
          toast(
            (result.errors || []).join(" · ") || "No channels were created",
            "warning"
          );
          return;
        }
        toast(
          `Created ${created} channel(s)${result.skipped ? `, skipped ${result.skipped} duplicate(s)` : ""}`,
          "success"
        );
        (result.errors || []).forEach((message) => toast(message, "warning"));
        setTimeout(() => window.location.reload(), 1000);
      } catch (err) {
        toast(err.message, "danger");
      }
    });
  }

  const editForm = document.getElementById("channel-edit-form");
  if (editForm) {
    editForm.addEventListener("submit", async (event) => {
      event.preventDefault();
      const payload = formData(editForm);
      payload.provider_id = payload.provider_id ? Number(payload.provider_id) : null;
      try {
        await api(`/api/channels/${editForm.dataset.id}`, {
          method: "PUT",
          body: json(payload),
        });
        toast("Channel saved", "success");
        window.location.reload();
      } catch (err) {
        toast(err.message, "danger");
      }
    });
  }

  // ------------------------------------------------------------- providers
  const providerModalEl = document.getElementById("providerModal");
  const providerForm = document.getElementById("provider-form");
  const providerTypes = readJson("provider-types") || [];
  const providerData = readJson("provider-data") || [];
  const providerExamples = readJson("provider-examples") || {};

  function readJson(id) {
    const node = document.getElementById(id);
    if (!node) {
      return null;
    }
    try {
      return JSON.parse(node.textContent);
    } catch (err) {
      return null;
    }
  }

  function schemaFor(type) {
    const entry = providerTypes.find((item) => item.type === type);
    return entry ? entry.schema || [] : [];
  }

  function getPath(object, path) {
    return path.split(".").reduce((node, key) => (node && typeof node === "object" ? node[key] : undefined), object);
  }

  function setPath(object, path, value) {
    const parts = path.split(".");
    let node = object;
    parts.slice(0, -1).forEach((key) => {
      if (typeof node[key] !== "object" || node[key] === null) {
        node[key] = {};
      }
      node = node[key];
    });
    node[parts[parts.length - 1]] = value;
  }

  function renderProviderFields(type, config) {
    const host = document.getElementById("provider-fields");
    if (!host) {
      return;
    }
    const schema = schemaFor(type);
    if (!schema.length) {
      host.innerHTML = `<div class="col-12 text-secondary smaller">
        This provider type needs no configuration - add channels and paste a URL on each one.
      </div>`;
      return;
    }
    host.innerHTML = schema
      .map((field) => {
        const current = getPath(config, field.key);
        const value = current === undefined || current === null ? (field.default ?? "") : current;
        const help = field.help ? `<div class="form-text">${escapeHtml(field.help)}</div>` : "";
        const width = field.type === "json" ? "col-12" : "col-md-4";
        // Placeholders show the expected shape without pre-filling anything.
        const ph = field.placeholder
          ? ` placeholder="${escapeHtml(field.placeholder)}"`
          : "";
        if (field.type === "bool") {
          return `<div class="col-md-4">
            <div class="form-check mt-4">
              <input class="form-check-input" type="checkbox" data-config="${field.key}"
                     id="cfg-${field.key}" ${value ? "checked" : ""}>
              <label class="form-check-label" for="cfg-${field.key}">${escapeHtml(field.label)}</label>
            </div>${help}
          </div>`;
        }
        if (field.type === "choice") {
          const options = (field.choices || [])
            .map((choice) => `<option value="${escapeHtml(choice)}" ${choice === value ? "selected" : ""}>${escapeHtml(choice)}</option>`)
            .join("");
          return `<div class="${width}">
            <label class="form-label">${escapeHtml(field.label)}</label>
            <select class="form-select" data-config="${field.key}">${options}</select>${help}
          </div>`;
        }
        if (field.type === "json") {
          const text = typeof value === "object" ? JSON.stringify(value, null, 2) : String(value || "");
          return `<div class="${width}">
            <label class="form-label">${escapeHtml(field.label)}</label>
            <textarea class="form-control font-monospace" rows="3" data-config="${field.key}"
                      data-json="1"${ph}>${escapeHtml(text)}</textarea>${help}
          </div>`;
        }
        const inputType = field.type === "number" ? "number" : "text";
        const required = field.required ? " required" : "";
        return `<div class="${width}">
          <label class="form-label">
            ${escapeHtml(field.label)}
            ${field.required ? '<span class="text-danger">*</span>' : ""}
          </label>
          <input class="form-control" type="${inputType}" data-config="${field.key}"
                 value="${escapeHtml(value)}"${ph}${required}>${help}
        </div>`;
      })
      .join("");
  }

  function collectConfig() {
    const raw = providerForm.querySelector('[name="config_json"]');
    if (raw && raw.value.trim()) {
      try {
        return JSON.parse(raw.value);
      } catch (err) {
        throw new Error("Raw JSON configuration is not valid JSON");
      }
    }
    const config = {};
    providerForm.querySelectorAll("[data-config]").forEach((input) => {
      const key = input.dataset.config;
      let value;
      if (input.type === "checkbox") {
        value = input.checked;
      } else if (input.dataset.json) {
        const text = input.value.trim();
        if (!text) {
          return;
        }
        try {
          value = JSON.parse(text);
        } catch (err) {
          throw new Error(`${key} must be valid JSON`);
        }
      } else if (input.type === "number") {
        value = input.value === "" ? undefined : Number(input.value);
      } else {
        value = input.value.trim();
      }
      if (value === "" || value === undefined) {
        return;
      }
      setPath(config, key, value);
    });
    return config;
  }

  function openProvider(provider) {
    if (!providerForm) {
      return;
    }
    providerForm.reset();
    // An example has no id, so saving it creates a new provider.
    providerForm.querySelector('[name="id"]').value =
      provider && provider.id ? provider.id : "";
    providerForm.querySelector('[name="name"]').value = provider ? provider.name : "";
    providerForm.querySelector('[name="type"]').value = provider ? provider.type : providerTypes[0]?.type || "manual";
    providerForm.querySelector('[name="enabled"]').checked = provider ? provider.enabled : true;
    providerForm.querySelector('[name="is_default"]').checked = provider ? provider.is_default : false;
    const raw = providerForm.querySelector('[name="config_json"]');
    if (raw) {
      raw.value = "";
    }
    renderProviderFields(
      providerForm.querySelector('[name="type"]').value,
      provider ? provider.config || {} : {}
    );
    bootstrap.Modal.getOrCreateInstance(providerModalEl).show();
  }

  document.addEventListener("click", (event) => {
    if (event.target.closest("[data-provider-new]")) {
      event.preventDefault();
      openProvider(null);
    }
    const editButton = event.target.closest("[data-provider-edit]");
    if (editButton) {
      event.preventDefault();
      const id = Number(editButton.dataset.providerEdit);
      const provider = providerData.find((item) => item.id === id) || null;
      // IPTV sources open in their own simpler form.
      if (provider && provider.type === "iptv" && iptvForm) {
        openIptv(provider);
      } else {
        openProvider(provider);
      }
    }
  });

  // ------------------------------------------------------ IPTV easy form
  const iptvModalEl = document.getElementById("iptvModal");
  const iptvForm = document.getElementById("iptv-form");

  function iptvRowHtml(name = "", url = "", key = "", id = "") {
    return `<tr class="iptv-channel-row" data-channel-id="${id}">
      <td><input class="form-control form-control-sm iptv-name" value="${escapeHtml(name)}" placeholder="Sport Channel 01"></td>
      <td><input class="form-control form-control-sm iptv-url" value="${escapeHtml(url)}" placeholder="https://media.example.com/play?id=82290"></td>
      <td><input class="form-control form-control-sm iptv-key" value="${escapeHtml(key)}" placeholder="sport01"></td>
      <td class="text-nowrap">
        <button class="btn btn-sm btn-outline-primary iptv-test" type="button" title="test">
          <i class="bi bi-play-circle"></i>
        </button>
        <button class="btn btn-sm btn-outline-danger iptv-del" type="button" title="remove">&times;</button>
      </td>
    </tr>
    <tr class="iptv-test-row" hidden><td colspan="4" class="py-1"></td></tr>`;
  }

  function addIptvRow(name = "", url = "", key = "", id = "") {
    const body = document.getElementById("iptv-rows");
    if (body) {
      body.insertAdjacentHTML("beforeend", iptvRowHtml(name, url, key, id));
    }
  }

  async function openIptv(provider) {
    if (!iptvForm) {
      return;
    }
    iptvForm.reset();
    document.getElementById("iptv-rows").innerHTML = "";
    document.getElementById("iptv-result").innerHTML = "";
    document.getElementById("iptv-preview-out").innerHTML = "";
    const cfg = provider ? provider.config || {} : {};
    const auth = cfg.auth || {};
    iptvForm.querySelector('[name="id"]').value = provider ? provider.id : "";
    iptvForm.querySelector('[name="name"]').value = provider ? provider.name : "";
    const needsLogin = (auth.type || "none") === "form";
    const toggle = iptvForm.querySelector('[name="requires_login"]');
    toggle.checked = needsLogin;
    document.getElementById("iptv-login").hidden = !needsLogin;
    iptvForm.querySelector('[name="login_url"]').value = auth.url || "";
    iptvForm.querySelector('[name="base_url"]').value = cfg.base_url || "";
    iptvForm.querySelector('[name="username_field"]').value = auth.username_field || "";
    iptvForm.querySelector('[name="password_field"]').value = auth.password_field || "";
    iptvForm.querySelector('[name="success_url"]').value = auth.success_url || "";
    iptvForm.querySelector('[name="prime_url"]').value = auth.prime_url || "";
    iptvForm.querySelector('[name="url_path"]').value = (cfg.stream || {}).url_path || "";
    iptvForm.dataset.knownIds = "";

    if (!provider) {
      // A brand new source starts with a few blank rows.
      addIptvRow();
      addIptvRow();
      addIptvRow();
      bootstrap.Modal.getOrCreateInstance(iptvModalEl).show();
      return;
    }

    // Editing: pre-fill the stored username/password so they are visible and
    // are not wiped on save.
    if (needsLogin) {
      try {
        const creds = await api(`/api/iptv/${provider.id}/credentials`);
        iptvForm.querySelector('[name="username"]').value = creds.username || "";
        iptvForm.querySelector('[name="password"]').value = creds.password || "";
      } catch (err) {
        /* leave blank if they can't be read */
      }
    }

    // Editing: load this provider's existing channels into the list so they
    // are visible and editable, not lost.
    try {
      const data = await api("/api/channels");
      const mine = (data.channels || []).filter((c) => c.provider_id === provider.id);
      const ids = [];
      mine.forEach((c) => {
        addIptvRow(c.name, c.resolve_url || c.input_url || "", c.stream_key || "", c.id);
        ids.push(c.id);
      });
      iptvForm.dataset.knownIds = ids.join(",");
      if (mine.length === 0) {
        addIptvRow();
      }
    } catch (err) {
      toast(err.message, "danger");
      addIptvRow();
    }
    bootstrap.Modal.getOrCreateInstance(iptvModalEl).show();
  }

  if (iptvForm) {
    document.getElementById("iptv-login-toggle").addEventListener("change", (e) => {
      document.getElementById("iptv-login").hidden = !e.target.checked;
    });
    document.getElementById("iptv-add-row").addEventListener("click", () => addIptvRow());
    document.getElementById("iptv-rows").addEventListener("click", async (e) => {
      const del = e.target.closest(".iptv-del");
      if (del) {
        const row = del.closest("tr");
        const feedback = row.nextElementSibling;
        if (feedback && feedback.classList.contains("iptv-test-row")) {
          feedback.remove();
        }
        row.remove();
        return;
      }
      const testBtn = e.target.closest(".iptv-test");
      if (testBtn) {
        await testIptvRow(testBtn.closest("tr"), testBtn);
      }
    });
    document.getElementById("iptv-paste").addEventListener("click", () => {
      bootstrap.Collapse.getOrCreateInstance(document.getElementById("iptv-paste-box")).toggle();
    });
    document.getElementById("iptv-paste-apply").addEventListener("click", () => {
      const text = document.getElementById("iptv-paste-text").value;
      let added = 0;
      text.split("\n").forEach((line) => {
        const raw = line.trim();
        if (!raw || raw.startsWith("#")) {
          return;
        }
        const parts = raw.split(/\s*[|\t;,]\s*/).filter(Boolean);
        const url = parts.find((p) => /^https?:\/\//i.test(p));
        if (!url) {
          return;
        }
        const labels = parts.filter((p) => p !== url);
        addIptvRow(labels[0] || "", url, labels[1] || "");
        added += 1;
      });
      document.getElementById("iptv-paste-text").value = "";
      bootstrap.Collapse.getOrCreateInstance(document.getElementById("iptv-paste-box")).hide();
      toast(`${added} row(s) added`, added ? "secondary" : "warning");
    });

    iptvForm.addEventListener("submit", async (event) => {
      event.preventDefault();
      const data = formData(iptvForm);
      const channels = Array.from(document.querySelectorAll("#iptv-rows tr.iptv-channel-row"))
        .map((tr) => ({
          id: tr.dataset.channelId ? Number(tr.dataset.channelId) : null,
          name: tr.querySelector(".iptv-name").value.trim(),
          url: tr.querySelector(".iptv-url").value.trim(),
          stream_key: tr.querySelector(".iptv-key").value.trim(),
        }))
        .filter((row) => row.url || row.id);
      if (!data.name) {
        toast("Give the source a name", "warning");
        return;
      }
      if (channels.length === 0 && !data.id) {
        toast("Add at least one channel URL", "warning");
        return;
      }
      const knownIds = (iptvForm.dataset.knownIds || "")
        .split(",")
        .map((s) => Number(s))
        .filter((n) => n);
      const payload = {
        id: data.id ? Number(data.id) : null,
        name: data.name,
        requires_login: Boolean(data.requires_login),
        base_url: data.base_url || "",
        login_url: data.login_url || "/login",
        success_url: data.success_url || "",
        prime_url: data.prime_url || "",
        username_field: data.username_field || "username",
        password_field: data.password_field || "password",
        url_path: data.url_path || "",
        channels,
        known_channel_ids: knownIds,
      };
      if (data.requires_login) {
        if (data.username) payload.username = data.username;
        if (data.password) payload.password = data.password;
      }
      try {
        const result = await api("/api/iptv", { method: "POST", body: json(payload) });
        const created = (result.created || []).length;
        const updated = (result.updated || []).length;
        const deleted = (result.deleted || []).length;
        const parts = [];
        if (created) parts.push(`${created} added`);
        if (updated) parts.push(`${updated} updated`);
        if (deleted) parts.push(`${deleted} removed`);
        if (result.skipped) parts.push(`${result.skipped} skipped`);
        toast(`Saved. ${parts.join(", ") || "no changes"}`, "success");
        (result.errors || []).forEach((m) => toast(m, "warning"));
        setTimeout(() => window.location.reload(), 1000);
      } catch (err) {
        toast(err.message, "danger");
      }
    });

    // Preview: fetch the first URL and show the response so the operator can
    // pick which JSON field holds the stream URL.
    const previewBtn = document.getElementById("iptv-preview-btn");
    if (previewBtn) {
      previewBtn.addEventListener("click", () => {
        const firstUrl = (document.querySelector("#iptv-rows .iptv-url") || {}).value || "";
        if (!firstUrl.trim()) {
          toast("Fill in a channel URL first", "warning");
          return;
        }
        runIptvPreview(firstUrl.trim());
      });
    }

    // Test login only: log in with the entered credentials and report the
    // result, without touching any channel URL.
    const testLoginBtn = document.getElementById("iptv-test-login-btn");
    if (testLoginBtn) {
      testLoginBtn.addEventListener("click", () => runIptvTestLogin(testLoginBtn));
    }
  }

  async function runIptvTestLogin(btn) {
    const out = document.getElementById("iptv-test-login-out");
    const data = formData(iptvForm);
    if (!data.requires_login) {
      if (out) out.innerHTML = '<span class="text-warning">เปิด "ต้องล็อกอิน" ก่อน</span>';
      return;
    }
    const payload = {
      requires_login: true,
      base_url: data.base_url || "",
      login_url: data.login_url || "/login",
      success_url: data.success_url || "",
      prime_url: data.prime_url || "",
      username_field: data.username_field || "username",
      password_field: data.password_field || "password",
      provider_id: data.id ? Number(data.id) : null,
    };
    if (data.username) payload.username = data.username;
    if (data.password) payload.password = data.password;
    btn.disabled = true;
    const original = btn.innerHTML;
    btn.innerHTML = '<span class="spinner-border spinner-border-sm"></span> กำลังล็อกอิน...';
    if (out) out.innerHTML = "";
    try {
      const r = await api("/api/iptv/test-login", { method: "POST", body: json(payload) });
      if (out) {
        const head = r.ok
          ? '<span class="text-success"><i class="bi bi-check-circle-fill"></i> ล็อกอินสำเร็จ</span>'
          : `<span class="text-danger"><i class="bi bi-x-circle-fill"></i> ล็อกอินไม่สำเร็จ · ${escapeHtml(r.error || "")}</span>`;
        out.innerHTML = head + renderLoginDebug(r.debug);
      }
      btn.classList.toggle("btn-outline-success", r.ok);
      btn.classList.toggle("btn-outline-danger", !r.ok);
      btn.classList.toggle("btn-outline-primary", false);
    } catch (err) {
      if (out) out.innerHTML = `<span class="text-danger">${escapeHtml(err.message)}</span>`;
    } finally {
      btn.disabled = false;
      btn.innerHTML = original;
    }
  }

  // A small "what actually happened" panel under the test-login button, so a
  // bounce is diagnosable: which page it landed on, whether a CSRF/hidden field
  // was picked up, and which fields were posted. No secret values are shown.
  function renderLoginDebug(d) {
    if (!d || typeof d !== "object") return "";
    const rows = [];
    const line = (label, value) =>
      rows.push(
        `<div class="d-flex gap-2"><span class="text-secondary" style="min-width:9rem">${label}</span>` +
          `<span class="text-break">${value}</span></div>`
      );
    if (d.login_url) line("ล็อกอินไปที่", `<code>${escapeHtml(d.login_url)}</code>`);
    if (d.primed) {
      const fields = (d.hidden_fields || []).length
        ? (d.hidden_fields || []).map((f) => `<code>${escapeHtml(f)}</code>`).join(", ")
        : '<span class="text-warning">ไม่พบ (เช่น csrf_tv_name) — อาจต้องตั้ง "หน้าที่แสดงฟอร์มล็อกอิน" ให้ตรง</span>';
      line("ฟิลด์ซ่อน (CSRF)", fields);
      if (d.prime_source) line("เจอ CSRF ที่", `<code>${escapeHtml(d.prime_source)}</code>`);
    } else {
      line("Prime หน้าฟอร์ม", '<span class="text-secondary">ไม่ได้ทำ</span>');
    }
    if ((d.posted_fields || []).length)
      line("ส่งฟิลด์", (d.posted_fields || []).map((f) => `<code>${escapeHtml(f)}</code>`).join(", "));
    if (d.post_status != null) line("HTTP status", String(d.post_status));
    if (d.final_url) {
      const landed = d.landed_on_success_url;
      const tag =
        landed === true
          ? ' <span class="text-success">(ถึงหน้าสำเร็จ)</span>'
          : landed === false
          ? ' <span class="text-danger">(เด้งกลับหน้าล็อกอิน)</span>'
          : "";
      line("ไปจบที่", `<code>${escapeHtml(d.final_url)}</code>${tag}`);
    }
    line("Cookie ที่ได้", String(d.cookies || 0));
    return `<div class="mt-2 p-2 rounded border smaller" style="line-height:1.7">${rows.join("")}</div>`;
  }

  // Fetch one URL through the current login config and show the response with a
  // clickable list of JSON fields. Used by the preview button and, on a failed
  // row test, to help the operator find the right field.
  async function runIptvPreview(url) {
    const out = document.getElementById("iptv-preview-out");
    const data = formData(iptvForm);
    out.innerHTML = '<div class="text-secondary smaller">กำลังดึง response...</div>';
    try {
      out.scrollIntoView({ behavior: "smooth", block: "nearest" });
    } catch (e) {
      /* older browsers */
    }
    const payload = {
      url,
      url_path: data.url_path || "",
      requires_login: Boolean(data.requires_login),
      base_url: data.base_url || "",
      login_url: data.login_url || "/login",
      success_url: data.success_url || "",
      prime_url: data.prime_url || "",
      username_field: data.username_field || "username",
      password_field: data.password_field || "password",
      provider_id: data.id ? Number(data.id) : null,
    };
    if (data.requires_login) {
      if (data.username) payload.username = data.username;
      if (data.password) payload.password = data.password;
    }
    try {
      const r = await api("/api/iptv/preview", { method: "POST", body: json(payload) });
      renderIptvPreview(r);
    } catch (err) {
      out.innerHTML = `<div class="alert alert-danger py-2 mb-0">${escapeHtml(err.message)}</div>`;
    }
  }

  async function testIptvRow(row, btn) {
    const url = (row.querySelector(".iptv-url").value || "").trim();
    const feedback = row.nextElementSibling;
    const cell = feedback && feedback.classList.contains("iptv-test-row")
      ? feedback.querySelector("td")
      : null;
    if (!url) {
      toast("Fill in the URL for this row first", "warning");
      return;
    }
    const data = formData(iptvForm);
    const payload = {
      url,
      url_path: data.url_path || "",
      requires_login: Boolean(data.requires_login),
      base_url: data.base_url || "",
      login_url: data.login_url || "/login",
      success_url: data.success_url || "",
      prime_url: data.prime_url || "",
      username_field: data.username_field || "username",
      password_field: data.password_field || "password",
      provider_id: data.id ? Number(data.id) : null,
    };
    if (data.requires_login) {
      if (data.username) payload.username = data.username;
      if (data.password) payload.password = data.password;
    }
    btn.disabled = true;
    const original = btn.innerHTML;
    btn.innerHTML = '<span class="spinner-border spinner-border-sm"></span>';
    if (feedback) {
      feedback.hidden = false;
    }
    if (cell) {
      cell.innerHTML = '<span class="text-secondary smaller">กำลังทดสอบ...</span>';
    }
    try {
      const r = await api("/api/iptv/test-url", { method: "POST", body: json(payload) });
      if (cell) {
        // Show whether the login step happened, so it's clear the credentials
        // above were used.
        let loginTag = "";
        if (payload.requires_login) {
          loginTag = r.logged_in
            ? '<span class="text-success"><i class="bi bi-key-fill"></i> ล็อกอินสำเร็จ</span> · '
            : (r.stage === "login"
                ? '<span class="text-danger"><i class="bi bi-key-fill"></i> ล็อกอินไม่สำเร็จ</span> · '
                : "");
        }
        cell.innerHTML = r.ok
          ? `<span class="smaller">${loginTag}<span class="text-success">
               <i class="bi bi-check-circle-fill"></i> ดึงได้ · ${escapeHtml(r.summary || "")}</span></span>`
          : `<span class="smaller">${loginTag}<span class="text-danger">
               <i class="bi bi-x-circle-fill"></i> ดึงไม่ได้ · ${escapeHtml(r.error || "")}</span>
               ${r.stage === "resolve"
                 ? ' <button type="button" class="btn btn-sm btn-link p-0 iptv-show-response">ดู response แล้วเลือก field</button>'
                 : ""}</span>`;
      }
      // When login worked but no URL was found, offer to open the response so
      // the operator can pick the field.
      if (!r.ok && r.stage === "resolve" && cell) {
        const link = cell.querySelector(".iptv-show-response");
        if (link) {
          link.addEventListener("click", () => runIptvPreview(url));
        }
      }
      btn.classList.toggle("btn-outline-success", r.ok);
      btn.classList.toggle("btn-outline-danger", !r.ok);
      btn.classList.toggle("btn-outline-primary", false);
    } catch (err) {
      if (cell) {
        cell.innerHTML = `<span class="text-danger smaller">${escapeHtml(err.message)}</span>`;
      }
    } finally {
      btn.disabled = false;
      btn.innerHTML = original;
    }
  }

  function renderIptvPreview(r) {
    const out = document.getElementById("iptv-preview-out");
    if (!r.ok) {
      out.innerHTML = `<div class="alert alert-danger py-2 mb-0">${escapeHtml(r.error || "failed")}</div>`;
      return;
    }
    const loginLine = r.logged_in
      ? '<span class="text-success"><i class="bi bi-key-fill"></i> ล็อกอินสำเร็จ</span> · '
      : "";
    let html = `<div class="border rounded p-2 mt-1">
      <div class="smaller text-secondary mb-2">${loginLine}HTTP ${escapeHtml(r.status)} · ${escapeHtml(r.content_type || "")}</div>`;

    const paths = r.paths || [];
    if (paths.length) {
      html += `<div class="smaller mb-1">คลิกเลือก field ที่เก็บ URL ของ stream:</div>
        <div class="d-flex flex-column gap-1 mb-2" style="max-height: 200px; overflow-y: auto;">`;
      paths.forEach((p) => {
        const badge = p.looks_like_media
          ? '<span class="badge text-bg-success">media</span>'
          : p.looks_like_url
          ? '<span class="badge text-bg-info">url</span>'
          : "";
        html += `<button type="button" class="btn btn-sm btn-outline-secondary text-start iptv-pick"
                   data-path="${escapeHtml(p.path)}">
            <code>${escapeHtml(p.path)}</code> ${badge}
            <span class="text-secondary smaller d-block text-truncate">${escapeHtml(p.value)}</span>
          </button>`;
      });
      html += `</div>`;
    } else if ((r.candidates || []).length) {
      html += `<div class="smaller">พบ URL เหล่านี้ (ระบบจะหาให้อัตโนมัติ):</div>
        <ul class="smaller mb-2">${r.candidates.map((c) => `<li><code>${escapeHtml(c)}</code></li>`).join("")}</ul>`;
    } else {
      html += `<div class="alert alert-warning py-2 smaller mb-2">ไม่พบ URL ใน response — ลองตรวจ URL หรือข้อมูลล็อกอิน</div>`;
    }

    if (r.json_preview !== undefined) {
      html += `<details class="smaller"><summary>ดู response ดิบ</summary>
        <pre class="json-preview mb-0">${escapeHtml(json(r.json_preview))}</pre></details>`;
    } else if (r.text_preview) {
      html += `<details class="smaller"><summary>ดู response ดิบ</summary>
        <pre class="json-preview mb-0">${escapeHtml(r.text_preview)}</pre></details>`;
    }
    html += `</div>`;
    out.innerHTML = html;

    out.querySelectorAll(".iptv-pick").forEach((btn) => {
      btn.addEventListener("click", () => {
        iptvForm.querySelector('[name="url_path"]').value = btn.dataset.path;
        toast(`Field set: ${btn.dataset.path}`, "success");
      });
    });
  }

  document.addEventListener("click", (event) => {
    if (event.target.closest("[data-iptv-new]")) {
      event.preventDefault();
      openIptv(null);
    }
  });

  const typeSelect = document.getElementById("provider-type");
  if (typeSelect) {
    typeSelect.addEventListener("change", () => renderProviderFields(typeSelect.value, {}));
  }

  // "Use this example" on a card, and "Fill in the example" inside the modal.
  function applyExample(type) {
    const example = providerExamples[type];
    if (!example) {
      toast(`No example available for ${type}`, "warning");
      return;
    }
    openProvider({
      id: "",
      name: example.name,
      type: example.type,
      enabled: true,
      is_default: providerData.length === 0,
      config: example.config,
    });
    toast("Example loaded - edit the values, then press Save provider", "secondary");
  }

  document.addEventListener("click", (event) => {
    const card = event.target.closest("[data-provider-example]");
    if (card) {
      event.preventDefault();
      applyExample(card.dataset.providerExample);
    }
  });

  const loadExample = document.getElementById("load-example");
  if (loadExample) {
    loadExample.addEventListener("click", () => {
      const type = typeSelect ? typeSelect.value : "http_json";
      const example = providerExamples[type];
      if (!example) {
        toast(`No example available for ${type}`, "warning");
        return;
      }
      // Keep whatever the operator already typed in the name field.
      const nameInput = providerForm.querySelector('[name="name"]');
      if (nameInput && !nameInput.value.trim()) {
        nameInput.value = example.name;
      }
      const raw = providerForm.querySelector('[name="config_json"]');
      if (raw) {
        raw.value = "";
      }
      renderProviderFields(type, example.config);
      toast("Example values filled in", "secondary");
    });
  }

  if (providerForm) {
    providerForm.addEventListener("submit", async (event) => {
      event.preventDefault();
      const data = formData(providerForm);
      let config;
      try {
        config = collectConfig();
      } catch (err) {
        toast(err.message, "danger");
        return;
      }
      const payload = {
        name: data.name,
        type: data.type,
        enabled: Boolean(data.enabled),
        is_default: Boolean(data.is_default),
        config,
      };
      ["username", "password", "token", "cookie"].forEach((key) => {
        if (data[key]) {
          payload[key] = data[key];
        }
      });
      const id = data.id;
      try {
        if (id) {
          await api(`/api/providers/${id}`, { method: "PUT", body: json(payload) });
        } else {
          await api("/api/providers", { method: "POST", body: json(payload) });
        }
        toast("Provider saved", "success");
        window.location.reload();
      } catch (err) {
        toast(err.message, "danger");
      }
    });
  }

  document.addEventListener("click", async (event) => {
    const deleteButton = event.target.closest("[data-provider-delete]");
    if (deleteButton) {
      event.preventDefault();
      if (!window.confirm(deleteButton.dataset.confirm || "Delete this provider?")) {
        return;
      }
      try {
        await api(`/api/providers/${deleteButton.dataset.providerDelete}`, { method: "DELETE" });
        toast("Provider deleted", "success");
        window.location.reload();
      } catch (err) {
        toast(err.message, "danger");
      }
      return;
    }

    const testButton = event.target.closest("[data-provider-test]");
    if (!testButton) {
      return;
    }
    event.preventDefault();
    const kind = testButton.dataset.providerTest;
    const id = testButton.dataset.id;
    const host = document.getElementById(`provider-result-${id}`);
    testButton.disabled = true;
    try {
      if (kind === "resolve") {
        const modal = document.getElementById("resolveModal");
        modal.dataset.providerId = id;
        bootstrap.Modal.getOrCreateInstance(modal).show();
        return;
      }
      const endpoint = kind === "auth" ? "test-auth" : "test-channels";
      const result = await api(`/api/providers/${id}/${endpoint}`, { method: "POST" });
      if (host) {
        host.innerHTML = result.ok
          ? `<div class="alert alert-success py-2 mb-0">${
              kind === "auth"
                ? "Login Successful"
                : `${result.count} channels found: ` +
                  escapeHtml((result.sample || []).map((c) => c.name).slice(0, 5).join(", "))
            }</div>`
          : `<div class="alert alert-danger py-2 mb-0">${escapeHtml(result.error)}</div>`;
      }
      toast(result.ok ? "Test passed" : result.error, result.ok ? "success" : "danger");
    } catch (err) {
      toast(err.message, "danger");
    } finally {
      testButton.disabled = false;
    }
  });

  const resolveRun = document.getElementById("resolve-run");
  if (resolveRun) {
    resolveRun.addEventListener("click", async () => {
      const modal = document.getElementById("resolveModal");
      const providerId = modal.dataset.providerId;
      const channelId = Number(document.getElementById("resolve-channel").value);
      const output = document.getElementById("resolve-output");
      output.innerHTML = '<div class="text-secondary">Resolving...</div>';
      try {
        const result = await api(`/api/providers/${providerId}/test-resolve`, {
          method: "POST",
          body: json({ channel_id: channelId }),
        });
        const report = result.report || {};
        output.innerHTML = `
          <div class="alert ${result.ok ? "alert-success" : "alert-danger"} mb-0">
            <div>Source reachable: <strong>${report.source_reachable ? "YES" : "NO"}</strong></div>
            ${result.ok ? `
              <div>Video codec: ${escapeHtml(report.video_codec || "-")}</div>
              <div>Audio codec: ${escapeHtml(report.audio_codec || "-")}</div>
              <div>Resolution: ${escapeHtml(report.resolution || "-")}</div>
              <div class="text-break">URL: <code>${escapeHtml((result.stream || {}).url || "")}</code></div>
            ` : `<div>${escapeHtml(result.error || "")}</div>`}
          </div>`;
      } catch (err) {
        output.innerHTML = `<div class="alert alert-danger mb-0">${escapeHtml(err.message)}</div>`;
      }
    });
  }

  // ---------------------------------------------------------- debug page
  const debugButton = document.getElementById("run-debug");
  if (debugButton) {
    debugButton.addEventListener("click", async () => {
      const host = document.getElementById("debug-output");
      debugButton.disabled = true;
      host.innerHTML = '<div class="card"><div class="card-body text-secondary">Running...</div></div>';
      try {
        const result = await api(`/api/providers/${debugButton.dataset.id}/debug`);
        if (!result.ok) {
          host.innerHTML = `<div class="alert alert-danger">${escapeHtml(result.error)}</div>`;
          return;
        }
        const report = result.report || {};
        const steps = (report.steps || [])
          .map(
            (step) => `
          <tr>
            <td>${escapeHtml(step.name)}</td>
            <td>${step.ok ? '<span class="badge text-bg-success">OK</span>' : '<span class="badge text-bg-danger">FAILED</span>'}</td>
            <td>${escapeHtml(step.status || "-")}</td>
            <td>${escapeHtml(step.content_type || "-")}</td>
            <td class="text-break">${escapeHtml(step.detail || "")}</td>
          </tr>
          ${step.preview ? `<tr><td colspan="5"><pre class="json-preview mb-0">${escapeHtml(step.preview)}</pre></td></tr>` : ""}`
          )
          .join("");
        const cookies = (report.cookies || [])
          .map((cookie) => `<li>${escapeHtml(cookie.name)} = <span class="text-secondary">***</span> <span class="smaller">(${escapeHtml(cookie.domain || "")})</span></li>`)
          .join("");
        host.innerHTML = `
          <div class="card mb-3">
            <div class="card-header">Request context</div>
            <div class="card-body smaller">
              <div>Base URL: <code>${escapeHtml(report.base_url || "")}</code></div>
              <div>Auth type: ${escapeHtml(report.auth_type || "")}</div>
              <div>Headers sent: <pre class="json-preview mb-0">${escapeHtml(json(report.request_headers || {}))}</pre></div>
            </div>
          </div>
          <div class="card mb-3">
            <div class="card-header">Steps</div>
            <div class="table-responsive">
              <table class="table table-sm mb-0 smaller">
                <thead><tr><th>Step</th><th>Result</th><th>HTTP</th><th>Content type</th><th>Detail</th></tr></thead>
                <tbody>${steps || '<tr><td colspan="5">No steps.</td></tr>'}</tbody>
              </table>
            </div>
          </div>
          <div class="card">
            <div class="card-header">Cookies received</div>
            <div class="card-body smaller">
              ${cookies ? `<ul class="mb-0">${cookies}</ul>` : '<span class="text-secondary">none</span>'}
              ${report.channel_count !== undefined ? `<div class="mt-2">Channels parsed: <strong>${escapeHtml(report.channel_count)}</strong></div>` : ""}
            </div>
          </div>`;
      } catch (err) {
        host.innerHTML = `<div class="alert alert-danger">${escapeHtml(err.message)}</div>`;
      } finally {
        debugButton.disabled = false;
      }
    });
  }

  // ---------------------------------------------------------- settings page
  const settingsForm = document.getElementById("settings-form");
  if (settingsForm) {
    settingsForm.addEventListener("submit", async (event) => {
      event.preventDefault();
      const data = formData(settingsForm);
      const payload = { values: {} };
      Object.entries(data).forEach(([key, value]) => {
        if (key === "telegram_bot_token") {
          if (value) {
            payload.telegram_bot_token = value;
          }
          return;
        }
        if (key === "admin_password") {
          if (value) {
            payload.admin_password = value;
          }
          return;
        }
        payload.values[key] = value;
      });
      try {
        const result = await api("/api/settings", { method: "POST", body: json(payload) });
        if (result.ok) {
          toast("Settings saved", "success");
          setTimeout(() => window.location.reload(), 800);
        } else {
          Object.entries(result.errors || {}).forEach(([key, message]) =>
            toast(`${key}: ${message}`, "danger")
          );
        }
      } catch (err) {
        toast(err.message, "danger");
      }
    });
  }

  const telegramTest = document.getElementById("telegram-test");
  if (telegramTest) {
    telegramTest.addEventListener("click", async () => {
      const host = document.getElementById("telegram-result") || document.getElementById("setup-result");
      telegramTest.disabled = true;
      try {
        const result = await api("/api/telegram/test", { method: "POST" });
        const message = result.ok ? "Message sent" : result.error || "failed";
        if (host) {
          host.innerHTML = `<span class="${result.ok ? "text-success" : "text-danger"}">${escapeHtml(message)}</span>`;
        }
        toast(message, result.ok ? "success" : "danger");
      } catch (err) {
        toast(err.message, "danger");
      } finally {
        telegramTest.disabled = false;
      }
    });
  }

  // Auto-start (start on boot + restart on crash) install/remove.
  const autostartInstall = document.getElementById("autostart-install");
  const autostartRemove = document.getElementById("autostart-remove");
  const autostartOut = document.getElementById("autostart-result");
  const autostartBadge = document.getElementById("autostart-badge");
  function paintAutostart(installed) {
    if (autostartInstall) autostartInstall.disabled = installed;
    if (autostartRemove) autostartRemove.disabled = !installed;
    if (autostartBadge) {
      autostartBadge.textContent = installed ? "on" : "off";
      autostartBadge.className = "badge " + (installed ? "text-bg-success" : "text-bg-secondary");
    }
  }
  async function callAutostart(path, btn) {
    if (!btn) return;
    const original = btn.innerHTML;
    btn.disabled = true;
    btn.innerHTML = '<span class="spinner-border spinner-border-sm"></span>';
    try {
      const r = await api(path, { method: "POST" });
      if (autostartOut) {
        autostartOut.innerHTML = r.ok
          ? `<span class="text-success"><i class="bi bi-check-circle-fill"></i> ${escapeHtml(r.message || "done")}</span>`
          : `<span class="text-danger"><i class="bi bi-x-circle-fill"></i> ${escapeHtml(r.error || "failed")}</span>`;
      }
      if (r.ok) paintAutostart(Boolean(r.installed));
      else btn.disabled = false;
    } catch (err) {
      if (autostartOut) autostartOut.innerHTML = `<span class="text-danger">${escapeHtml(err.message)}</span>`;
      btn.disabled = false;
    } finally {
      btn.innerHTML = original;
    }
  }
  if (autostartInstall) {
    autostartInstall.addEventListener("click", () =>
      callAutostart("/api/autostart/install", autostartInstall)
    );
  }
  if (autostartRemove) {
    autostartRemove.addEventListener("click", () =>
      callAutostart("/api/autostart/remove", autostartRemove)
    );
  }

  const importButton = document.getElementById("import-btn");
  const importFile = document.getElementById("import-file");
  if (importButton && importFile) {
    importButton.addEventListener("click", () => importFile.click());
    importFile.addEventListener("change", async () => {
      const file = importFile.files[0];
      if (!file) {
        return;
      }
      try {
        const text = await file.text();
        const result = await api("/api/config/import", {
          method: "POST",
          body: json({ data: JSON.parse(text) }),
        });
        const report = result.report || {};
        toast(
          `Imported ${report.channels || 0} channels, ${report.providers || 0} providers`,
          result.ok ? "success" : "warning"
        );
        (report.errors || []).forEach((message) => toast(message, "danger"));
        setTimeout(() => window.location.reload(), 1200);
      } catch (err) {
        toast(err.message, "danger");
      } finally {
        importFile.value = "";
      }
    });
  }

  // ------------------------------------------------------------- setup page
  const setupForm = document.getElementById("setup-form");
  if (setupForm) {
    setupForm.addEventListener("submit", async (event) => {
      event.preventDefault();
      const data = formData(setupForm);
      const payload = {
        values: {
          default_rtmp_server: data.default_rtmp_server || "",
          telegram_chat_id: data.telegram_chat_id || "",
        },
      };
      if (data.telegram_bot_token) {
        payload.telegram_bot_token = data.telegram_bot_token;
      }
      try {
        const result = await api("/api/settings", { method: "POST", body: json(payload) });
        if (result.ok) {
          toast("Saved", "success");
          window.location.href = "/";
        } else {
          Object.entries(result.errors || {}).forEach(([key, message]) =>
            toast(`${key}: ${message}`, "danger")
          );
        }
      } catch (err) {
        toast(err.message, "danger");
      }
    });
  }

  const ffmpegTest = document.getElementById("ffmpeg-test");
  if (ffmpegTest) {
    ffmpegTest.addEventListener("click", async () => {
      const host = document.getElementById("setup-result");
      try {
        const status = await api("/api/status");
        const ok = status.ffmpeg.available && status.ffprobe.available;
        host.innerHTML = `<div class="${ok ? "text-success" : "text-danger"}">
          ${escapeHtml(status.ffmpeg.version || status.ffmpeg.error)}<br>
          ${escapeHtml(status.ffprobe.version || status.ffprobe.error)}
        </div>`;
      } catch (err) {
        toast(err.message, "danger");
      }
    });
  }

  const loginTest = document.getElementById("login-test");
  if (loginTest) {
    loginTest.addEventListener("click", async () => {
      const host = document.getElementById("setup-result");
      try {
        const result = await api("/api/test-login", { method: "POST", body: json({}) });
        host.innerHTML = `<div class="${result.ok ? "text-success" : "text-danger"}">
          ${escapeHtml(result.ok ? result.message || "Login Successful" : result.error)}
        </div>`;
      } catch (err) {
        toast(err.message, "danger");
      }
    });
  }
})();
