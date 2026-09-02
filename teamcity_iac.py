#!/usr/bin/env python3
"""Reconcile TeamCity projects for the split EL_CONF repositories.

The existing EL_CONF project is the source of truth. Each requested repository
gets a sibling TeamCity project copied from EL_CONF, so its platform matrix,
templates, dependencies, features, and agent requirements stay identical.

The command is plan-only by default. Pass --apply to create missing projects
and set their repository parameters. It never deletes TeamCity objects.
"""

import argparse
import base64
import json
import os
import re
import socket
import sys
import time
import urllib.error
import urllib.parse
import urllib.request
import xml.etree.ElementTree as ET


REPOSITORIES = (
    "as_system_data",
    "el_conf_chart_data_manager",
    "el_conf_config_builder",
    "el_conf_device_config_manager",
    "el_conf_core_sdk",
    "el_conf_gui_sdk",
    "el_conf_themes",
    "el_conf_utils",
    "qt_rest_api",
    "el_conf_elecont_protocol_client",
    "el_conf_plugin_sdk",
    "el_conf_sura_connector_client",
    "el_conf_chart_ui_plugin",
    "as_app",
)

DEFAULT_TEAMCITY_URL = "http://teamcity.inc.elara.local"
DEFAULT_SOURCE_PATH = "SURA2/COMPONENTS/CMAKE/EL_CONF"
MANAGED_DESCRIPTION = (
    "Managed by su2-repos-setup/teamcity_iac.py; source: "
    + DEFAULT_SOURCE_PATH
)
MANAGED_PARAMETER = "iac.managedBy"
MANAGED_VALUE = "su2-repos-setup/teamcity_iac.py"


class TeamCityError(RuntimeError):
    def __init__(self, message, status=None):
        super().__init__(message)
        self.status = status


