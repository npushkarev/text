#!/usr/bin/env python3
"""Скопировать текущие исходники cs_doc в as_doc без истории Git."""
import argparse
import base64
import getpass
import json
import os
from pathlib import Path
import re
import shutil
import subprocess
import sys
import tempfile
import urllib.error
import urllib.parse
import urllib.request


class NoRedirect(urllib.request.HTTPRedirectHandler):
    def redirect_request(self, req, fp, code, msg, headers, newurl):
        return None


class Bitbucket:
    def __init__(self, base, user, password):
        self.base = base.rstrip("/") + "/rest/api/1.0"
        token = base64.b64encode((user + ":" + password).encode()).decode()
        self.auth = "Basic " + token
        self.opener = urllib.request.build_opener(NoRedirect())

    def request(self, path, method="GET", body=None, missing=False):
        headers = {"Authorization": self.auth, "Accept": "application/json"}
        data = None
        if body is not None:
            headers["Content-Type"] = "application/json"
            data = json.dumps(body).encode()
        req = urllib.request.Request(self.base + path, data=data, headers=headers, method=method)
        try:
            with self.opener.open(req, timeout=60) as response:
                text = response.read()
        except urllib.error.HTTPError as error:
            if missing and error.code == 404:
                return None
            detail = "HTTP {}".format(error.code)
            try:
                errors = json.loads(error.read()).get("errors", [])
                detail += ": " + "; ".join(item.get("message", "") for item in errors)
            except (ValueError, AttributeError):
                pass
            raise RuntimeError("{} {}: {}".format(method, path, detail)) from None
        return json.loads(text) if text else None


def git(*args, cwd=None, text=True):
    return subprocess.check_output(["git", *args], cwd=cwd, universal_newlines=text)


def clone_url(info):
    for link in info.get("links", {}).get("clone", []):
        if link.get("name") == "http":
            return link["href"]
    raise RuntimeError("Bitbucket не вернул HTTP clone URL.")


