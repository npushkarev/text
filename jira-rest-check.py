#!/usr/bin/env python3
"""python3 jira-rest-check.py --jira 'https://<адрес Jira>' --project 'CS' --user '<логин>'"""
import argparse
import base64
import getpass
import json
import ssl
import sys
import urllib.error
import urllib.parse
import urllib.request

HEADERS = ("Location", "X-AUSERNAME", "X-Seraph-LoginReason", "X-Authentication-Denied-Reason",
           "WWW-Authenticate", "Content-Type")


class NoRedirect(urllib.request.HTTPRedirectHandler):
    def redirect_request(self, req, fp, code, msg, headers, newurl):
        return None


def check(opener, base, path, auth):
    url = base + "/rest/api/2/" + path
    req = urllib.request.Request(url, headers={"Accept": "application/json"})
    if auth:
        req.add_header("Authorization", auth)
    try:
        resp = opener.open(req, timeout=30)
        code, headers, body = resp.status, resp.headers, resp.read(4096)
    except urllib.error.HTTPError as error:
        code, headers, body = error.code, error.headers, error.read(4096)
    except urllib.error.URLError as error:
        return {"path": path, "error": str(error.reason)}
    result = {"path": path, "code": code}
    for name in HEADERS:
        if headers.get(name):
            result[name] = headers.get(name)
    try:
        data = json.loads(body.decode("utf-8", "replace"))
        if isinstance(data, dict):
            if data.get("errorMessages"):
                result["errors"] = "; ".join(data["errorMessages"])
            for key in ("version", "baseUrl", "name", "key"):
                if key in data:
                    result[key] = data[key]
        elif isinstance(data, list):
            result["items"] = len(data)
    except ValueError:
        pass
    return result


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--jira", required=True, help="значение Jira.Address, например https://jira.example.local")
    parser.add_argument("--project", required=True, help="значение ProjectKey")
    parser.add_argument("--user", help="значение Jira.Username; без него проверяются только анонимные запросы")
    parser.add_argument("--token", action="store_true", help="вместо пароля спросить Personal Access Token (Bearer)")
    parser.add_argument("--ca-file", help="файл корпоративного CA, если агент ему ещё не доверяет")
    args = parser.parse_args()

    base = args.jira.rstrip("/")
    if urllib.parse.urlsplit(base).scheme not in ("http", "https"):
        parser.error("--jira должен начинаться с http:// или https://")
    context = ssl.create_default_context(cafile=args.ca_file)
    opener = urllib.request.build_opener(NoRedirect(), urllib.request.HTTPSHandler(context=context))

    auth = ""
    if args.token:
        auth = "Bearer " + getpass.getpass("Personal Access Token: ")
    elif args.user:
        secret = getpass.getpass("Пароль %s: " % args.user)
        auth = "Basic " + base64.b64encode((args.user + ":" + secret).encode()).decode()

    key = urllib.parse.quote(args.project, safe="")
    jql = urllib.parse.quote("project = %s" % args.project, safe="")
    paths = ["serverInfo", "myself", "project/" + key, "project/" + key + "/versions",
             "search?jql=%s&maxResults=1&fields=summary" % jql]

    print("Jira:", base)
    print("Авторизация:", auth.split(" ", 1)[0] if auth else "нет (анонимно)")
    failed = False
    for path in paths:
        result = check(opener, base, path, auth)
        print(json.dumps(result, ensure_ascii=False))
        failed = failed or "error" in result or not 200 <= result.get("code", 0) < 300
    return 1 if failed else 0


if __name__ == "__main__":
    try:
        sys.exit(main())
    except KeyboardInterrupt:
        sys.exit(130)
