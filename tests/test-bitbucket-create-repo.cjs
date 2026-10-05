const test = require('node:test');
const assert = require('node:assert/strict');
const fs = require('node:fs');
const path = require('node:path');
const vm = require('node:vm');

const source = fs.readFileSync(path.join(__dirname, '..', 'bitbucket-create-repo-f12.js'), 'utf8');
const sha = 'a'.repeat(40);

async function run(options = {}) {
  const calls = [];
  const logs = [];
  let exists = Boolean(options.exists);
  let defaultBranch = '';
  let confirmed = false;
  const branches = [];
  const project = { key: 'SU2', name: 'СУРА2' };
  const code = source
    .replace('const APPLY = false;', `const APPLY = ${Boolean(options.apply)};`)
    .replace('const REPO = "";', 'const REPO = "new_repo";')
    .replace('const BRANCHES = ["master", "develop"];',
      `const BRANCHES = ${JSON.stringify(options.branches || ['master', 'develop'])};`);
  const context = options.context || '';
  const fetch = async (url, init) => {
    assert.ok(url.startsWith(context + '/rest/api/1.0/'));
    assert.equal(init.credentials, 'same-origin');
    assert.equal(init.headers.Accept, 'application/json');
    const route = url.slice((context + '/rest/api/1.0').length);
    const method = init.method || 'GET';
    calls.push({ method, route, body: init.body });
    const reply = (status, body) => ({
      status, ok: status >= 200 && status < 300,
      text: async () => body === undefined ? '' : JSON.stringify(body),
    });
    if (options.failAt === `${method} ${route}`)
      return reply(403, { errors: [{ message: 'Denied' }] });
    if (route === '/projects/SU2') return reply(200, project);
    if (route === '/projects/SU2/repos/new_repo' && method === 'GET') {
      if (options.race && confirmed) exists = true;
      return exists ? reply(200, { slug: 'new_repo', project }) : reply(404, {});
    }
    if (route === '/projects/SU2/repos' && method === 'POST') {
      assert.deepEqual(JSON.parse(init.body), { name: 'new_repo', scmId: 'git', forkable: true });
      exists = true;
      return reply(201, { slug: 'new_repo', project });
    }
    if (route.endsWith('/browse/README.md') && method === 'PUT') {
      assert.equal(init.body.get('content'), '# new_repo\n');
      assert.equal(init.body.get('branch'), 'master');
      assert.equal(init.body.get('message'), 'Initial commit');
      assert.equal(init.headers['Content-Type'], undefined);
      branches.push({ id: 'refs/heads/master', displayId: 'master', latestCommit: sha });
      return reply(200, { id: sha });
    }
    if (route.endsWith('/branches') && method === 'POST') {
      assert.deepEqual(JSON.parse(init.body), { name: 'develop', startPoint: sha });
      branches.push({ id: 'refs/heads/develop', displayId: 'develop', latestCommit: sha });
      return reply(200, branches[1]);
    }
    if (route.endsWith('/branches/default')) {
      if (method === 'PUT') {
        defaultBranch = JSON.parse(init.body).id;
        return reply(204);
      }
      return reply(200, { id: options.badDefault ? 'refs/heads/master' : defaultBranch });
    }
    if (route.includes('/branches?')) {
      const start = Number(new URL(url, 'https://bitbucket.invalid').searchParams.get('start'));
      if (options.paginate && start === 0)
        return reply(200, { values: branches.slice(0, 1), isLastPage: false, nextPageStart: 1 });
      return reply(200, { values: options.paginate ? branches.slice(1) : branches, isLastPage: true });
    }
    throw new Error(`Unexpected request: ${method} ${route}`);
  };
  await vm.runInNewContext(code, {
    window: { AJS: { contextPath: () => context } },
    location: { pathname: context + (options.pathname || '/projects/SU2/repos'), origin: 'https://bitbucket.invalid' },
    fetch, FormData,
    confirm: () => { confirmed = true; return options.confirm !== false; },
    console: Object.fromEntries(['log', 'error', 'table'].map(level =>
      [level, (...args) => logs.push({ level, args })])),
  });
  return { calls, logs, exists, branches, defaultBranch };
}