def copy_sources(args, api):
    if args.source == args.target:
        raise RuntimeError("Источник и назначение должны отличаться.")
    for name in (args.source, args.target):
        if not re.fullmatch(r"[a-z0-9][a-z0-9._-]*", name):
            raise RuntimeError("Недопустимое имя репозитория: " + name)
    for branch in (args.first_branch, args.second_branch):
        git("check-ref-format", "--branch", branch)
    if (args.first_branch == args.second_branch or
            args.first_branch.startswith(args.second_branch + "/") or
            args.second_branch.startswith(args.first_branch + "/")):
        raise RuntimeError("Имена двух веток совпадают или конфликтуют.")
    if args.default_branch not in (args.first_branch, args.second_branch):
        raise RuntimeError("Ветка по умолчанию должна быть одной из двух создаваемых.")

    project_path = "/projects/" + urllib.parse.quote(args.project, safe="")
    source_path = project_path + "/repos/" + args.source
    target_path = project_path + "/repos/" + args.target
    project = api.request(project_path)
    if project.get("key") != args.project:
        raise RuntimeError("Bitbucket вернул другой ключ проекта.")
    source_info = api.request(source_path)
    target_info = api.request(target_path, missing=True)
    if target_info and git("ls-remote", "--refs", clone_url(target_info)).strip():
        raise RuntimeError("{} уже содержит Git-ссылки. Ничего не изменено.".format(args.target))
    branch = args.source_branch
    if not branch:
        default = api.request(source_path + "/branches/default")
        if not default["id"].startswith("refs/heads/"):
            raise RuntimeError("Bitbucket не вернул исходную ветку по умолчанию.")
        branch = default["id"][len("refs/heads/"):]

    work = Path(tempfile.mkdtemp(prefix="bitbucket-copy-"))
    print("Рабочий каталог: {} (сохраняется и при ошибке)".format(work), flush=True)
    source = work / "source.git"
    target = work / "target.git"
    git("clone", "--bare", "--single-branch", "--branch", branch, clone_url(source_info), str(source))
    commit = git("--git-dir=" + str(source), "rev-parse", "HEAD").strip()
    tree = git("--git-dir=" + str(source), "rev-parse", commit + "^{tree}").strip()
    entries = git("--git-dir=" + str(source), "ls-tree", "-r", "-z", commit, text=False).split(b"\0")
    entries = list(filter(None, entries))
    for entry in entries:
        meta, filename = entry.split(b"\t", 1)
        if meta.startswith(b"160000 "):
            raise RuntimeError("В источнике есть submodule. Сначала нужен отдельный перенос его файлов.")
        if filename.split(b"/")[-1] == b".gitattributes" and meta.startswith(b"100"):
            blob = meta.split()[2].decode()
            attributes = git("--git-dir=" + str(source), "cat-file", "blob", blob, text=False)
            if any(re.search(rb"\bfilter\s*=\s*lfs\b", line) for line in attributes.splitlines()
                   if not line.lstrip().startswith(b"#")):
                raise RuntimeError("В источнике используется Git LFS. Скрипт не переносит LFS-объекты.")
    if not entries:
        raise RuntimeError("В исходном коммите нет файлов.")

    git("init", "--bare", str(target))
    git("--git-dir=" + str(target), "fetch", "--no-tags", str(source), commit)
    identity = []
    if args.git_name:
        identity += ["-c", "user.name=" + args.git_name]
    if args.git_email:
        identity += ["-c", "user.email=" + args.git_email]
    # Новый корневой коммит с тем же деревом: без родителей и преобразования файлов.
    initial = git(*identity, "--git-dir=" + str(target), "commit-tree", tree,
                  "-m", "Initial sources from {} at {}".format(args.source, commit)).strip()
    for name in (args.first_branch, args.second_branch):
        git("--git-dir=" + str(target), "update-ref", "refs/heads/" + name, initial)
    print("Проект: {} ({})".format(args.project, project.get("name", "")))
    print("Источник: {}@{} ({})".format(args.source, branch, commit))
    print("Назначение: {}. Файлов: {}".format(args.target, len(entries)))
    print("Ветки: {}, {}. По умолчанию: {}".format(args.first_branch, args.second_branch, args.default_branch))
    if not args.apply:
        print("DRY RUN: Bitbucket не изменён. Для переноса добавьте --apply.")
        return

    stage = "создание или проверка целевого репозитория"
    try:
        target_info = api.request(target_path, missing=True)
        if target_info is None:
            target_info = api.request(project_path + "/repos", "POST", {
                "name": args.target, "scmId": "git", "forkable": True,
            })
            print("CREATED: " + args.target, flush=True)
        if (target_info.get("slug") != args.target or
                target_info.get("project", {}).get("key") != args.project):
            raise RuntimeError("Сервер вернул другое имя репозитория или проект.")
        url = clone_url(target_info)
        stage = "проверка пустого репозитория перед push"
        if git("ls-remote", "--refs", url).strip():
            raise RuntimeError("Целевой репозиторий уже содержит Git-ссылки. Push остановлен.")
        stage = "отправка исходников и двух веток"
        git("--git-dir=" + str(target), "push", "--atomic", url,
            "refs/heads/" + args.first_branch, "refs/heads/" + args.second_branch)
        stage = "выбор ветки по умолчанию"
        api.request(target_path + "/branches/default", "PUT", {
            "id": "refs/heads/" + args.default_branch,
        })
        stage = "проверка результата"
        expected = {"refs/heads/" + name: initial for name in (args.first_branch, args.second_branch)}
        refs = dict((line.split()[1], line.split()[0]) for line in git("ls-remote", "--refs", url).splitlines())
        default = api.request(target_path + "/branches/default")
        if refs != expected or default.get("id") != "refs/heads/" + args.default_branch:
            raise RuntimeError("Результат не соответствует плану.")
        print("ГОТОВО: {} -> {}. Коммит: {}".format(args.source, args.target, initial))
    except Exception:
        print("Не завершён этап: {}. Целевой репозиторий не удаляется.".format(stage), file=sys.stderr)
        raise


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--bitbucket", default="https://bitbucket.inc.elara.local")
    parser.add_argument("--project", default="SU2")
    parser.add_argument("--source", default="cs_doc")
    parser.add_argument("--target", default="as_doc")
    parser.add_argument("--source-branch", default="")
    parser.add_argument("--first-branch", default="master")
    parser.add_argument("--second-branch", default="develop")
    parser.add_argument("--default-branch", default="develop")
    parser.add_argument("--user", default=os.environ.get("BITBUCKET_USER", ""))
    parser.add_argument("--git-name", default=os.environ.get("GIT_NAME", ""))
    parser.add_argument("--git-email", default=os.environ.get("GIT_EMAIL", ""))
    parser.add_argument("--apply", action="store_true", help="создать репозиторий и отправить исходники")
    args = parser.parse_args()
    try:
        if not shutil.which("git"):
            raise RuntimeError("git не найден.")
        parsed = urllib.parse.urlsplit(args.bitbucket)
        if parsed.scheme not in ("https", "http") or not parsed.netloc or parsed.username or parsed.query or parsed.fragment:
            raise RuntimeError("--bitbucket должен быть адресом сервера без логина и пароля.")
        if parsed.scheme == "http":
            print("ВНИМАНИЕ: HTTP передаёт учётные данные без шифрования.", file=sys.stderr)
        user = args.user or input("Логин Bitbucket: ").strip()
        if not user or ":" in user:
            raise RuntimeError("Укажите логин Bitbucket.")
        password = getpass.getpass("Пароль Bitbucket для REST: ")
        copy_sources(args, Bitbucket(args.bitbucket, user, password))
        return 0
    except (RuntimeError, ValueError, KeyError, OSError, subprocess.CalledProcessError) as error:
        print("ОШИБКА: " + str(error), file=sys.stderr)
        return 1
    except (KeyboardInterrupt, EOFError):
        print("Остановлено.", file=sys.stderr)
        return 1


if __name__ == "__main__":
    sys.exit(main())
