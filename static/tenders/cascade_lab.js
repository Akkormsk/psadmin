(() => {
  const root = document.getElementById("cascade-lab");
  if (!root) return;

  const form = document.getElementById("lab-run-form");
  const message = document.getElementById("lab-message");
  const nodes = [...document.querySelectorAll(".cascade-node")];
  const inputTerminal = document.getElementById("lab-step-input");
  const outputTerminal = document.getElementById("lab-step-output");
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
    if (step === 1) return activeRun?.input_payload;
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
      const values = item.metrics || {};
      metrics.innerHTML = metric("Статус", item.status)
        + metric("Время", `${Number(values.seconds || 0).toFixed(3)} с`)
        + metric("Стоимость", `${Number(values.cost_rub || 0).toFixed(4)} ₽`)
        + metric("Объём", `${values.input_count ?? "—"} → ${values.output_count ?? "—"}`)
        + metric("Токены", Object.values(values.usage_by_model || {}).reduce(
          (total, usage) => total + (usage.prompt_tokens || 0) + (usage.completion_tokens || 0), 0,
        ));
    } else {
      const input = plannedInput(selectedStep);
      inputTerminal.textContent = input === undefined ? "До этого шага ещё нет входных данных." : pretty(input);
      outputTerminal.textContent = isRunning ? "Выполняется…" : "Шаг ещё не выполнялся.";
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
        node.classList.add(item.status === "skipped" ? "is-skipped" : "is-completed");
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
