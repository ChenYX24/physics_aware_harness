(function (root) {
  "use strict";

  function clone(value) {
    return JSON.parse(JSON.stringify(value));
  }

  function pointerParts(pointer) {
    if (typeof pointer !== "string" || !pointer.startsWith("/")) {
      throw new Error(`Invalid JSON Pointer: ${pointer}`);
    }
    const parts = pointer
      .slice(1)
      .split("/")
      .map((part) => part.replace(/~1/g, "/").replace(/~0/g, "~"));
    if (!parts.length || parts.some((part) => part === "")) {
      throw new Error(`Invalid JSON Pointer: ${pointer}`);
    }
    return parts;
  }

  function getPointer(payload, pointer) {
    return pointerParts(pointer).reduce((value, part) => {
      if (Array.isArray(value)) {
        const index = Number(part);
        if (!Number.isInteger(index) || index < 0 || index >= value.length) {
          throw new Error(`JSON Pointer does not resolve: ${pointer}`);
        }
        return value[index];
      }
      if (!value || typeof value !== "object" || !(part in value)) {
        throw new Error(`JSON Pointer does not resolve: ${pointer}`);
      }
      return value[part];
    }, payload);
  }

  function setPointer(payload, pointer, nextValue) {
    const parts = pointerParts(pointer);
    const last = parts.pop();
    const parent = parts.reduce((value, part) => {
      if (Array.isArray(value)) {
        const index = Number(part);
        if (!Number.isInteger(index) || index < 0 || index >= value.length) {
          throw new Error(`JSON Pointer does not resolve: ${pointer}`);
        }
        return value[index];
      }
      if (!value || typeof value !== "object" || !(part in value)) {
        throw new Error(`JSON Pointer does not resolve: ${pointer}`);
      }
      return value[part];
    }, payload);
    if (Array.isArray(parent)) {
      const index = Number(last);
      if (!Number.isInteger(index) || index < 0 || index >= parent.length) {
        throw new Error(`JSON Pointer does not resolve: ${pointer}`);
      }
      parent[index] = clone(nextValue);
    } else if (parent && typeof parent === "object" && last in parent) {
      parent[last] = clone(nextValue);
    } else {
      throw new Error(`JSON Pointer does not resolve: ${pointer}`);
    }
  }

  function finiteNumber(value, operator) {
    if (typeof value !== "number" || !Number.isFinite(value)) {
      throw new Error(`Computed edit ${operator} requires finite numbers`);
    }
    return value;
  }

  function evaluateExpression(expression, payload) {
    if (
      expression === null ||
      ["string", "number", "boolean"].includes(typeof expression)
    ) {
      return clone(expression);
    }
    if (!expression || typeof expression !== "object" || Array.isArray(expression)) {
      throw new Error("Computed edit expression must be a literal or object");
    }
    if (Object.keys(expression).length === 1 && "path" in expression) {
      return clone(getPointer(payload, expression.path));
    }
    const op = expression.op;
    if (op === "bands") {
      const value = finiteNumber(evaluateExpression(expression.value, payload), op);
      for (const band of expression.bands || []) {
        if (
          !("lt" in band) ||
          value < finiteNumber(evaluateExpression(band.lt, payload), op)
        ) {
          return clone(band.result);
        }
      }
      throw new Error("Computed edit bands has no matching/default result");
    }
    if (!["add", "sub", "mul", "div", "pow"].includes(op)) {
      throw new Error(`Unsupported computed edit operator: ${op}`);
    }
    const args = (expression.args || []).map((arg) =>
      finiteNumber(evaluateExpression(arg, payload), op),
    );
    if (!args.length || (["sub", "div", "pow"].includes(op) && args.length !== 2)) {
      throw new Error(`Computed edit ${op} has invalid args`);
    }
    let result;
    if (op === "add") result = args.reduce((sum, value) => sum + value, 0);
    if (op === "mul") result = args.reduce((product, value) => product * value, 1);
    if (op === "sub") result = args[0] - args[1];
    if (op === "div") result = args[0] / args[1];
    if (op === "pow") result = args[0] ** args[1];
    return finiteNumber(result, op);
  }

  function validateInputs(plan, baseCase) {
    if (!plan || plan.schema_version !== "harness_variant_plan_v1") {
      throw new Error("Parameter plan must use schema harness_variant_plan_v1");
    }
    if (!Array.isArray(plan.axes) || !plan.axes.length) {
      throw new Error("Parameter plan needs at least one axis");
    }
    if (!baseCase || baseCase.schema_version !== "harness_case_spec_v1") {
      throw new Error("Base file must be a harness_case_spec_v1 CaseSpec");
    }
  }

  function levelValue(axis, level, baseCase) {
    if ("value" in level) return level.value;
    if (axis.value_pointer && level.edits && axis.value_pointer in level.edits) {
      return level.edits[axis.value_pointer];
    }
    return axis.value_pointer ? getPointer(baseCase, axis.value_pointer) : null;
  }

  function initialChoices(plan, baseCase) {
    validateInputs(plan, baseCase);
    const axes = {};
    for (const axis of plan.axes) {
      const current = axis.value_pointer
        ? getPointer(baseCase, axis.value_pointer)
        : undefined;
      const match = axis.levels.find(
        (level) => levelValue(axis, level, baseCase) === current,
      );
      axes[axis.id] = match
        ? { kind: "preset", levelId: match.id, customValue: current }
        : { kind: "custom", customValue: current };
    }
    const fields = {};
    for (const field of plan.ui?.fields || []) {
      fields[field.id] = getPointer(baseCase, field.pointers[0]);
    }
    return {
      axes,
      fields,
      passes: clone(plan.ui?.render?.passes || ["rgb"]),
      views: clone(plan.ui?.render?.views || []),
    };
  }

  function variantId(plan, choices) {
    return plan.axes
      .map((axis) => {
        const choice = choices.axes[axis.id];
        if (choice.kind === "preset") return choice.levelId;
        const value = String(choice.customValue)
          .replace(/[^a-zA-Z0-9.-]+/g, "_")
          .replace(/\./g, "p");
        return `${axis.id}-${value}`;
      })
      .join("__");
  }

  function materialize(baseCase, plan, choices) {
    validateInputs(plan, baseCase);
    const payload = clone(baseCase);
    const levels = {};
    const customValues = {};
    for (const axis of plan.axes) {
      const choice = choices.axes[axis.id];
      if (!choice) throw new Error(`Missing choice for axis: ${axis.id}`);
      if (choice.kind === "preset") {
        const level = axis.levels.find((item) => item.id === choice.levelId);
        if (!level) throw new Error(`Unknown level ${choice.levelId} for ${axis.id}`);
        for (const [pointer, value] of Object.entries(level.edits || {})) {
          setPointer(payload, pointer, value);
        }
        levels[axis.id] = level.id;
      } else {
        if (!axis.value_pointer) {
          throw new Error(`Axis ${axis.id} does not declare value_pointer`);
        }
        setPointer(payload, axis.value_pointer, choice.customValue);
        levels[axis.id] = "custom";
        customValues[axis.id] = choice.customValue;
      }
    }
    for (const field of plan.ui?.fields || []) {
      const value = choices.fields[field.id];
      for (const pointer of field.pointers) setPointer(payload, pointer, value);
    }
    const computed = plan.ui?.computed_edits || {};
    for (const [pointer, expression] of Object.entries(computed)) {
      setPointer(payload, pointer, evaluateExpression(expression, payload));
    }
    const id = variantId(plan, choices);
    payload.case_id = `${baseCase.case_id}__${id}`;
    payload.variant_plan = {
      schema_version: plan.schema_version,
      plan: plan.__sourceName || "parameter-plan.json",
      variant: id,
      levels,
      custom_values: customValues,
      computed_pointers: Object.keys(computed),
      editor: "tools/case_parameter_editor.html",
    };
    return payload;
  }

  function diffPointers(before, after) {
    const rows = [];
    function visit(left, right, pointer) {
      if (JSON.stringify(left) === JSON.stringify(right)) return;
      const bothArrays = Array.isArray(left) && Array.isArray(right);
      const bothObjects =
        !bothArrays &&
        left &&
        right &&
        typeof left === "object" &&
        typeof right === "object" &&
        !Array.isArray(left) &&
        !Array.isArray(right);
      if (bothArrays || bothObjects) {
        const keys = new Set([
          ...Object.keys(left || {}),
          ...Object.keys(right || {}),
        ]);
        for (const key of keys) {
          const escaped = String(key).replace(/~/g, "~0").replace(/\//g, "~1");
          visit(left?.[key], right?.[key], `${pointer}/${escaped}`);
        }
        return;
      }
      rows.push({ pointer: pointer || "/", before: left, after: right });
    }
    visit(before, after, "");
    return rows;
  }

  function renderCommand(plan, filename, choices) {
    if (!choices.passes.length) throw new Error("Select at least one render pass");
    if (!choices.views.length) throw new Error("Select at least one camera");
    const passes = choices.passes.join(",");
    const mode =
      choices.passes.includes("rgb") && choices.passes.length > 1
        ? "both"
        : choices.passes.length === 1 && choices.passes[0] === "rgb"
          ? "rgb"
          : "data";
    return [
      "python3.13 scripts/harness_run_case.py",
      shellQuote(`./${filename}`),
      "--backend ue",
      `--case-route ${shellQuote(plan.case_route)}`,
      `--views ${shellQuote(choices.views.join(","))}`,
      `--render-passes ${shellQuote(passes)}`,
      `--mode ${mode}`,
    ].join(" \\\n  ");
  }

  function shellQuote(value) {
    return `'${String(value).replace(/'/g, `'\\''`)}'`;
  }

  const api = {
    clone,
    diffPointers,
    evaluateExpression,
    getPointer,
    initialChoices,
    levelValue,
    materialize,
    renderCommand,
    setPointer,
  };

  root.CaseParameterEditorCore = api;
  if (typeof module !== "undefined" && module.exports) module.exports = api;

  if (typeof document !== "undefined") {
    document.addEventListener("DOMContentLoaded", () => startEditor(api));
  }

  function startEditor(core) {
    const state = {
      plan: null,
      baseCase: null,
      choices: null,
      output: null,
      planName: "parameter-plan.json",
      savedVariants: [],
      lastAutoLabel: "",
    };
    const byId = (id) => document.getElementById(id);
    const status = byId("status");

    function h(value) {
      return String(value)
        .replace(/&/g, "&amp;")
        .replace(/</g, "&lt;")
        .replace(/>/g, "&gt;")
        .replace(/"/g, "&quot;")
        .replace(/'/g, "&#039;");
    }

    function showStatus(message, kind = "neutral") {
      status.textContent = message;
      status.dataset.kind = kind;
    }

    async function fetchJson(path) {
      const response = await fetch(path);
      if (!response.ok) throw new Error(`${response.status} ${response.statusText}: ${path}`);
      return response.json();
    }

    async function loadSample() {
      showStatus("正在载入玻璃撞击参数计划…");
      try {
        const plan = await fetchJson(
          "../config/variant_plans/glass_panel_impact_speed.json",
        );
        const baseCase = await fetchJson(`../${plan.base_case}`);
        useInputs(plan, baseCase, "config/variant_plans/glass_panel_impact_speed.json");
      } catch (error) {
        showStatus(
          `示例载入失败：${error.message}。请从仓库根目录启动 HTTP server，或选择两个 JSON 文件。`,
          "error",
        );
      }
    }

    function useInputs(plan, baseCase, planName) {
      try {
        plan.__sourceName = planName;
        state.plan = plan;
        state.baseCase = baseCase;
        state.planName = planName;
        state.choices = core.initialChoices(plan, baseCase);
        state.savedVariants = [];
        state.lastAutoLabel = "";
        byId("variantLabel").value = "";
        renderShell();
        recompute();
        seedPlannedVariants();
        renderQueue();
        showStatus(`已载入 ${plan.ui?.title || baseCase.case_id}`, "ready");
      } catch (error) {
        showStatus(error.message, "error");
      }
    }

    async function loadFiles(files) {
      try {
        const payloads = await Promise.all(
          [...files].map(async (file) => ({
            name: file.name,
            value: JSON.parse(await file.text()),
          })),
        );
        const planFile = payloads.find(
          (item) => item.value.schema_version === "harness_variant_plan_v1",
        );
        const caseFile = payloads.find(
          (item) => item.value.schema_version === "harness_case_spec_v1",
        );
        let plan = planFile?.value || state.plan;
        let baseCase = caseFile?.value;
        if (!plan) throw new Error("没有找到 harness_variant_plan_v1 JSON");
        if (!baseCase) {
          baseCase = await fetchJson(`../${plan.base_case}`);
        }
        useInputs(plan, baseCase, planFile?.name || state.planName);
      } catch (error) {
        showStatus(`JSON 载入失败：${error.message}`, "error");
      }
    }

    function renderShell() {
      const plan = state.plan;
      byId("emptyState").hidden = true;
      byId("editor").hidden = false;
      byId("caseTitle").textContent = plan.ui?.title || state.baseCase.case_id;
      byId("caseSummary").textContent = plan.ui?.summary || state.baseCase.prompt;
      byId("caseRoute").textContent = plan.case_route;
      byId("primaryControls").innerHTML = plan.axes
        .map((axis) => axisMarkup(axis))
        .join("");
      byId("commonControls").innerHTML = fieldsMarkup("common");
      byId("advancedControls").innerHTML = fieldsMarkup("advanced");
      byId("passControls").innerHTML = checksMarkup(
        plan.ui?.render?.available_passes || ["rgb", "depth", "segmentation"],
        state.choices.passes,
        "pass",
      );
      byId("viewControls").innerHTML = checksMarkup(
        plan.ui?.render?.views || [],
        state.choices.views,
        "view",
      );
      bindControls();
      renderFiles();
      renderQueue();
    }

    function axisMarkup(axis) {
      const choice = state.choices.axes[axis.id];
      const input = axis.input || {};
      const custom = choice.kind === "custom";
      const buttons = axis.levels
        .map(
          (level) => `
            <button class="preset ${choice.levelId === level.id && !custom ? "selected" : ""}"
              type="button" data-axis="${h(axis.id)}" data-level="${h(level.id)}">
              <span>${h(level.label || level.id)}</span>
              <strong>${h(core.levelValue(axis, level, state.baseCase))} ${h(input.unit || "")}</strong>
            </button>`,
        )
        .join("");
      return `
        <fieldset class="axis-block">
          <legend>${h(axis.label || axis.id)}</legend>
          <p class="micro">PRIMARY AXIS · ${h(axis.value_pointer || "")}</p>
          <div class="preset-row">${buttons}</div>
          ${
            input.allow_custom
              ? `<label class="custom-value ${custom ? "active" : ""}">
                  <input type="radio" name="axis-${h(axis.id)}" data-custom-axis="${h(axis.id)}"
                    ${custom ? "checked" : ""}>
                  <span>自定义</span>
                  <input type="number" data-axis-value="${h(axis.id)}"
                    value="${h(choice.customValue)}"
                    min="${h(input.min ?? "")}" max="${h(input.max ?? "")}"
                    step="${h(input.step ?? "any")}">
                  <b>${h(input.unit || "")}</b>
                </label>`
              : ""
          }
        </fieldset>`;
    }

    function fieldsMarkup(tier) {
      const rows = (state.plan.ui?.fields || []).filter((field) => field.tier === tier);
      if (!rows.length) return '<p class="muted">此计划没有这一层参数。</p>';
      return rows
        .map((field) => {
          const warning = field.warning
            ? `<p class="field-warning">${h(field.warning)}</p>`
            : "";
          return `
            <label class="field-control">
              <span>${h(field.label || field.id)}</span>
              <div class="field-input">
                <input data-field="${h(field.id)}" type="${field.type === "text" ? "text" : "number"}"
                  value="${h(state.choices.fields[field.id])}"
                  min="${h(field.min ?? "")}" max="${h(field.max ?? "")}"
                  step="${h(field.step ?? "any")}">
                <b>${h(field.unit || "")}</b>
              </div>
              <code>${h(field.pointers.join(" · "))}</code>
              ${warning}
            </label>`;
        })
        .join("");
    }

    function checksMarkup(items, selected, kind) {
      return items
        .map(
          (item) => `
            <label class="check-chip">
              <input type="checkbox" data-${kind}="${h(item)}"
                ${selected.includes(item) ? "checked" : ""}>
              <span>${h(item.replaceAll("_", " "))}</span>
            </label>`,
        )
        .join("");
    }

    function bindControls() {
      document.querySelectorAll("[data-axis][data-level]").forEach((button) => {
        button.addEventListener("click", () => {
          const axis = state.plan.axes.find((item) => item.id === button.dataset.axis);
          const level = axis.levels.find((item) => item.id === button.dataset.level);
          state.choices.axes[axis.id] = {
            kind: "preset",
            levelId: level.id,
            customValue: core.levelValue(axis, level, state.baseCase),
          };
          renderShell();
          recompute();
        });
      });
      document.querySelectorAll("[data-custom-axis]").forEach((input) => {
        input.addEventListener("change", () => {
          state.choices.axes[input.dataset.customAxis].kind = "custom";
          renderShell();
          recompute();
        });
      });
      document.querySelectorAll("[data-axis-value]").forEach((input) => {
        input.addEventListener("input", () => {
          const choice = state.choices.axes[input.dataset.axisValue];
          choice.kind = "custom";
          choice.customValue = Number(input.value);
          recompute();
        });
      });
      document.querySelectorAll("[data-field]").forEach((input) => {
        input.addEventListener("input", () => {
          const field = state.plan.ui.fields.find(
            (item) => item.id === input.dataset.field,
          );
          state.choices.fields[field.id] =
            field.type === "text" ? input.value : Number(input.value);
          recompute();
        });
      });
      document.querySelectorAll("[data-pass]").forEach((input) => {
        input.addEventListener("change", () => {
          state.choices.passes = checkedValues("pass");
          recompute();
        });
      });
      document.querySelectorAll("[data-view]").forEach((input) => {
        input.addEventListener("change", () => {
          state.choices.views = checkedValues("view");
          recompute();
        });
      });
    }

    function checkedValues(kind) {
      return [...document.querySelectorAll(`[data-${kind}]:checked`)].map(
        (input) => input.dataset[kind],
      );
    }

    function recompute() {
      try {
        document.querySelectorAll("input").forEach((input) => {
          if (!input.checkValidity()) {
            throw new Error(`参数超出允许范围：${input.closest("label")?.innerText || input.value}`);
          }
        });
        state.output = core.materialize(
          state.baseCase,
          state.plan,
          state.choices,
        );
        const filename = `${state.output.case_id}.json`;
        const command = core.renderCommand(state.plan, filename, state.choices);
        byId("renderCommand").textContent = command;
        byId("outputName").textContent = filename;
        const labelInput = byId("variantLabel");
        if (!labelInput.value || labelInput.value === state.lastAutoLabel) {
          labelInput.value = state.output.variant_plan.variant;
        }
        state.lastAutoLabel = state.output.variant_plan.variant;
        renderDiff();
        renderEnergy();
        showStatus("参数有效，可以导出并渲染。", "ready");
      } catch (error) {
        state.output = null;
        byId("renderCommand").textContent = "修正参数后生成命令";
        showStatus(error.message, "error");
      }
    }

    function renderDiff() {
      const ignored = new Set(["/variant_plan"]);
      const rows = core
        .diffPointers(state.baseCase, state.output)
        .filter((row) => ![...ignored].some((prefix) => row.pointer.startsWith(prefix)));
      byId("changeCount").textContent = `${rows.length} POINTERS`;
      byId("diffList").innerHTML = rows
        .map(
          (row) => `
            <li>
              <code>${h(row.pointer)}</code>
              <span>${h(formatValue(row.before))}</span>
              <i>→</i>
              <strong>${h(formatValue(row.after))}</strong>
            </li>`,
        )
        .join("");
    }

    function formatValue(value) {
      if (value === undefined) return "—";
      if (typeof value === "string") return value;
      return JSON.stringify(value);
    }

    function renderEnergy() {
      try {
        const energy = core.getPointer(
          state.output,
          "/physical_parameters/nominal_incident_energy_j",
        );
        const shatter = core.getPointer(
          state.output,
          "/physical_parameters/energy_response_curve_j/shattered",
        );
        const burst = core.getPointer(
          state.output,
          "/physical_parameters/energy_response_curve_j/burst",
        );
        const damage = core.getPointer(
          state.output,
          "/expected_physics/expected_damage_state",
        );
        const ceiling = Math.max(burst * 1.25, energy * 1.1, 1);
        byId("energyMarker").style.bottom = `${Math.min(94, (energy / ceiling) * 100)}%`;
        byId("shatterMark").style.bottom = `${(shatter / ceiling) * 100}%`;
        byId("burstMark").style.bottom = `${Math.min(98, (burst / ceiling) * 100)}%`;
        byId("energyValue").textContent = `${Number(energy.toFixed(3))} J`;
        byId("damageState").textContent = damage;
        byId("shatterLabel").textContent = `SHATTER ${shatter} J`;
        byId("burstLabel").textContent = `BURST ${burst} J`;
        byId("energyPanel").hidden = false;
      } catch (_error) {
        byId("energyPanel").hidden = true;
      }
    }

    function renderFiles() {
      const rows = [
        ["PLAN", state.planName, "参数轴、默认档和联动公式"],
        ["BASE", state.plan.base_case, "作为输入读取，不覆盖"],
        ["OUTPUT", "下载后的 batch JSON", "内嵌所有选中 CaseSpec 与捕获选择"],
        ["INPUTS", "inputs/parameter_batches/", "脚本执行时保存独立 CaseSpec"],
        ["ROUTE", state.plan.case_route, "运行结果目录"],
      ];
      byId("fileList").innerHTML = rows
        .map(
          ([kind, path, note]) => `
            <li><b>${h(kind)}</b><code>${h(path)}</code><span>${h(note)}</span></li>`,
        )
        .join("");
    }

    function seedPlannedVariants() {
      for (const selected of state.plan.selected_variants || []) {
        const choices = core.initialChoices(state.plan, state.baseCase);
        for (const [axisId, levelId] of Object.entries(selected.levels || {})) {
          choices.axes[axisId] = {
            kind: "preset",
            levelId,
            customValue: choices.axes[axisId].customValue,
          };
        }
        const caseSpec = core.materialize(state.baseCase, state.plan, choices);
        caseSpec.case_id = `${state.baseCase.case_id}__${selected.id}`;
        caseSpec.variant_plan.variant = selected.id;
        state.savedVariants.push({
          id: selected.id,
          label: selected.id,
          planned: true,
          selected: selected.id === "baseline",
          case_spec: caseSpec,
          render: {
            views: clone(choices.views),
            passes: clone(choices.passes),
          },
        });
      }
    }

    function safeVariantLabel(value) {
      return String(value)
        .trim()
        .toLowerCase()
        .replace(/[^a-z0-9_-]+/g, "_")
        .replace(/^[_-]+|[_-]+$/g, "");
    }

    function saveCurrentVariant() {
      if (!state.output) return;
      const label = safeVariantLabel(byId("variantLabel").value);
      if (!label) {
        showStatus("请填写只含字母、数字、横线或下划线的变体名称。", "error");
        return;
      }
      const caseSpec = clone(state.output);
      caseSpec.case_id = `${state.baseCase.case_id}__${label}`;
      caseSpec.variant_plan.variant = label;
      const row = {
        id: label,
        label,
        planned: false,
        selected: true,
        case_spec: caseSpec,
        render: {
          views: clone(state.choices.views),
          passes: clone(state.choices.passes),
        },
      };
      const index = state.savedVariants.findIndex((item) => item.id === label);
      if (index >= 0) state.savedVariants[index] = row;
      else state.savedVariants.push(row);
      renderQueue();
      showStatus(
        `${index >= 0 ? "已更新" : "已保存"}变体 ${label}；可勾选后导出批次。`,
        "ready",
      );
    }

    function variantSummary(row) {
      return state.plan.axes
        .map((axis) => {
          const value = core.getPointer(row.case_spec, axis.value_pointer);
          return `${axis.label || axis.id} ${value}${axis.input?.unit || ""}`;
        })
        .join(" · ");
    }

    function renderQueue() {
      if (!state.plan) return;
      byId("queueCount").textContent =
        `${state.savedVariants.filter((row) => row.selected).length} / ${state.savedVariants.length} SELECTED`;
      byId("variantQueue").innerHTML = state.savedVariants
        .map(
          (row) => `
            <li>
              <label class="queue-select">
                <input type="checkbox" data-queue-select="${h(row.id)}"
                  ${row.selected ? "checked" : ""}>
                <span></span>
              </label>
              <div>
                <strong>${h(row.label)}</strong>
                ${row.planned ? '<b class="planned-badge">MODEL PLANNED</b>' : '<b class="custom-badge">SAVED EDIT</b>'}
                <p>${h(variantSummary(row))}</p>
                <code>${h(row.render.views.length)} views × ${h(row.render.passes.join("+"))}</code>
              </div>
              <button type="button" data-queue-remove="${h(row.id)}" aria-label="移除 ${h(row.label)}">×</button>
            </li>`,
        )
        .join("");
      document.querySelectorAll("[data-queue-select]").forEach((input) => {
        input.addEventListener("change", () => {
          const row = state.savedVariants.find(
            (item) => item.id === input.dataset.queueSelect,
          );
          row.selected = input.checked;
          renderQueue();
        });
      });
      document.querySelectorAll("[data-queue-remove]").forEach((button) => {
        button.addEventListener("click", () => {
          state.savedVariants = state.savedVariants.filter(
            (item) => item.id !== button.dataset.queueRemove,
          );
          renderQueue();
        });
      });
    }

    function batchPayload() {
      const entries = state.savedVariants
        .filter((row) => row.selected)
        .map(({ id, case_spec, render }) => ({ id, case_spec, render }));
      if (!entries.length) throw new Error("请至少勾选一个已保存变体");
      return {
        schema_version: "harness_parameter_batch_v1",
        batch_id: `${state.baseCase.case_id}_parameter_batch`,
        case_route: state.plan.case_route,
        plan_source: state.planName,
        entries,
      };
    }

    function batchFilename() {
      return `${state.baseCase.case_id}__render_batch.json`;
    }

    function downloadBatch() {
      let payload;
      try {
        payload = batchPayload();
      } catch (error) {
        showStatus(error.message, "error");
        return;
      }
      const filename = batchFilename();
      const blob = new Blob([`${JSON.stringify(payload, null, 2)}\n`], {
        type: "application/json",
      });
      const url = URL.createObjectURL(blob);
      const link = document.createElement("a");
      link.href = url;
      link.download = filename;
      link.click();
      URL.revokeObjectURL(url);
      showStatus(`已导出 ${payload.entries.length} 个变体：${filename}`, "ready");
    }

    async function copyText(value) {
      try {
        await navigator.clipboard.writeText(value);
      } catch (_error) {
        const textarea = document.createElement("textarea");
        textarea.value = value;
        document.body.appendChild(textarea);
        textarea.select();
        document.execCommand("copy");
        textarea.remove();
      }
    }

    async function copyCurrentCommand() {
      await copyText(byId("renderCommand").textContent);
      showStatus("当前单变体渲染命令已复制。", "ready");
    }

    async function copyBatchCommand() {
      try {
        const payload = batchPayload();
        const command =
          `python3.13 scripts/harness_render_parameter_batch.py './${batchFilename()}' --execute`;
        await copyText(command);
        showStatus(`已复制 ${payload.entries.length} 个变体的批次渲染命令。`, "ready");
      } catch (error) {
        showStatus(error.message, "error");
      }
    }

    byId("loadSample").addEventListener("click", loadSample);
    byId("jsonFiles").addEventListener("change", (event) =>
      loadFiles(event.target.files),
    );
    byId("variantLabel").addEventListener("input", () => {
      state.lastAutoLabel = "";
    });
    byId("saveVariant").addEventListener("click", saveCurrentVariant);
    byId("downloadBatch").addEventListener("click", downloadBatch);
    byId("copyCommand").addEventListener("click", copyCurrentCommand);
    byId("copyBatchCommand").addEventListener("click", copyBatchCommand);
    loadSample();
  }

  if (
    typeof module !== "undefined" &&
    module.exports &&
    typeof require !== "undefined" &&
    require.main === module
  ) {
    const assert = require("node:assert");
    const fs = require("node:fs");
    const path = require("node:path");
    const repo = path.resolve(__dirname, "..");
    const plan = JSON.parse(
      fs.readFileSync(
        path.join(repo, "config/variant_plans/glass_panel_impact_speed.json"),
      ),
    );
    const baseCase = JSON.parse(fs.readFileSync(path.join(repo, plan.base_case)));
    const choices = api.initialChoices(plan, baseCase);
    choices.axes.impact_speed = { kind: "custom", customValue: 2.5 };
    const output = api.materialize(baseCase, plan, choices);
    assert.equal(output.physical_parameters.nominal_incident_energy_j, 25);
    assert.equal(output.objects[0].initial_position_m[1], -0.705);
    assert.equal(output.expected_physics.expected_damage_state, "burst");
    assert.match(
      api.renderCommand(plan, `${output.case_id}.json`, choices),
      /--views 'front_static,side_static,top_down,tracking_subject,event_closeup'/,
    );
    process.stdout.write("case_parameter_editor self-test: ok\n");
  }
})(typeof globalThis !== "undefined" ? globalThis : this);