class TeamCity:
    def __init__(self, base_url, timeout=30, retries=4):
        self.base_url = base_url.rstrip("/")
        self.timeout = timeout
        self.retries = retries

        token = os.environ.get("TC_TOKEN")
        user = os.environ.get("TC_USER")
        password = os.environ.get("TC_PASSWORD")
        if bool(user) != bool(password):
            raise TeamCityError("TC_USER and TC_PASSWORD must be set together")

        self.headers = {"User-Agent": "su2-teamcity-iac/1.0"}
        if token:
            self.headers["Authorization"] = "Bearer " + token
            auth_prefix = "/app/rest/2018.1"
        elif user and password:
            raw = (user + ":" + password).encode("utf-8")
            self.headers["Authorization"] = "Basic " + base64.b64encode(raw).decode("ascii")
            auth_prefix = "/httpAuth/app/rest/2018.1"
        else:
            auth_prefix = "/app/rest/2018.1"
        self.api_url = self.base_url + auth_prefix

    def request(self, method, path, body=None, content_type=None, accept="application/json"):
        headers = dict(self.headers)
        headers["Accept"] = accept
        if content_type:
            headers["Content-Type"] = content_type
        request = urllib.request.Request(
            self.api_url + path,
            data=body,
            headers=headers,
            method=method,
        )

        for attempt in range(1, self.retries + 1):
            try:
                with urllib.request.urlopen(request, timeout=self.timeout) as response:
                    return response.read()
            except urllib.error.HTTPError as exc:
                details = exc.read().decode("utf-8", "replace").strip()
                if exc.code >= 500 and attempt < self.retries:
                    time.sleep(attempt)
                    continue
                raise TeamCityError(
                    "TeamCity HTTP {} for {} {}: {}".format(
                        exc.code, method, path, details or exc.reason
                    ),
                    status=exc.code,
                ) from exc
            except (urllib.error.URLError, ConnectionResetError, socket.timeout) as exc:
                if attempt < self.retries:
                    time.sleep(attempt)
                    continue
                raise TeamCityError(
                    "Cannot reach TeamCity after {} attempts: {}".format(
                        self.retries, exc
                    )
                ) from exc

        raise TeamCityError("Unexpected TeamCity request failure")

    def json(self, method, path):
        raw = self.request(method, path)
        try:
            return json.loads(raw.decode("utf-8"))
        except (UnicodeDecodeError, json.JSONDecodeError) as exc:
            raise TeamCityError("TeamCity returned a non-JSON response") from exc

    def projects(self):
        query = urllib.parse.urlencode(
            {
                "locator": "count:10000",
                "fields": "project(id,name,parentProjectId,description)",
            }
        )
        payload = self.json("GET", "/projects?" + query)
        return payload.get("project", [])

    def copy_project(self, source_id, parent_id, project_id, name):
        root = ET.Element(
            "newProjectDescription",
            {
                "copyAllAssociatedSettings": "true",
                "id": project_id,
                "name": name,
                "description": MANAGED_DESCRIPTION,
            },
        )
        ET.SubElement(root, "sourceProject", {"locator": "id:" + source_id})
        ET.SubElement(root, "parentProject", {"locator": "id:" + parent_id})
        self.request(
            "POST",
            "/projects",
            ET.tostring(root, encoding="utf-8", xml_declaration=True),
            "application/xml",
        )

    def set_project_name(self, project_id, display_name):
        locator = urllib.parse.quote("id:" + project_id, safe=":_-")
        self.request(
            "PUT",
            "/projects/{}/name".format(locator),
            display_name.encode("utf-8"),
            "text/plain; charset=utf-8",
            accept="text/plain",
        )

    def parameter_names(self, project_id):
        locator = urllib.parse.quote("id:" + project_id, safe=":_-")
        query = urllib.parse.urlencode({"fields": "property(name)"})
        payload = self.json("GET", "/projects/{}/parameters?{}".format(locator, query))
        return {item["name"] for item in payload.get("property", [])}

    def set_parameter(self, project_id, name, value, existing_names):
        locator = urllib.parse.quote("id:" + project_id, safe=":_-")
        encoded_name = urllib.parse.quote(name, safe="._-")
        if name in existing_names:
            self.request(
                "PUT",
                "/projects/{}/parameters/{}".format(locator, encoded_name),
                value.encode("utf-8"),
                "text/plain; charset=utf-8",
                accept="text/plain",
            )
            return

        prop = ET.Element("property", {"name": name, "value": value})
        self.request(
            "POST",
            "/projects/{}/parameters".format(locator),
            ET.tostring(prop, encoding="utf-8", xml_declaration=True),
            "application/xml",
        )
        existing_names.add(name)

    def reconcile_parameters(self, project_id, repo_name):
        existing = self.parameter_names(project_id)
        desired = {
            "repoProject": "su2",
            "repoName": repo_name,
            "projectName": repo_name,
            MANAGED_PARAMETER: MANAGED_VALUE,
        }
        for name, value in desired.items():
            self.set_parameter(project_id, name, value, existing)


def normalized_path(value):
    parts = [part.strip() for part in value.split("/") if part.strip()]
    if not parts:
        raise TeamCityError("Source project path is empty")
    return tuple(parts)


def project_path(project, projects_by_id):
    names = []
    current = project
    visited = set()
    while current and current.get("id") != "_Root":
        current_id = current.get("id")
        if current_id in visited:
            raise TeamCityError("Cycle in TeamCity project hierarchy at " + str(current_id))
        visited.add(current_id)
        names.append(current.get("name", ""))
        parent_id = current.get("parentProjectId")
        if not parent_id:
            break
        current = projects_by_id.get(parent_id)
        if current is None and parent_id != "_Root":
            raise TeamCityError("Missing parent project in REST response: " + parent_id)
    return tuple(reversed(names))


def find_project_by_path(projects, wanted_path):
    by_id = {project["id"]: project for project in projects}
    wanted_folded = tuple(part.casefold() for part in wanted_path)
    matches = [
        project
        for project in projects
        if tuple(part.casefold() for part in project_path(project, by_id)) == wanted_folded
    ]
    if len(matches) != 1:
        raise TeamCityError(
            "Expected exactly one TeamCity project at '{}', found {}".format(
                "/".join(wanted_path), len(matches)
            )
        )
    return matches[0]


