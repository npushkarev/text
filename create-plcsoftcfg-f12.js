/*
 * F12 script: create repository plcsoftcfg next to plcbuildsys (project SURA2)
 * with the same folder hierarchy. Git keeps no empty folders, so every leaf
 * folder gets an empty .gitkeep.
 *
 * Run in a logged-in Bitbucket tab. It is a dry run by default.
 * Change APPLY to true only after checking the console table.
 */
(async () => {
  const APPLY = false;
  const SRC_REPO = "plcbuildsys";
  const NEW_REPO = "plcsoftcfg";
  const PROJECT = "";  // empty: take the project of SRC_REPO

  const base = (window.AJS && AJS.contextPath && AJS.contextPath()) || "";
  const api = `${base}/rest/api/1.0`;
  const esc = encodeURIComponent;
  const escPath = p => p.split("/").map(esc).join("/");

  const request = async (url, init = {}) => {
    const r = await fetch(url, {
      credentials: "same-origin",
      ...init,
      headers: { Accept: "application/json", "X-Atlassian-Token": "no-check", ...(init.headers || {}) },
    });
    const body = await r.text();
    if (!r.ok) throw new Error(`${init.method || "GET"} ${url}: HTTP ${r.status}\n${body}`);
    return body ? JSON.parse(body) : null;
  };

  const found = await request(`${api}/repos?${new URLSearchParams({ name: SRC_REPO, limit: "100" })}`);
  const sources = found.values.filter(r => r.slug === SRC_REPO && (!PROJECT || r.project.key === PROJECT));
  if (sources.length !== 1)
    throw new Error(`${SRC_REPO} найден ${sources.length} раз: ${sources.map(r => r.project.key).join(", ")}. Задайте PROJECT.`);
  const project = sources[0].project;
  const repo = name => `${api}/projects/${esc(project.key)}/repos/${esc(name)}`;

  const branch = (await request(`${repo(SRC_REPO)}/branches/default`)).displayId;

  const files = [];
  for (let start = 0; ;) {
    const page = await request(`${repo(SRC_REPO)}/files?${new URLSearchParams({ at: branch, limit: "1000", start })}`);
    files.push(...page.values);
    if (page.isLastPage) break;
    start = page.nextPageStart;
  }

  const dirs = new Set();
  for (const f of files) {
    const parts = f.split("/").slice(0, -1);
    for (let i = 1; i <= parts.length; i++) dirs.add(parts.slice(0, i).join("/"));
  }
  const leaves = [...dirs].filter(d => ![...dirs].some(o => o.startsWith(d + "/"))).sort();

  const exists = (await fetch(repo(NEW_REPO), { credentials: "same-origin" })).status;
  console.log(`Проект: ${project.key} (${project.name}), источник: ${SRC_REPO}@${branch}, файлов: ${files.length}, папок: ${dirs.size}`);
  console.table([...dirs].sort().map(d => ({ folder: d, gitkeep: leaves.includes(d) })));

  if (exists !== 404) throw new Error(`${project.key}/${NEW_REPO}: HTTP ${exists}, репозиторий уже есть или нет доступа. Ничего не изменено.`);
  if (!leaves.length) throw new Error(`В ${SRC_REPO} нет папок. Ничего не изменено.`);
  if (!APPLY) return console.log("DRY RUN: Bitbucket не изменён. Для применения поставьте APPLY = true.");
  if (!confirm(`Создать ${project.key}/${NEW_REPO} и ${leaves.length} коммитов с .gitkeep в ветке ${branch}?`)) return;

  await request(`${api}/projects/${esc(project.key)}/repos`, {
    method: "POST",
    headers: { "Content-Type": "application/json" },
    body: JSON.stringify({ name: NEW_REPO, scmId: "git", forkable: true }),
  });
  console.log(`CREATED: ${project.key}/${NEW_REPO}`);

  try {
    for (const dir of leaves) {
      const form = new FormData();
      form.append("content", "");
      form.append("message", `Структура папок как в ${SRC_REPO}: ${dir}`);
      form.append("branch", branch);
      await request(`${repo(NEW_REPO)}/browse/${escPath(dir + "/.gitkeep")}`, { method: "PUT", body: form });
      console.log(`+ ${dir}/.gitkeep`);
    }
    await request(`${repo(NEW_REPO)}/branches/default`, {
      method: "PUT",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ id: `refs/heads/${branch}` }),
    });
  } catch (error) {
    const url = sources[0].links.clone.find(l => l.name === "http").href.replace(SRC_REPO, NEW_REPO);
    console.error("Не удалось заполнить через REST. Репозиторий создан; залейте папки через git:\n" + [
      `git clone ${url} && cd ${NEW_REPO}`,
      ...leaves.map(d => `mkdir -p '${d}' && touch '${d}/.gitkeep'`),
      `git checkout -B ${branch} && git add . && git commit -m 'Структура папок как в ${SRC_REPO}'`,
      `git push -u origin ${branch}`,
    ].join("\n"));
    throw error;
  }
  console.log(`ГОТОВО: ${location.origin}${base}/projects/${project.key}/repos/${NEW_REPO}/browse`);
})().catch(error => console.error("ОШИБКА:", error));
