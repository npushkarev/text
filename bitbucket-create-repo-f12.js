// Bitbucket Server: F12 > Console. Проект СУРА2 (SU2). Заполните REPO.
// APPLY = false: проверка и план. APPLY = true: создание после подтверждения.
(async () => {
  const APPLY = false;
  const PROJECT = "SU2";
  const REPO = "";
  const BRANCHES = ["master", "develop"];
  const DEFAULT_BRANCH = "develop";

  const context = (window.AJS && window.AJS.contextPath && window.AJS.contextPath()) || "";
  const api = `${context}/rest/api/1.0`;
  const esc = encodeURIComponent;
  const match = location.pathname.slice(context.length).match(/^\/projects\/([^/]+)(?:\/|$)/);
  const project = PROJECT || (match && decodeURIComponent(match[1]));

  if (!project) throw new Error("Откройте страницу проекта Bitbucket или заполните PROJECT.");
  if (!/^[a-z0-9][a-z0-9._-]*$/.test(REPO))
    throw new Error("Заполните REPO: латинские строчные буквы, цифры, точка, дефис, подчёркивание.");
  if (BRANCHES.length !== 2 || new Set(BRANCHES).size !== 2)
    throw new Error("BRANCHES должен содержать ровно два разных имени.");
  for (const name of BRANCHES) {
    if (typeof name !== "string" || !name || name === "@" || name.startsWith("-") ||
        /[\x00-\x20\x7f~^:?*\[\\]/.test(name) || name.includes("..") ||
        name.includes("@{") || name.endsWith(".") ||
        name.split("/").some(part => !part || part.startsWith(".") || part.endsWith(".lock")))
      throw new Error(`Недопустимое имя ветки: ${name}`);
  }
  if (BRANCHES[0].startsWith(BRANCHES[1] + "/") || BRANCHES[1].startsWith(BRANCHES[0] + "/"))
    throw new Error("Имена веток конфликтуют: нельзя одновременно иметь foo и foo/bar.");
  if (!BRANCHES.includes(DEFAULT_BRANCH))
    throw new Error("DEFAULT_BRANCH должен совпадать с одним из имён в BRANCHES.");

  const request = async (path, init = {}, allowMissing = false) => {
    const response = await fetch(api + path, {
      credentials: "same-origin",
      ...init,
      headers: {
        Accept: "application/json",
        "X-Atlassian-Token": "no-check",
        ...(init.headers || {}),
      },
    });
    if (allowMissing && response.status === 404) return null;
    const text = await response.text();
    if (!response.ok) {
      let detail = `HTTP ${response.status}`;
      try {
        const body = JSON.parse(text);
        if (body.errors) detail += ": " + body.errors.map(e => e.message).join("; ");
      } catch (_) { /* HTML страницы входа не выводим в консоль. */ }
      throw new Error(`${init.method || "GET"} ${path}: ${detail}`);
    }
    try { return text ? JSON.parse(text) : null; }
    catch (_) { throw new Error(`${path}: ожидался JSON. Проверьте вход в Bitbucket.`); }
  };
  const json = (method, body) => ({
    method,
    headers: { "Content-Type": "application/json" },
    body: JSON.stringify(body),
  });
  const projectPath = `/projects/${esc(project)}`;
  const repoPath = `${projectPath}/repos/${esc(REPO)}`;
  const info = await request(projectPath);
  if (!info || info.key !== project) throw new Error("Ответ Bitbucket не соответствует ключу PROJECT.");
  if (await request(repoPath, {}, true))
    throw new Error(`${project}/${REPO} уже существует. Скрипт не меняет существующие репозитории.`);

  console.table([
    { action: "Проект", value: `${info.key} (${info.name})` },
    { action: "Создать репозиторий", value: REPO },
    { action: "Первый коммит", value: `README.md в ${BRANCHES[0]}` },
    { action: "Создать вторую ветку", value: `${BRANCHES[1]} от того же коммита` },
    { action: "Ветка по умолчанию", value: DEFAULT_BRANCH },
  ]);
  if (!APPLY) return console.log("DRY RUN: ничего не изменено. Для создания поставьте APPLY = true.");
  if (!confirm(`Создать ${project}/${REPO}, README.md и ветки ${BRANCHES.join(", ")}?`))
    return console.log("Отменено. Ничего не изменено.");

  // Повторная проверка после подтверждения. POST также отклоняет дубликат.
  if (await request(repoPath, {}, true)) throw new Error("Репозиторий уже появился. Создание остановлено.");
  const created = await request(`${projectPath}/repos`, json("POST", {
    name: REPO, scmId: "git", forkable: true,
  }));
  let stage = "проверка созданного репозитория";
  const url = `${location.origin}${context}/projects/${esc(project)}/repos/${esc(REPO)}/browse`;
  console.log(`CREATED: ${project}/${REPO}`);
  try {
    if (!created || created.slug !== REPO || !created.project || created.project.key !== project)
      throw new Error("Сервер вернул другое имя или проект. Инициализация остановлена.");
    stage = "первый коммит README.md";
    const content = `# ${REPO}\n`;
    const form = new FormData();
    form.append("content", content);
    form.append("message", "Initial commit");
    form.append("branch", BRANCHES[0]);
    const commit = await request(`${repoPath}/browse/README.md`, { method: "PUT", body: form });
    if (!commit || !/^[0-9a-f]{40,64}$/i.test(commit.id || ""))
      throw new Error("Сервер не вернул ID первого коммита.");
    console.log(`COMMIT: ${commit.id} (${BRANCHES[0]})`);

    stage = "создание второй ветки";
    await request(`${repoPath}/branches`, json("POST", { name: BRANCHES[1], startPoint: commit.id }));
    stage = "выбор ветки по умолчанию";
    await request(`${repoPath}/branches/default`, json("PUT", { id: `refs/heads/${DEFAULT_BRANCH}` }));

    stage = "проверка результата";
    const branches = [];
    for (let start = 0; ;) {
      const page = await request(`${repoPath}/branches?limit=100&start=${start}`);
      if (!page || !Array.isArray(page.values)) throw new Error("Некорректный список веток.");
      branches.push(...page.values);
      if (page.isLastPage === true) break;
      if (!Number.isInteger(page.nextPageStart) || page.nextPageStart <= start)
        throw new Error("Некорректная пагинация списка веток.");
      start = page.nextPageStart;
    }
    const defaultBranch = await request(`${repoPath}/branches/default`);
    if (branches.length !== 2 || !BRANCHES.every(name => branches.some(b =>
      b.id === `refs/heads/${name}` && b.latestCommit === commit.id)) ||
      !defaultBranch || defaultBranch.id !== `refs/heads/${DEFAULT_BRANCH}`)
      throw new Error("Результат не соответствует плану. Проверьте ветки вручную.");
    console.table(branches.map(b => ({ branch: b.displayId, commit: b.latestCommit })));
    console.log(`ГОТОВО: ${url}\nВетка по умолчанию: ${DEFAULT_BRANCH}`);
  } catch (error) {
    console.error(`Репозиторий создан, но этап «${stage}» не завершён. Он не удалён: ${url}`);
    throw error;
  }
})().catch(error => console.error("ОШИБКА:", error.message));