const writes = result => result.calls.filter(call => call.method !== 'GET');
const errors = result => result.logs.filter(log => log.level === 'error').map(log => log.args.join(' '));

test('dry run only reads, without creating a repository', async () => {
  const result = await run();
  assert.equal(writes(result).length, 0);
  assert.equal(result.exists, false);
  assert.equal(errors(result).length, 0);
  assert.ok(result.logs.some(log => String(log.args[0]).includes('DRY RUN')));
});

test('create, initial commit, two branches and default, including paginated verification', async () => {
  const result = await run({ apply: true, paginate: true });
  assert.deepEqual(writes(result).map(call => call.method), ['POST', 'PUT', 'POST', 'PUT']);
  assert.equal(result.branches.length, 2);
  assert.equal(result.defaultBranch, 'refs/heads/develop');
  assert.equal(errors(result).length, 0);
  assert.ok(result.logs.some(log => String(log.args[0]).includes('ГОТОВО')));
});

test('Bitbucket context path is preserved', async () => {
  const result = await run({ apply: true, context: '/bitbucket' });
  assert.equal(errors(result).length, 0);
  assert.ok(result.logs.some(log => String(log.args[0]).includes('/bitbucket/projects/SU2/')));
});

test('existing repository is never modified', async () => {
  const result = await run({ apply: true, exists: true });
  assert.equal(writes(result).length, 0);
  assert.match(errors(result).join('\n'), /уже существует/);
});

test('cancelled confirmation does not write', async () => {
  const result = await run({ apply: true, confirm: false });
  assert.equal(writes(result).length, 0);
  assert.equal(errors(result).length, 0);
});

test('repository appearing during confirmation stops creation', async () => {
  const result = await run({ apply: true, race: true });
  assert.equal(writes(result).length, 0);
  assert.match(errors(result).join('\n'), /уже появился/);
});

for (const branches of [['same', 'same'], ['master', 'bad..name'], ['master', 'bad.lock'],
  ['master', 'a//b'], ['master', 'bad\\name'], ['foo', 'foo/bar'], ['master', 'bad name'],
  ['master', 'bad[ref'], ['master', 'bad@{ref'], ['master', '-bad'], ['master', 'bad.'],
  ['master', '@'], ['master', '.hidden'], ['master', 'main']]) {
  test(`reject invalid configuration ${JSON.stringify(branches)} before REST`, async () => {
    const result = await run({ apply: true, branches });
    assert.equal(result.calls.length, 0);
    assert.ok(errors(result).length > 0);
  });
}

test('preflight authentication error does not create', async () => {
  const result = await run({ apply: true, failAt: 'GET /projects/SU2' });
  assert.equal(writes(result).length, 0);
  assert.match(errors(result).join('\n'), /HTTP 403/);
});

for (const failAt of ['PUT /projects/SU2/repos/new_repo/browse/README.md',
  'POST /projects/SU2/repos/new_repo/branches', 'PUT /projects/SU2/repos/new_repo/branches/default']) {
  test(`partial failure ${failAt} leaves repository and reports incomplete stage`, async () => {
    const result = await run({ apply: true, failAt });
    assert.equal(result.exists, true);
    assert.ok(writes(result).every(call => call.method !== 'DELETE'));
    assert.match(errors(result).join('\n'), /не завершён/);
    assert.ok(!result.logs.some(log => String(log.args[0]).includes('ГОТОВО')));
    assert.equal(writes(result).at(-1).method + ' ' + writes(result).at(-1).route, failAt);
  });
}

test('verification mismatch must not report success', async () => {
  const result = await run({ apply: true, badDefault: true });
  assert.match(errors(result).join('\n'), /не соответствует плану/);
  assert.ok(!result.logs.some(log => String(log.args[0]).includes('ГОТОВО')));
});

test('explicit SU2 project does not depend on the open page', async () => {
  const result = await run({ apply: true, pathname: '/projects/OTHER/repos' });
  assert.equal(errors(result).length, 0);
  assert.ok(result.calls.every(call => call.route.startsWith('/projects/SU2')));
  assert.equal(result.branches.length, 2);
});
