(() => {
  const root = document.getElementById("cascade-lab");
  if (!root) return;

  const form = document.getElementById("lab-run-form");
  const message = document.getElementById("lab-message");
  const nodes = [...document.querySelectorAll(".cascade-node")];
  const inputTerminal = document.getElementById("lab-step-input");
  const outputTerminal = document.getElementById("lab-step-output");
  const inputReadable = document.getElementById("lab-step-input-readable");
  const outputReadable = document.getElementById("lab-step-output-readable");
  const metrics = document.getElementById("lab-step-metrics");
  const previousButton = document.getElementById("lab-view-prev");
  const followingButton = document.getElementById("lab-view-next");
  const runNextButton = document.getElementById("lab-run-next");
  const rerunButton = document.getElementById("lab-rerun-step");
  let activeRun = null;
  let selectedStep = null;

  const csrf = () => form.querySelector("[name=csrfmiddlewaretoken]").value;
  const pretty = value => JSON.stringify(value ?? null, null, 2);
  const metric = (label, value) => `<span>${label}: <strong>${value}</strong></span>`;
  const snapshot = step => activeRun?.snapshots?.find(item => item.step === Number(step));
  const availableStep = () => Math.min(8, activeRun?.current_step || 0);

  const cellText = value => value === null || value === undefined || value === "" ? "—" : String(value);

  function appendSummary(target, text, tone = "") {
    const summary = document.createElement("p");
    summary.className = `cascade-lab__data-summary ${tone}`.trim();
    summary.textContent = text;
    target.append(summary);
  }

  function appendTable(target, columns, rows) {
    const wrapper = document.createElement("div");
    wrapper.className = "cascade-lab__table-wrap";
    const table = document.createElement("table");
    const head = document.createElement("thead");
    const headRow = document.createElement("tr");
    columns.forEach(column => {
      const th = document.createElement("th");
      th.textContent = column.label;
      headRow.append(th);
    });
    head.append(headRow);
    const body = document.createElement("tbody");
    rows.forEach(row => {
      const tr = document.createElement("tr");
      columns.forEach(column => {
        const td = document.createElement("td");
        const value = typeof column.value === "function" ? column.value(row) : row[column.value];
        if (value instanceof Node) td.append(value);
        else td.textContent = cellText(value);
        tr.append(td);
      });
      body.append(tr);
    });
    table.append(head, body);
    wrapper.append(table);
    target.append(wrapper);
  }

  function renderLine(target, value) {
    appendSummary(target, `${cellText(value.name)} · количество: ${cellText(value.quantity)}`);
    const requirements = value.requirements?.requirements || [];
    if (requirements.length) appendTable(target, [
      {label: "Параметр", value: "label"},
      {label: "Требование", value: "value"},
      {label: "Учитывать", value: row => row.selected === false ? "Нет" : "Да"},
    ], requirements);
  }

  function renderCriteria(target, rows) {
    appendSummary(target, `${rows.filter(row => row.checked !== false).length} учитываются · ${rows.length} всего`);
    appendTable(target, [
      {label: "Учитывать", value: row => row.checked === false ? "Нет" : "Да"},
      {label: "Параметр", value: row => row.concept || row.label},
      {label: "Условие", value: "operator"},
      {label: "Значение", value: row => [row.value, row.unit].filter(Boolean).join(" ")},
      {label: "Важность", value: row => row.importance ?? "—"},
      {label: "Почему", value: "importance_reason"},
    ], rows);
  }

  function renderProducts(target, value, step, side) {
    const rows = value.preview || value;
    const total = value.count ?? rows.length;
    appendSummary(target, `Найдено: ${total}. Показано: ${rows.length}.`);
    appendTable(target, [
      {label: "Товар", value: row => {
        const name = row.name || row.title || row.id;
        if (step !== 8 || side !== "output" || !row.url) return name;
        const link = document.createElement("a");
        link.href = row.url;
        link.target = "_blank";
        link.rel = "noopener";
        link.textContent = name;
        return link;
      }},
      {label: "Поставщик", value: row => row.supplier_name || row.supplier_code || row.supplier},
      {label: "Артикул", value: row => row.article || row.id || row.external_id},
      {label: "Цена, ₽", value: "price"},
      {label: "Остаток", value: row => row.stock ?? row.total_stock},
      {label: "Проверка", value: row => {
        const yes = row.match_count ?? row.matches?.length;
        const no = row.mismatch_count ?? row.mismatches?.length;
        const unknown = row.unknown_count ?? row.unknown?.length;
        return [yes !== undefined ? `Да ${yes}` : "", no !== undefined ? `Нет ${no}` : "", unknown !== undefined ? `НЗ ${unknown}` : ""].filter(Boolean).join(" · ");
      }},
    ], rows);
    if (step === 6 && side === "output") {
      const cells = rows.flatMap(row => (row.matrix || []).map(cell => ({product: row.name || row.id, ...cell})));
      if (cells.length) {
        appendSummary(target, "Подробная матрица по каждому требованию");
        appendTable(target, [
          {label: "Товар", value: "product"},
          {label: "Параметр ТЗ", value: "criterion"},
          {label: "Требуется", value: "required"},
          {label: "Результат", value: row => ({yes: "Да", no: "Нет", unknown: "НЗ", not_checked: "Не проверено"}[row.verdict] || row.verdict)},
          {label: "Почему", value: "reason"},
          {label: "Источник", value: row => ({code: "Код", cache: "Кэш", agent: "Агент", not_checked: "Не проверено"}[row.source] || row.source)},
        ], cells);
      }
    }
  }

  function renderPhrases(target, rows, step, side) {
    if (step === 2 && side === "output") {
      const limits = activeRun?.settings?.steps?.["2"] || {};
      const minimum = Number(limits.min_phrases || 12);
      const maximum = Number(limits.max_phrases || 24);
      appendSummary(
        target,
        `${rows.length} фраз · минимум ${minimum} · максимум ${maximum}`,
        rows.length >= minimum ? "is-ok" : "is-warning",
      );
    } else {
      appendSummary(target, `${rows.length} поисковых фраз`);
    }
    const list = document.createElement("ol");
    list.className = "cascade-lab__phrase-list";
    rows.forEach(value => {
      const item = document.createElement("li");
      item.textContent = value;
      list.append(item);
    });
    target.append(list);
  }

  function renderObject(target, value) {
    const rows = Object.entries(value).filter(([, item]) => (
      item === null || ["string", "number", "boolean"].includes(typeof item)
    )).map(([key, item]) => ({key, value: cellText(item)}));
    if (rows.length) appendTable(target, [
      {label: "Поле", value: "key"},
      {label: "Значение", value: "value"},
    ], rows);
    else appendSummary(target, "Структурированные данные доступны во вкладке JSON.");
  }

  function renderReadable(target, value, step, side) {
    target.replaceChildren();
    if (value === undefined) {
      appendSummary(target, "До этого шага ещё нет входных данных.");
    } else if (value === null) {
      appendSummary(target, "Нет данных.");
    } else if (value?.kind === "catalog_products") {
      renderProducts(target, value, step, side);
    } else if (Array.isArray(value) && value.every(item => typeof item === "string")) {
      renderPhrases(target, value, step, side);
    } else if (Array.isArray(value) && value.some(item => item && typeof item === "object" && "checked" in item)) {
      renderCriteria(target, value);
    } else if (Array.isArray(value) && value.some(item => item && typeof item === "object")) {
      renderProducts(target, value, step, side);
    } else if (value && typeof value === "object" && value.name && value.requirements) {
      renderLine(target, value);
    } else if (value && typeof value === "object") {
      renderObject(target, value);
    } else {
      appendSummary(target, cellText(value));
    }
  }

  function setIoView(side, view) {
    const section = document.querySelector(`[data-io-side="${side}"]`);
    section.querySelectorAll("[data-io-view]").forEach(button => button.classList.toggle("is-active", button.dataset.ioView === view));
    section.querySelector("pre").hidden = view !== "json";
    section.querySelector(".cascade-lab__readable").hidden = view !== "readable";
    localStorage.setItem(`cascade-lab-${side}-view`, view);
  }

  function renderChecks(run) {
    const panel = document.getElementById("lab-checks");
    const list = document.getElementById("lab-check-list");
    const checks = run.result?.checks || [];
    panel.hidden = !checks.length;
    list.replaceChildren();
    checks.forEach(check => {
      const row = document.createElement("div");
      row.className = `cascade-lab__check ${check.passed ? "is-passed" : "is-failed"}`;
      const mark = document.createElement("strong");
      mark.textContent = check.passed ? "✓" : "×";
      const label = document.createElement("span");
      label.textContent = check.label;
      if (check.actual !== undefined) {
        const actual = document.createElement("small");
        actual.textContent = `Факт: ${JSON.stringify(check.actual)}`;
        label.append(document.createElement("br"), actual);
      }
      row.append(mark, label);
      list.append(row);
    });
  }

  function plannedInput(step) {
    if (step === 1) return activeRun?.input_payload?.requirements || {};
    if (step === 2) return { name: activeRun?.input_payload?.name || "" };
    return snapshot(step - 1)?.output;
  }

  function selectStep(step) {
    selectedStep = Math.max(1, Math.min(8, Number(step)));
    nodes.forEach(node => node.classList.toggle("is-selected", Number(node.dataset.step) === selectedStep));
    const item = snapshot(selectedStep);
    const node = nodes[selectedStep - 1];
    document.getElementById("lab-step-title").textContent = `${selectedStep}. ${node.querySelector("strong").textContent}`;
    document.getElementById("lab-step-method").textContent = node.dataset.method;
    if (item) {
      inputTerminal.textContent = pretty(item.input);
      outputTerminal.textContent = pretty(item.output);
      renderReadable(inputReadable, item.input, selectedStep, "input");
      renderReadable(outputReadable, item.output, selectedStep, "output");
      const values = item.metrics || {};
      metrics.innerHTML = metric("Статус", item.status)
        + metric("Время", `${Number(values.seconds || 0).toFixed(3)} с`)
        + metric("Стоимость", `${Number(values.cost_rub || 0).toFixed(4)} ₽`)
        + metric("Объём", `${values.input_count ?? "—"} → ${values.output_count ?? "—"}`)
        + metric("Токены", Object.values(values.usage_by_model || {}).reduce((sum, usage) => sum + (usage.prompt_tokens || 0) + (usage.completion_tokens || 0), 0));
      if (item.error) metrics.insertAdjacentHTML("beforeend", `<span class="cascade-lab__metric-error">Ошибка: ${item.error}</span>`);
    } else {
      const input = plannedInput(selectedStep);
      inputTerminal.textContent = input === undefined ? "До этого шага ещё нет входных данных." : pretty(input);
      outputTerminal.textContent = "Шаг ещё не выполнялся.";
      renderReadable(inputReadable, input, selectedStep, "input");
      outputReadable.replaceChildren();
      appendSummary(outputReadable, "Шаг ещё не выполнялся.");
      metrics.replaceChildren();
    }
    const last = availableStep();
    previousButton.hidden = !activeRun || selectedStep <= 1;
    followingButton.hidden = !activeRun || selectedStep >= last;
    runNextButton.hidden = !activeRun || activeRun.current_step >= 8;
    runNextButton.textContent = `Выполнить шаг ${Math.min(8, (activeRun?.current_step || 0) + 1)}`;
    rerunButton.hidden = !activeRun || selectedStep > activeRun.current_step;
  }

  function render(run) {
    activeRun = run;
    document.getElementById("lab-total-time").textContent = `${Number(run.total_seconds || 0).toFixed(2)} с`;
    document.getElementById("lab-total-cost").textContent = `${Number(run.total_cost_rub || 0).toFixed(2)} ₽`;
    nodes.forEach(node => {
      const item = snapshot(Number(node.dataset.step));
      node.classList.remove("is-running", "is-completed", "is-skipped", "is-error");
      if (!item) return node.querySelector(".cascade-node__metrics").textContent = "Не запускался";
      node.classList.add(item.status === "skipped" ? "is-skipped" : item.status === "fallback" ? "is-error" : "is-completed");
      const values = item.metrics || {};
      node.querySelector(".cascade-node__metrics").textContent = `${values.output_count ?? 0} · ${Number(values.seconds || 0).toFixed(2)} с · ${Number(values.cost_rub || 0).toFixed(2)} ₽`;
    });
    message.textContent = run.result?.pause_reason || (run.current_step === 8 ? "Прогон завершён" : `Остановлено после шага ${run.current_step}`);
    renderChecks(run);
    selectStep(selectedStep || Math.max(1, run.current_step));
  }

  function settingsFromForm(includeFixtures = false) {
    const value = name => form.elements.namedItem(name)?.value;
    const integer = (name, fallback) => Number.parseInt(value(name) || fallback, 10);
    let customCards = [];
    try { customCards = JSON.parse(value("cards_json") || "[]"); } catch (_) {}
    const settings = {
      steps: {
        "1": {model: value("step_1_model"), cache: value("step_1_cache"), max_active_requirements: integer("step_1_max_requirements", 0)},
        "2": {model: value("step_2_model"), cache: value("step_2_cache"), min_phrases: integer("step_2_min_phrases", 12), max_phrases: integer("step_2_max_phrases", 24)},
        "3": {sources: value("step_3_sources")},
        "4": {model: value("step_4_model"), intensity: value("step_4_intensity"), cache: value("step_4_cache")},
        "5": {color_filter: value("step_5_color_filter"), stock_policy: value("step_5_stock_policy"), tolerance_percent: integer("step_5_tolerance_percent", 5)},
        "6": {model: value("step_6_model"), cache: value("step_6_cache"), numeric_prefill: value("step_6_numeric_prefill"), first_batch: integer("step_6_first_batch", 25), ceiling: integer("step_6_ceiling", 75)},
        "7": {matrix_order: value("step_7_matrix_order"), price_order: value("step_7_price_order")},
        "8": {live_prices: value("step_8_live_prices")},
      },
      top: integer("step_8_top", 10),
      max_cost_rub: Number(value("max_cost_rub") || 10),
      max_seconds: Number(value("max_seconds") || 10),
    };
    if (includeFixtures) settings.custom_cards = Array.isArray(customCards) ? customCards : [];
    return settings;
  }

  function applySettings(settings) {
    const map = {
      step_1_model: ["1", "model"], step_1_cache: ["1", "cache"], step_1_max_requirements: ["1", "max_active_requirements"],
      step_2_model: ["2", "model"], step_2_cache: ["2", "cache"], step_2_min_phrases: ["2", "min_phrases"], step_2_max_phrases: ["2", "max_phrases"],
      step_3_sources: ["3", "sources"], step_4_model: ["4", "model"], step_4_intensity: ["4", "intensity"], step_4_cache: ["4", "cache"],
      step_5_color_filter: ["5", "color_filter"], step_5_stock_policy: ["5", "stock_policy"], step_5_tolerance_percent: ["5", "tolerance_percent"],
      step_6_model: ["6", "model"], step_6_cache: ["6", "cache"], step_6_numeric_prefill: ["6", "numeric_prefill"], step_6_first_batch: ["6", "first_batch"], step_6_ceiling: ["6", "ceiling"],
      step_7_matrix_order: ["7", "matrix_order"], step_7_price_order: ["7", "price_order"], step_8_live_prices: ["8", "live_prices"],
    };
    Object.entries(map).forEach(([field, [step, key]]) => {
      const control = form.elements.namedItem(field);
      if (control && settings?.steps?.[step]?.[key] !== undefined) {
        control.value = settings.steps[step][key];
        control.dispatchEvent(new Event("change", {bubbles: true}));
      }
    });
    if (settings.top !== undefined) form.elements.namedItem("step_8_top").value = settings.top;
    if (settings.max_cost_rub !== undefined) form.elements.namedItem("max_cost_rub").value = settings.max_cost_rub;
    if (settings.max_seconds !== undefined) form.elements.namedItem("max_seconds").value = settings.max_seconds;
  }

  async function execute(fromStep, stopAfter) {
    const settings = settingsFromForm(true);
    document.getElementById("lab-run").disabled = true;
    runNextButton.disabled = true;
    try {
      for (let step = fromStep; step <= stopAfter; step += 1) {
        const history = (activeRun?.snapshots || []).filter(item => item.step < step);
        const priorSeconds = history.reduce((sum, item) => sum + Number(item.metrics?.seconds || 0), 0);
        const priorCost = history.reduce((sum, item) => sum + Number(item.metrics?.cost_rub || 0), 0);
        const resume = history.length ? [{...history[history.length - 1], input: null}] : [];
        const body = new FormData(form);
        body.set("from_step", String(step));
        body.set("stop_after", String(step));
        body.set("settings", JSON.stringify(settings));
        body.set("snapshots", JSON.stringify(resume));
        body.set("cascade_state", "{}");
        body.set("prior_total_seconds", String(priorSeconds));
        body.set("prior_total_cost_rub", String(priorCost));
        selectedStep = step;
        nodes[step - 1].classList.add("is-running");
        nodes[step - 1].querySelector(".cascade-node__metrics").textContent = "Выполняется…";
        message.textContent = `Выполняю шаг ${step} из ${stopAfter}…`;
        const response = await fetch(root.dataset.executeUrl, {method: "POST", body});
        let data;
        try {
          data = await response.json();
        } catch (_) {
          throw new Error(`Сервер не смог обработать данные шага ${step} (HTTP ${response.status}).`);
        }
        if (!response.ok) throw new Error(data.error || "Не удалось выполнить каскад.");
        data.snapshots = [...history, ...data.snapshots.filter(item => item.step >= step)];
        selectedStep = data.current_step || step;
        render(data);
        if (data.current_step < step || data.result?.pause_reason) break;
      }
    } catch (error) {
      nodes.forEach(node => node.classList.remove("is-running"));
      const failedNode = nodes[(selectedStep || fromStep) - 1];
      failedNode?.classList.add("is-error");
      if (failedNode) failedNode.querySelector(".cascade-node__metrics").textContent = "Ошибка";
      message.textContent = error.message;
    } finally {
      document.getElementById("lab-run").disabled = false;
      runNextButton.disabled = false;
    }
  }

  function syncProductFields() {
    document.getElementById("lab-product-fields").hidden = Boolean(document.getElementById("lab-line").value);
  }
  function bindRequirementRow(row) {
    row.querySelector("[data-remove-requirement]").onclick = () => {
      const list = document.getElementById("lab-requirement-list");
      if (list.children.length === 1) row.querySelectorAll("input").forEach(input => input.value = "");
      else row.remove();
    };
  }

  nodes.forEach(node => node.onclick = () => selectStep(node.dataset.step));
  document.querySelectorAll(".cascade-lab__requirement-row").forEach(bindRequirementRow);
  document.getElementById("lab-add-requirement").onclick = () => {
    const row = document.querySelector(".cascade-lab__requirement-row").cloneNode(true);
    row.querySelectorAll("input").forEach(input => input.value = "");
    bindRequirementRow(row);
    document.getElementById("lab-requirement-list").append(row);
  };
  document.getElementById("lab-line").onchange = syncProductFields;
  document.getElementById("lab-preset").onchange = event => {
    const option = event.target.selectedOptions[0];
    if (!option?.dataset.settings) return;
    try { applySettings(JSON.parse(option.dataset.settings)); } catch (_) {}
    document.getElementById("lab-preset-name").value = option.textContent.trim();
  };
  previousButton.onclick = () => selectStep(selectedStep - 1);
  followingButton.onclick = () => selectStep(selectedStep + 1);
  document.querySelectorAll("[data-io-side]").forEach(section => {
    const side = section.dataset.ioSide;
    section.querySelectorAll("[data-io-view]").forEach(button => button.onclick = () => setIoView(side, button.dataset.ioView));
    setIoView(side, localStorage.getItem(`cascade-lab-${side}-view`) || "readable");
  });
  form.onsubmit = event => { event.preventDefault(); activeRun = null; selectedStep = 1; execute(1, Number(document.getElementById("lab-stop-after").value)); };
  runNextButton.onclick = () => {
    const target = Math.min(8, activeRun.current_step + 1);
    execute(target, target);
  };
  rerunButton.onclick = () => execute(selectedStep, Math.max(selectedStep, Number(document.getElementById("lab-stop-after").value)));
  document.getElementById("lab-save-preset").onclick = async () => {
    const name = document.getElementById("lab-preset-name").value.trim();
    const body = new URLSearchParams({csrfmiddlewaretoken: csrf(), name, settings: JSON.stringify(settingsFromForm())});
    const response = await fetch(root.dataset.presetUrl, {method: "POST", headers: {"Content-Type": "application/x-www-form-urlencoded"}, body});
    const data = await response.json();
    message.textContent = response.ok ? `Набор «${data.name}» сохранён.` : data.error;
    if (response.ok) {
      const select = document.getElementById("lab-preset");
      let option = [...select.options].find(item => item.value === String(data.id));
      if (!option) {
        option = document.createElement("option");
        option.value = data.id;
        select.append(option);
      }
      option.textContent = data.name;
      option.dataset.settings = JSON.stringify(data.settings);
      select.value = String(data.id);
    }
  };
  document.getElementById("lab-activate").onclick = async () => {
    const name = document.getElementById("lab-preset-name").value.trim() || "Настройки из лаборатории";
    const body = new URLSearchParams({csrfmiddlewaretoken: csrf(), name, settings: JSON.stringify(settingsFromForm())});
    const response = await fetch(root.dataset.activateUrl, {method: "POST", headers: {"Content-Type": "application/x-www-form-urlencoded"}, body});
    const data = await response.json();
    message.textContent = response.ok ? "Настройки применены к подбору товаров." : data.error;
    if (response.ok) document.getElementById("lab-active-config").textContent = data.name;
  };
  syncProductFields();
  selectStep(1);
})();
