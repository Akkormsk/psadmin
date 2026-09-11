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
  let pollTimer = null;

  const csrf = () => form.querySelector("[name=csrfmiddlewaretoken]").value;
  const url = (name, id) => root.dataset[name].replace("/0/", `/${id}/`);
  const pretty = value => JSON.stringify(value ?? null, null, 2);
  const metric = (label, value) => `<span>${label}: <strong>${value}</strong></span>`;
  const snapshot = step => activeRun?.snapshots?.find(item => item.step === Number(step));
  const availableStep = () => Math.min(8, (activeRun?.current_step || 0) + (activeRun?.status === "running" ? 1 : 0));

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
        td.textContent = cellText(value);
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
    ], rows);
  }

  function renderProducts(target, value) {
    const rows = value.preview || value;
    const total = value.count ?? rows.length;
    appendSummary(target, `Найдено: ${total}. Показано: ${rows.length}.`);
    appendTable(target, [
      {label: "Товар", value: row => row.name || row.title || row.id},
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
      renderProducts(target, value);
    } else if (Array.isArray(value) && value.every(item => typeof item === "string")) {
      renderPhrases(target, value, step, side);
    } else if (Array.isArray(value) && value.some(item => item && typeof item === "object" && "checked" in item)) {
      renderCriteria(target, value);
    } else if (Array.isArray(value) && value.some(item => item && typeof item === "object")) {
      renderProducts(target, value);
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
    const isRunning = activeRun?.status === "running" && selectedStep === activeRun.current_step + 1;
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
        + metric("Токены", Object.values(values.usage_by_model || {}).reduce(
          (total, usage) => total + (usage.prompt_tokens || 0) + (usage.completion_tokens || 0), 0,
        ));
      if (item.error) {
        const error = document.createElement("span");
        error.className = "cascade-lab__metric-error";
        error.textContent = `Ошибка агента: ${item.error}`;
        metrics.append(error);
      }
    } else {
      const input = plannedInput(selectedStep);
      inputTerminal.textContent = input === undefined ? "До этого шага ещё нет входных данных." : pretty(input);
      outputTerminal.textContent = isRunning ? "Выполняется…" : "Шаг ещё не выполнялся.";
      renderReadable(inputReadable, input, selectedStep, "input");
      outputReadable.replaceChildren();
      appendSummary(outputReadable, isRunning ? "Выполняется…" : "Шаг ещё не выполнялся.");
      metrics.innerHTML = isRunning ? metric("Статус", "Выполняется") : "";
    }

    const lastAvailable = availableStep();
    previousButton.hidden = !activeRun || selectedStep <= 1;
    followingButton.hidden = !activeRun || selectedStep >= lastAvailable;
    runNextButton.hidden = !activeRun || activeRun.status === "running" || activeRun.current_step >= 8;
    runNextButton.textContent = `Выполнить шаг ${Math.min(8, (activeRun?.current_step || 0) + 1)}`;
    rerunButton.hidden = !activeRun || activeRun.status === "running" || selectedStep > activeRun.current_step;
  }

  function render(run) {
    activeRun = run;
    document.getElementById("lab-total-time").textContent = `${Number(run.total_seconds || 0).toFixed(2)} с`;
    document.getElementById("lab-total-cost").textContent = `${Number(run.total_cost_rub || 0).toFixed(2)} ₽`;
    nodes.forEach(node => {
      const step = Number(node.dataset.step);
      const item = snapshot(step);
      node.classList.remove("is-running", "is-completed", "is-skipped", "is-error");
      if (item) {
        node.classList.add(item.status === "skipped" ? "is-skipped" : item.status === "fallback" ? "is-error" : "is-completed");
        const values = item.metrics || {};
        node.querySelector(".cascade-node__metrics").textContent = `${values.output_count ?? 0} · ${Number(values.seconds || 0).toFixed(2)} с · ${Number(values.cost_rub || 0).toFixed(2)} ₽`;
      } else if (run.status === "running" && step === run.current_step + 1) {
        node.classList.add("is-running");
        node.querySelector(".cascade-node__metrics").textContent = "Выполняется…";
      } else {
        node.querySelector(".cascade-node__metrics").textContent = "Не запускался";
      }
    });
    message.textContent = run.status === "running" ? "Прогон выполняется…"
      : run.status === "error" ? run.error
        : run.status === "paused" ? (run.result?.pause_reason || `Остановлено после шага ${run.current_step}`)
          : run.result?.checks?.length ? (run.result.passed ? "Все проверки пройдены" : "Есть непройденные проверки")
            : "Прогон завершён";
    renderChecks(run);
    selectStep(selectedStep || Math.max(1, run.current_step));
    if (run.status === "running") schedulePoll();
  }

  async function loadRun(id) {
    clearTimeout(pollTimer);
    const response = await fetch(url("detailUrl", id));
    if (!response.ok) throw new Error("Не удалось загрузить прогон.");
    render(await response.json());
  }

  function schedulePoll() {
    clearTimeout(pollTimer);
    pollTimer = setTimeout(() => loadRun(activeRun.id).catch(error => message.textContent = error.message), 1000);
  }

  function syncProductFields() {
    document.getElementById("lab-product-fields").hidden = Boolean(document.getElementById("lab-line").value)
      || Boolean(document.getElementById("lab-case").value);
  }

  function bindRequirementRow(row) {
    row.querySelector("[data-remove-requirement]").onclick = () => {
      const list = document.getElementById("lab-requirement-list");
      if (list.children.length === 1) {
        row.querySelectorAll("input").forEach(input => input.value = "");
      } else {
        row.remove();
      }
    };
  }

  nodes.forEach(node => node.onclick = () => selectStep(node.dataset.step));
  document.querySelectorAll("[data-run-id]").forEach(button => button.onclick = () => {
    selectedStep = null;
    loadRun(button.dataset.runId);
  });
  document.querySelectorAll(".cascade-lab__requirement-row").forEach(bindRequirementRow);
  document.getElementById("lab-add-requirement").onclick = () => {
    const row = document.querySelector(".cascade-lab__requirement-row").cloneNode(true);
    row.querySelectorAll("input").forEach(input => input.value = "");
    bindRequirementRow(row);
    document.getElementById("lab-requirement-list").append(row);
    row.querySelector("input").focus();
  };
  document.getElementById("lab-line").onchange = syncProductFields;
  document.getElementById("lab-case").onchange = event => {
    document.getElementById("lab-line").disabled = Boolean(event.target.value);
    syncProductFields();
  };
  previousButton.onclick = () => selectStep(selectedStep - 1);
  followingButton.onclick = () => selectStep(selectedStep + 1);
  document.querySelectorAll("[data-io-side]").forEach(section => {
    const side = section.dataset.ioSide;
    section.querySelectorAll("[data-io-view]").forEach(button => {
      button.onclick = () => setIoView(side, button.dataset.ioView);
    });
    setIoView(side, localStorage.getItem(`cascade-lab-${side}-view`) || "readable");
  });

  form.onsubmit = async event => {
    event.preventDefault();
    message.textContent = "Запускаю…";
    const response = await fetch(root.dataset.createUrl, {method: "POST", body: new FormData(form)});
    const data = await response.json();
    if (!response.ok) {
      message.textContent = data.error || "Не удалось запустить.";
      return;
    }
    selectedStep = 1;
    loadRun(data.run_id);
  };

  runNextButton.onclick = async () => {
    const target = Math.min(8, activeRun.current_step + 1);
    selectedStep = target;
    selectStep(target);
    outputTerminal.textContent = "Запускаю…";
    runNextButton.disabled = true;
    const body = new URLSearchParams({csrfmiddlewaretoken: csrf(), stop_after: String(target)});
    try {
      const response = await fetch(url("executeUrl", activeRun.id), {
        method: "POST",
        headers: {"Content-Type": "application/x-www-form-urlencoded"},
        body,
      });
      const data = await response.json();
      if (!response.ok) throw new Error(data.error || "Не удалось выполнить шаг.");
      activeRun.status = "running";
      render(activeRun);
      loadRun(data.run_id);
    } catch (error) {
      message.textContent = error.message;
      outputTerminal.textContent = error.message;
    } finally {
      runNextButton.disabled = false;
    }
  };

  rerunButton.onclick = async () => {
    const body = new URLSearchParams({
      csrfmiddlewaretoken: csrf(),
      from_step: String(selectedStep),
      stop_after: String(Math.max(selectedStep, activeRun.stop_after)),
    });
    const response = await fetch(url("forkUrl", activeRun.id), {
      method: "POST",
      headers: {"Content-Type": "application/x-www-form-urlencoded"},
      body,
    });
    const data = await response.json();
    if (response.ok) {
      selectedStep = Number(selectedStep);
      loadRun(data.run_id);
    } else {
      message.textContent = data.error;
    }
  };

  syncProductFields();
})();
