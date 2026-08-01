(function (root) {
  "use strict";

  const DEFAULT_VIEWS = [
    "front_static",
    "side_static",
    "top_down",
    "tracking_subject",
    "event_closeup",
  ];
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
    const schemas = new Set([
      "harness_variant_plan_v1",
      "harness_frozen_run_control_plan_v1",
    ]);
    if (!plan || !schemas.has(plan.schema_version)) {
      throw new Error("Unsupported Harness control plan schema");
    }
    if (
      !Array.isArray(plan.axes) ||
      (!plan.axes.length && plan.schema_version === "harness_variant_plan_v1")
    ) {
      throw new Error("Parameter plan needs at least one axis");
    }
    if (!baseCase || baseCase.schema_version !== "harness_case_spec_v1") {
      throw new Error("Base file must be a harness_case_spec_v1 CaseSpec");
    }
  }

  function levelValue(axis, level, baseCase) {
    if ("value" in level) return level.value;
    const pointer =
      axis.value_pointer ||
      Object.keys(
        axis.levels.find((item) => item.id === axis.baseline)?.edits ||
          axis.levels[0]?.edits ||
          {},
      )[0];
    if (pointer && level.edits && pointer in level.edits) {
      return level.edits[pointer];
    }
    return pointer ? getPointer(baseCase, pointer) : level.id;
  }

  function initialChoices(plan, baseCase) {
    validateInputs(plan, baseCase);
    const axes = {};
    for (const axis of plan.axes) {
      const pointer =
        axis.value_pointer ||
        Object.keys(
          axis.levels.find((item) => item.id === axis.baseline)?.edits ||
            axis.levels[0]?.edits ||
            {},
        )[0];
      const current = pointer
        ? getPointer(baseCase, pointer)
        : undefined;
      const match =
        axis.levels.find(
          (level) => levelValue(axis, level, baseCase) === current,
        ) ||
        axis.levels.find((level) => level.id === axis.baseline) ||
        axis.levels[0];
      axes[axis.id] = {
        kind: "preset",
        levelId: match.id,
        customValue: levelValue(axis, match, baseCase),
      };
    }
    const fields = {};
    for (const field of plan.ui?.fields || []) {
      fields[field.id] = getPointer(baseCase, field.pointers[0]);
    }
    return {
      axes,
      fields,
      passes: clone(plan.ui?.render?.passes || ["rgb"]),
      views: clone(plan.ui?.render?.views || DEFAULT_VIEWS),
      resolution: clone(
        plan.ui?.render?.resolution || {
          id: "full_hd",
          width: 1920,
          height: 1080,
        },
      ),
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
      .join("__") || "exact_reproduction";
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

  function renderCommand(plan, filename, choices, runControl = null) {
    if (!choices.passes.length) throw new Error("Select at least one render pass");
    if (!choices.views.length) throw new Error("Select at least one camera");
    if (
      !Number.isInteger(choices.resolution?.width) ||
      !Number.isInteger(choices.resolution?.height)
    ) {
      throw new Error("Select a valid render resolution");
    }
    const passes = choices.passes.join(",");
    const mode =
      choices.passes.includes("rgb") && choices.passes.length > 1
        ? "both"
        : choices.passes.length === 1 && choices.passes[0] === "rgb"
          ? "rgb"
          : "data";
    const execution = runControl?.execution || {};
    const output = execution.reproduction_output_root
      ? `--output-root ${shellQuote(execution.reproduction_output_root)}`
      : `--case-route ${shellQuote(plan.case_route)}`;
    return [
      `${shellQuote(execution.python || "python3.13")} ${shellQuote(execution.runner || "scripts/harness_run_case.py")}`,
      shellQuote(`./${filename}`),
      `--backend ${shellQuote(execution.backend || "ue")}`,
      output,
      `--views ${shellQuote(choices.views.join(","))}`,
      `--render-passes ${shellQuote(passes)}`,
      `--mode ${mode}`,
      `--width ${choices.resolution.width}`,
      `--height ${choices.resolution.height}`,
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
      runControl: null,
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
      const planName = "config/variant_plans/glass_panel_impact_speed.json";
      showStatus("正在载入玻璃撞击参数计划…");
      try {
        const plan = await fetchJson(`../${planName}`);
        const baseCase = await fetchJson(`../${plan.base_case}`);
        useInputs(plan, baseCase, planName);
      } catch (error) {
        showStatus(
          `示例载入失败：${error.message}。请从仓库根目录启动 HTTP server，或选择两个 JSON 文件。`,
          "error",
        );
      }
    }

    function useInputs(plan, baseCase, planName, runControl = null) {
      try {
        plan.__sourceName = planName;
        state.plan = plan;
        state.baseCase = baseCase;
        state.planName = planName;
        state.choices = core.initialChoices(plan, baseCase);
        state.savedVariants = [];
        state.lastAutoLabel = "";
        state.runControl = runControl;
        if (runControl?.execution) {
          state.choices.views = clone(runControl.execution.views);
          state.choices.passes = clone(runControl.execution.render_passes);
          state.choices.resolution = {
            id: "exact_run",
            width: runControl.execution.width,
            height: runControl.execution.height,
          };
        }
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
      byId("caseRoute").textContent =
        plan.case_route || state.runControl?.execution?.output_root || "custom output";
      byId("primaryControls").innerHTML = plan.axes.length
        ? plan.axes.map((axis) => axisMarkup(axis)).join("")
        : '<p class="muted">本次运行没有声明可变 JSON Pointer；CaseSpec 保持冻结，仍可复现或调整捕获配置。</p>';
      byId("commonControls").innerHTML = fieldsMarkup("common");
      byId("advancedControls").innerHTML = fieldsMarkup("advanced");
      byId("passControls").innerHTML = checksMarkup(
        [...new Set([
          ...(plan.ui?.render?.available_passes || ["rgb", "depth", "segmentation"]),
          ...state.choices.passes,
        ])],
        state.choices.passes,
        "pass",
      );
      byId("viewControls").innerHTML = checksMarkup(
        [...new Set([...(plan.ui?.render?.views || DEFAULT_VIEWS), ...state.choices.views])],
        state.choices.views,
        "view",
      );
      byId("resolutionControls").innerHTML = resolutionMarkup();
      bindControls();
      renderRunContract();
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
            input.allow_custom && axis.value_pointer
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
              <small class="field-group">${h(field.group || "case")}</small>
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

    function resolutionMarkup() {
      const options = clone(state.plan.ui?.render?.available_resolutions || [
        { id: "hd", label: "HD · 快速检查", width: 1280, height: 720 },
        { id: "full_hd", label: "Full HD · 默认", width: 1920, height: 1080 },
        { id: "4k", label: "4K · 正式保留", width: 3840, height: 2160 },
      ]);
      if (
        !options.some(
          (option) =>
            option.width === state.choices.resolution.width &&
            option.height === state.choices.resolution.height,
        )
      ) {
        options.unshift({
          ...state.choices.resolution,
          label: "本次运行",
        });
      }
      return options
        .map(
          (option) => `
            <label class="resolution-card">
              <input type="radio" name="resolution" data-resolution="${h(option.id)}"
                data-width="${h(option.width)}" data-height="${h(option.height)}"
                ${state.choices.resolution.id === option.id ? "checked" : ""}>
              <span>
                <b>${h(option.label)}</b>
                <code>${h(option.width)} × ${h(option.height)}</code>
              </span>
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
      document.querySelectorAll("[data-resolution]").forEach((input) => {
        input.addEventListener("change", () => {
          state.choices.resolution = {
            id: input.dataset.resolution,
            width: Number(input.dataset.width),
            height: Number(input.dataset.height),
          };
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
        const command = core.renderCommand(
          state.plan,
          filename,
          state.choices,
          state.runControl,
        );
        byId("renderCommand").textContent = command;
        byId("outputName").textContent = filename;
        const labelInput = byId("variantLabel");
        if (!labelInput.value || labelInput.value === state.lastAutoLabel) {
          labelInput.value = state.output.variant_plan.variant;
        }
        state.lastAutoLabel = state.output.variant_plan.variant;
        renderDiff();
        renderResultTicket();
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

    function renderResultTicket() {
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
        byId("resultSubtitle").textContent = "联动公式的可见反馈";
        byId("resultEyebrow").textContent = "Computed incident energy";
        byId("resultNote").textContent =
          "速度、质量和阈值变化会同步更新初态、事件能量与预期损伤。";
        byId("shatterLabel").textContent = `SHATTER ${shatter} J`;
        byId("burstLabel").textContent = `BURST ${burst} J`;
        byId("energyPanel").dataset.mode = "energy";
        byId("energyPanel").hidden = false;
      } catch (_error) {
        const axis = state.plan.axes[0];
        const pointer =
          axis?.value_pointer ||
          Object.keys(
            axis?.levels.find((item) => item.id === axis.baseline)?.edits ||
              axis?.levels[0]?.edits ||
              {},
          )[0];
        const value = pointer ? core.getPointer(state.output, pointer) : "ready";
        byId("energyValue").textContent =
          `${formatValue(value)} ${axis?.input?.unit || ""}`.trim();
        byId("damageState").textContent = "CASE READY";
        byId("resultSubtitle").textContent = "当前案例的主变量与验证契约";
        byId("resultEyebrow").textContent = axis?.label || axis?.id || "Exact CaseSpec";
        byId("resultNote").textContent =
          `${state.output.capability_id} · ${state.output.objects.length} objects · ${state.output.required_signals.length} signals`;
        byId("energyPanel").dataset.mode = "generic";
        byId("energyPanel").hidden = false;
      }
    }

    function renderFiles() {
      if (state.runControl) {
        const rows = [
          ["CONTROL", "run_control.json", "复现命令、运行配置与输入哈希"],
          ["CASE", "case_spec.json", "本次运行的精确 CaseSpec"],
          ["PLAN", state.planName, state.runControl.control_mode === "variable" ? "可控变量与联动公式" : "未声明变量，输入冻结"],
          ["OUTPUT", "reproductions/", "复现运行写入独立目录，不覆盖原证据"],
          ["HTML", "run_control.html", "自包含复现控制页"],
        ];
        byId("fileList").innerHTML = rows
          .map(
            ([kind, path, note]) => `
              <li><b>${h(kind)}</b><code>${h(path)}</code><span>${h(note)}</span></li>`,
          )
          .join("");
        return;
      }
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

    function renderRunContract() {
      const control = state.runControl;
      byId("runContract").hidden = !control;
      byId("copyExactCommand").hidden = !control;
      byId("brandTitle").textContent = control ? "Physics Run Control" : "Physics Case Studio";
      for (const id of ["assetLink", "loadSample", "fileLoader"]) {
        byId(id).hidden = Boolean(control);
      }
      if (!control) return;
      byId("runStatus").textContent = control.status;
      byId("runBackend").textContent = control.execution.backend;
      byId("runControlMode").textContent = control.control_mode;
      byId("runCaseHash").textContent = control.case_sha256;
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
            resolution: clone(choices.resolution),
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
          resolution: clone(state.choices.resolution),
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
          const pointer =
            axis.value_pointer ||
            Object.keys(
              axis.levels.find((item) => item.id === axis.baseline)?.edits ||
                axis.levels[0]?.edits ||
                {},
            )[0];
          const value = pointer
            ? core.getPointer(row.case_spec, pointer)
            : row.case_spec.variant_plan.levels[axis.id];
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
                <code>${h(row.render.views.length)} views × ${h(row.render.passes.join("+"))} · ${h(row.render.resolution.width)}×${h(row.render.resolution.height)}</code>
                <small class="pipeline-track">FILE READY → RENDER PENDING → VALIDATE BLOCKED → REGENERATE —</small>
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

    async function copyExactCommand() {
      await copyText(state.runControl.reproduce_command);
      showStatus("本次运行的精确复现命令已复制。", "ready");
    }

    async function copyBatchCommand() {
      try {
        const payload = batchPayload();
        const command =
          `python3.13 scripts/harness_render_parameter_batch.py './${batchFilename()}' --prepare`;
        await copyText(command);
        showStatus(`已复制 ${payload.entries.length} 个变体的队列准备命令；不会触发渲染。`, "ready");
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
    byId("copyExactCommand").addEventListener("click", copyExactCommand);
    byId("copyCommand").addEventListener("click", copyCurrentCommand);
    byId("copyBatchCommand").addEventListener("click", copyBatchCommand);
    const embedded = byId("case-editor-data");
    if (embedded) {
      try {
        const payload = JSON.parse(embedded.textContent);
        useInputs(
          payload.plan,
          payload.base_case,
          payload.plan_name,
          payload.run_control || null,
        );
      } catch (error) {
        showStatus(`内嵌 Case 载入失败：${error.message}`, "error");
      }
    } else {
      loadSample();
    }
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
    const genericPlan = JSON.parse(
      fs.readFileSync(
        path.join(repo, "config/variant_plans/newton_cradle_release_angle.json"),
      ),
    );
    const genericCase = JSON.parse(
      fs.readFileSync(path.join(repo, genericPlan.base_case)),
    );
    const genericChoices = api.initialChoices(genericPlan, genericCase);
    const genericOutput = api.materialize(genericCase, genericPlan, genericChoices);
    assert.equal(genericOutput.expected_physics.active_release_angle_degrees, 35);
    assert.equal(genericChoices.views.length, 5);
    process.stdout.write("case_parameter_editor self-test: ok\n");
  }
})(typeof globalThis !== "undefined" ? globalThis : this);
