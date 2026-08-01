(function () {
  "use strict";

  const state = {
    assets: [],
    filtered: 0,
    offset: 0,
    limit: 36,
    selected: null,
    facetsLoaded: false,
  };
  const byId = (id) => document.getElementById(id);

  function h(value) {
    return String(value ?? "")
      .replace(/&/g, "&amp;")
      .replace(/</g, "&lt;")
      .replace(/>/g, "&gt;")
      .replace(/"/g, "&quot;")
      .replace(/'/g, "&#039;");
  }

  function label(value) {
    return {
      reference_ready: "REFERENCE READY",
      local_preview: "LOCAL PREVIEW",
      blocked: "BLOCKED",
      runtime_verified: "RUNTIME VERIFIED",
      catalog_ready: "CATALOG READY",
      pass_local_preview: "LOCAL PREVIEW PASS",
      pass: "PASS",
      fail: "FAIL",
    }[value] || String(value || "UNKNOWN").replace(/_/g, " ").toUpperCase();
  }

  function badgeKind(value) {
    if (["reference_ready", "runtime_verified", "pass"].includes(value)) return "good";
    if (["local_preview", "catalog_ready", "pass_local_preview"].includes(value)) return "warn";
    return "bad";
  }

  function filterParams() {
    return {
      q: byId("search").value.trim(),
      category: byId("category").value,
      qualification: byId("qualification").value,
      binding: byId("binding").value,
      source: byId("source").value,
    };
  }

  function fillSelect(id, rows, fallback) {
    const select = byId(id);
    const current = select.value;
    select.innerHTML =
      `<option value="">${h(fallback)}</option>` +
      rows
        .map(
          (row) =>
            `<option value="${h(row.value)}">${h(label(row.value))} · ${h(row.count)}</option>`,
        )
        .join("");
    select.value = current;
  }

  function loadFacets(data) {
    if (state.facetsLoaded) return;
    fillSelect("category", data.facets.categories, "全部类别");
    fillSelect("qualification", data.facets.qualifications, "全部资格");
    fillSelect("binding", data.facets.bindings, "全部状态");
    fillSelect("source", data.facets.sources, "全部来源");
    const verified =
      data.facets.bindings.find((row) => row.value === "runtime_verified")?.count || 0;
    byId("verifiedMetric").textContent = verified.toLocaleString();
    state.facetsLoaded = true;
  }

  async function fetchPage(reset = false) {
    if (reset) {
      state.assets = [];
      state.offset = 0;
      state.selected = null;
      byId("detail").hidden = true;
      byId("placeholder").hidden = false;
    }
    byId("status").textContent = "LOADING";
    byId("status").dataset.kind = "warn";
    const params = new URLSearchParams({
      ...filterParams(),
      offset: String(state.offset),
      limit: String(state.limit),
    });
    try {
      const response = await fetch(`/api/assets?${params}`);
      if (!response.ok) throw new Error(`${response.status} ${response.statusText}`);
      const data = await response.json();
      loadFacets(data);
      state.assets.push(...data.assets);
      state.offset = state.assets.length;
      state.filtered = data.filtered;
      renderGrid();
      byId("totalMetric").textContent = data.total.toLocaleString();
      byId("filteredMetric").textContent = data.filtered.toLocaleString();
      byId("catalogMeta").textContent =
        `${state.assets.length.toLocaleString()} / ${data.filtered.toLocaleString()} 项 · ${data.registry}`;
      byId("status").textContent = "READY";
      byId("status").dataset.kind = "good";
      byId("loadMore").hidden = state.assets.length >= data.filtered;
    } catch (error) {
      byId("status").textContent = "ERROR";
      byId("status").dataset.kind = "bad";
      byId("catalogMeta").textContent = `读取失败：${error.message}`;
    }
  }

  function renderGrid() {
    const grid = byId("assetGrid");
    byId("empty").hidden = state.assets.length !== 0;
    grid.innerHTML = state.assets
      .map(
        (asset) => `
          <button class="asset-card" type="button" data-asset-id="${h(asset.asset_id)}"
            aria-pressed="${asset.asset_id === state.selected}">
            <span class="thumb">
              ${
                asset.thumbnail_url
                  ? `<img src="${h(asset.thumbnail_url)}" alt="" loading="lazy">`
                  : "NO PREVIEW"
              }
            </span>
            <span class="card-body">
              <h3 title="${h(asset.name)}">${h(asset.name)}</h3>
              <code>${h(asset.type)} · ${h(asset.technical_name)}</code>
              <span class="badges">
                <span class="badge" data-kind="${badgeKind(asset.qualification)}">${h(label(asset.qualification))}</span>
                <span class="badge" data-kind="${badgeKind(asset.binding_status)}">${h(label(asset.binding_status))}</span>
              </span>
            </span>
          </button>`,
      )
      .join("");
    grid.querySelectorAll(".asset-card").forEach((button) => {
      button.addEventListener("click", () => selectAsset(button.dataset.assetId));
    });
    grid.querySelectorAll("img").forEach((image) => {
      image.addEventListener("error", () => {
        image.parentElement.textContent = "PREVIEW UNAVAILABLE";
      });
    });
  }

  function spineRow(title, detail, stateName) {
    return `
      <div class="spine-row" data-state="${h(stateName)}">
        <span class="node"></span><strong>${h(title)}</strong><code>${h(detail)}</code>
      </div>`;
  }

  function fact(name, value, mono = false) {
    const tag = mono ? "code" : "span";
    return `<div class="fact"><b>${h(name)}</b><${tag}>${h(value || "—")}</${tag}></div>`;
  }

  function renderDetail(asset) {
    const execution = asset.execution_gate;
    const reference = asset.reference_gate;
    const evidence = asset.runtime_evidence || [];
    const materializedState = asset.materialized && asset.dependencies_ready ? "good" : "bad";
    const executionState = execution.status === "pass" ? "good" : execution.status === "pass_local_preview" ? "warn" : "bad";
    const referenceState = reference.status === "pass" ? "good" : "warn";
    const runtimeState = evidence.length ? "good" : asset.binding_status === "catalog_ready" ? "warn" : "bad";
    byId("detail").innerHTML = `
      <div class="detail-hero">
        ${asset.thumbnail_url ? `<img src="${h(asset.thumbnail_url)}" alt="">` : ""}
        <div class="detail-title">
          <h2>${h(asset.name)}</h2>
          <code>${h(asset.asset_id)}</code>
        </div>
      </div>
      <div class="detail-body">
        <span class="badges">
          <span class="badge" data-kind="${badgeKind(asset.qualification)}">${h(label(asset.qualification))}</span>
          <span class="badge" data-kind="${badgeKind(asset.binding_status)}">${h(label(asset.binding_status))}</span>
        </span>
        <p>${h(asset.description || "此资产没有目录描述；请按路径、依赖和资格证据判断。")}</p>
        <div class="binding-spine" aria-label="资产资格链">
          ${spineRow("目录物化", asset.dependencies_ready ? `${asset.dependency_count} dependencies closed` : "dependency gap", materializedState)}
          ${spineRow("执行资格", label(execution.status), executionState)}
          ${spineRow("Reference 资格", label(reference.status), referenceState)}
          ${spineRow("Runtime binding", evidence.length ? `${evidence.length} evidence record(s)` : label(asset.binding_status), runtimeState)}
        </div>
        <div class="facts">
          ${fact("Category", [asset.category, asset.subcategory].filter(Boolean).join(" / "))}
          ${fact("UE class", asset.type)}
          ${fact("Source", asset.source_kind)}
          ${fact("Quality", asset.quality_status)}
          ${fact("License", asset.license)}
          ${fact("Physics metadata", asset.physics_candidate ? label(execution.status) : "NOT REQUIRED")}
          ${fact("UE path", asset.ue_path, true)}
          ${fact("Dependencies", `${asset.dependency_count} · ${asset.dependencies_ready ? "closed" : "incomplete"}`)}
        </div>
        <details ${evidence.length ? "open" : ""}>
          <summary>Runtime binding evidence · ${evidence.length}</summary>
          <ul class="evidence-list">
            ${
              evidence.length
                ? evidence
                    .map(
                      (row) => `<li>
                        <b>${h(row.case_id)} / ${h(row.object_id)}</b><br>
                        ${h(row.runtime_actor_id)} · ${h(row.runtime_usage)} · ${h(row.collision_geometry_verification)}
                        <br><code>${h(row.report)}</code>
                      </li>`,
                    )
                    .join("")
                : "<li>未加载 runtime_actor_placement.json；当前只显示 catalog binding 状态。</li>"
            }
          </ul>
        </details>
        <details>
          <summary>资格阻塞项</summary>
          <ul class="evidence-list">
            ${(reference.failure_codes || []).map((code) => `<li>${h(code)}</li>`).join("") || "<li>无阻塞项。</li>"}
          </ul>
        </details>
      </div>`;
    byId("detail").querySelector("img")?.addEventListener("error", (event) => {
      event.currentTarget.hidden = true;
    });
  }

  async function selectAsset(assetId) {
    state.selected = assetId;
    renderGrid();
    try {
      const response = await fetch(`/api/asset?id=${encodeURIComponent(assetId)}`);
      if (!response.ok) throw new Error(`${response.status} ${response.statusText}`);
      const asset = await response.json();
      byId("placeholder").hidden = true;
      byId("detail").hidden = false;
      renderDetail(asset);
    } catch (error) {
      byId("placeholder").hidden = false;
      byId("detail").hidden = true;
      byId("placeholder").innerHTML = `<b>读取失败</b><p>${h(error.message)}</p>`;
    }
  }

  let searchTimer;
  byId("search").addEventListener("input", () => {
    clearTimeout(searchTimer);
    searchTimer = setTimeout(() => fetchPage(true), 180);
  });
  for (const id of ["category", "qualification", "binding", "source"]) {
    byId(id).addEventListener("change", () => fetchPage(true));
  }
  byId("loadMore").addEventListener("click", () => fetchPage(false));
  fetchPage(true);
})();
