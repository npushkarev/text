// Вернуть конфигурации с копий VCS root ("Bitbucket (1)", "Bitbucket (2)") на исходный "Bitbucket".
// Запуск: DevTools → Console на любой странице TeamCity (2018.1), под админом.
// 1) APPLY = false: печатает diff настроек копий против оригинала и список, кого переключит.
// 2) APPLY = true: переключает entries (checkout rules сохраняются). Сначала шаблоны, потом конфигурации.
// 3) DELETE_COPIES = true: удаляет копии, у которых не осталось использований.
(async () => {
  const APPLY = false;
  const DELETE_COPIES = false;
  const ORIGINAL = 'Bitbucket';
  const COPIES = ['Bitbucket (1)', 'Bitbucket (2)'];
  const IGNORE_PROPS = [];

  const base = (window.base_uri || location.origin) + '/app/rest';
  const hdr = { Accept: 'application/json', 'Content-Type': 'application/json' };
  const req = async (method, path, body) => {
    const r = await fetch(base + path, { method, headers: hdr, credentials: 'same-origin',
      body: body === undefined ? undefined : JSON.stringify(body) });
    const t = await r.text();
    if (!r.ok) throw new Error(`${method} ${path} → ${r.status}: ${t.slice(0, 300)}`);
    return t && t[0] === '{' ? JSON.parse(t) : t;
  };

  const all = (await req('GET', '/vcs-roots?locator=count:1000&fields=vcsRoot(id,name,project(id))')).vcsRoot || [];
  const byName = n => all.filter(v => v.name === n);
  const one = n => {
    const f = byName(n);
    if (f.length !== 1) throw new Error(`"${n}": найдено ${f.length} корней, ожидался ровно один`);
    return f[0];
  };
  const orig = one(ORIGINAL);
  const props = async id => Object.fromEntries(
    ((await req('GET', `/vcs-roots/id:${id}?fields=properties(property(name,value))`)).properties.property || [])
      .map(p => [p.name, p.value]));
  const origProps = await props(orig.id);

  const plan = [];
  const diffs = [];
  for (const name of COPIES) {
    const c = one(name);
    const cp = await props(c.id);
    for (const k of new Set([...Object.keys(origProps), ...Object.keys(cp)])) {
      if (IGNORE_PROPS.includes(k) || origProps[k] === cp[k]) continue;
      diffs.push({ copy: name, property: k, original: origProps[k], copyValue: cp[k] });
    }
    const users = (await req('GET',
      `/buildTypes?locator=vcsRoot:(id:${c.id}),templateFlag:any&fields=buildType(id,name,projectName,templateFlag)`)
    ).buildType || [];
    for (const bt of users) plan.push({ copy: name, copyId: c.id, bt: bt.id, name: `${bt.projectName} / ${bt.name}`,
      template: !!bt.templateFlag });
  }
  plan.sort((a, b) => b.template - a.template);

  console.log(`Оригинал: ${orig.name} (${orig.id})`);
  if (diffs.length) { console.warn('Копии отличаются от оригинала:'); console.table(diffs); }
  else console.log('Настройки копий совпадают с оригиналом.');
  console.table(plan);

  if (!APPLY) { console.log('DRY RUN: TeamCity не изменён.'); return; }
  if (diffs.length && !confirmOk()) return;

  const done = [];
  for (const p of plan) {
    try {
      const entries = (await req('GET', `/buildTypes/id:${p.bt}/vcs-root-entries`))['vcs-root-entry'] || [];
      const own = entries.find(e => e['vcs-root'].id === p.copyId);
      if (!own) { done.push({ ...p, result: 'SKIP: entry унаследован от шаблона' }); continue; }
      const rules = own['checkout-rules'] || '';
      if (!entries.some(e => e['vcs-root'].id === orig.id))
        await req('POST', `/buildTypes/id:${p.bt}/vcs-root-entries`,
          { id: orig.id, 'vcs-root': { id: orig.id }, 'checkout-rules': rules });
      await req('DELETE', `/buildTypes/id:${p.bt}/vcs-root-entries/${p.copyId}`);
      done.push({ ...p, result: 'OK' });
    } catch (e) { done.push({ ...p, result: 'FAIL: ' + e.message }); }
  }
  console.table(done);

  if (!DELETE_COPIES) return;
  for (const name of COPIES) {
    const c = one(name);
    const left = (await req('GET', `/buildTypes?locator=vcsRoot:(id:${c.id}),templateFlag:any&fields=count`)).count;
    if (left) { console.warn(`${name}: осталось ${left} использований, не удаляю`); continue; }
    await req('DELETE', `/vcs-roots/id:${c.id}`);
    console.log(`${name}: удалён`);
  }

  function confirmOk() {
    console.error('Копии отличаются от оригинала (см. таблицу выше). Проверьте diff; если отличия не нужны,' +
      ' внесите ключи в IGNORE_PROPS и запустите снова.');
    return false;
  }
})().catch(e => console.error('ОШИБКА:', e.message));