def id_segment(name):
    words = re.findall(r"[A-Za-z0-9]+", name)
    if not words:
        raise TeamCityError("Cannot generate TeamCity ID from " + repr(name))
    return "".join(word[:1].upper() + word[1:] for word in words)


def desired_project_id(parent_id, repo_name):
    return parent_id + "_" + id_segment(repo_name)


def classify(existing_by_name, existing_by_id, parent_id, repo_name):
    project_id = desired_project_id(parent_id, repo_name)
    by_name = existing_by_name.get(repo_name.casefold())
    by_id = existing_by_id.get(project_id)

    if by_name and by_name.get("id") != project_id:
        return "CONFLICT", project_id, by_name
    if by_id and by_id.get("name", "").casefold() != repo_name.casefold():
        return "CONFLICT", project_id, by_id

    existing = by_name or by_id
    if not existing:
        return "CREATE", project_id, None
    if existing.get("description", "").startswith("Managed by su2-repos-setup/"):
        return "RECONCILE", project_id, existing
    return "UNMANAGED", project_id, existing


def parse_args(argv):
    parser = argparse.ArgumentParser(
        description="Create TeamCity projects for split EL_CONF repositories"
    )
    parser.add_argument(
        "--apply",
        action="store_true",
        help="apply the plan; without this flag no TeamCity settings are changed",
    )
    parser.add_argument(
        "--teamcity-url",
        default=os.environ.get("TEAMCITY_URL", DEFAULT_TEAMCITY_URL),
    )
    parser.add_argument(
        "--source-path",
        default=os.environ.get("TC_SOURCE_PATH", DEFAULT_SOURCE_PATH),
        help="full path of the TeamCity project to copy",
    )
    parser.add_argument("--timeout", type=int, default=30)
    return parser.parse_args(argv)


def main(argv=None):
    args = parse_args(argv or sys.argv[1:])
    source_path = normalized_path(args.source_path)
    tc = TeamCity(args.teamcity_url, timeout=args.timeout)
    projects = tc.projects()
    source = find_project_by_path(projects, source_path)
    parent_id = source.get("parentProjectId")
    if not parent_id:
        raise TeamCityError("Source project has no parent and cannot be cloned safely")

    direct_children = [
        project for project in projects if project.get("parentProjectId") == parent_id
    ]
    by_name = {project.get("name", "").casefold(): project for project in direct_children}
    by_id = {project.get("id"): project for project in direct_children}

    print("Mode:   {}".format("APPLY" if args.apply else "PLAN"))
    print("Source: {} ({})".format(" / ".join(source_path), source["id"]))
    print("Target: {}".format(parent_id))

    plan = []
    conflicts = 0
    for repo_name in REPOSITORIES:
        state, project_id, existing = classify(
            by_name, by_id, parent_id, repo_name
        )
        plan.append((state, project_id, repo_name, existing))
        suffix = ""
        if existing:
            suffix = " (existing id: {})".format(existing.get("id"))
        print("{:<10} {} -> {}{}".format(state, repo_name, project_id, suffix))
        if state in ("CONFLICT", "UNMANAGED"):
            conflicts += 1

    if conflicts:
        raise TeamCityError(
            "Refusing to apply: {} existing project conflict(s) require manual review".format(
                conflicts
            )
        )

    if not args.apply:
        print("\nPlan only. Run again with --apply to create/reconcile these projects.")
        return 0

    for state, project_id, repo_name, _existing in plan:
        if state == "CREATE":
            print("Copying EL_CONF -> {}...".format(repo_name))
            tc.copy_project(source["id"], parent_id, project_id, repo_name.upper())
        tc.set_project_name(project_id, repo_name.upper())
        tc.reconcile_parameters(project_id, repo_name)
        print("OK       {}".format(repo_name))

    print("\nApplied: {} TeamCity projects are managed.".format(len(REPOSITORIES)))
    return 0


if __name__ == "__main__":
    try:
        sys.exit(main())
    except TeamCityError as exc:
        print("ERROR: " + str(exc), file=sys.stderr)
        sys.exit(2)
