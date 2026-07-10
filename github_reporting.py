from __future__ import annotations

import hashlib
import hmac
import json
import socket
import urllib.error
import urllib.request
from dataclasses import dataclass, replace
from typing import Any

from system_log import sanitize_text


class GitHubReportingError(RuntimeError):
    def __init__(
        self,
        category: str,
        *,
        retry_safe: bool = False,
        status_code: int | None = None,
    ) -> None:
        self.category = str(category or "outcome_unknown")
        self.retry_safe = bool(retry_safe)
        self.status_code = int(status_code) if status_code is not None else None
        super().__init__(self.category)


@dataclass(frozen=True)
class GitHubIssue:
    url: str
    number: int
    node_id: str
    project_status: str = "disabled"


class GitHubReporter:
    def __init__(
        self,
        *,
        enabled: bool,
        token: str,
        repository: str,
        project_owner: str = "",
        project_number: int = 0,
        project_add_enabled: bool = False,
        fingerprint_secret: str = "",
        timeout_seconds: int = 20,
    ) -> None:
        self.enabled = enabled
        self.token = token.strip()
        self.repository = repository.strip()
        self.project_owner = project_owner.strip()
        self.project_number = int(project_number)
        self.project_add_enabled = bool(project_add_enabled)
        self.fingerprint_secret = fingerprint_secret.strip()
        self.timeout_seconds = max(1, int(timeout_seconds))

    @property
    def is_configured(self) -> bool:
        return bool(self.enabled and self.token and self.repository)

    def create_self_report_issue(
        self,
        *,
        title: str,
        body: str,
        labels: list[str] | None = None,
        dedupe_key: str,
    ) -> GitHubIssue | None:
        if not self.is_configured:
            return None

        marker = self._dedupe_marker(dedupe_key)
        public_body = f"{body.rstrip()}\n\n{marker}\n"
        issue = self._create_issue(title=title, body=public_body, labels=labels or [])
        if not self.project_add_enabled:
            return issue
        if not self.project_owner or self.project_number <= 0:
            return replace(issue, project_status="failed")
        try:
            self._add_issue_to_project(issue.node_id)
        except Exception:
            return replace(issue, project_status="failed")
        return replace(issue, project_status="added")

    def _dedupe_marker(self, dedupe_key: str) -> str:
        secret = (self.fingerprint_secret or self.token).encode("utf-8")
        message = f"github-self-report:v1\0{self.repository}\0{dedupe_key}".encode("utf-8")
        digest = hmac.new(secret, message, hashlib.sha256).hexdigest()[:24]
        return f"<!-- aigan-self-report:{digest} -->"

    def _create_issue(self, *, title: str, body: str, labels: list[str]) -> GitHubIssue:
        payload = {"title": sanitize_text(title, 200), "body": body, "labels": labels}
        data = self._request_json(
            f"https://api.github.com/repos/{self.repository}/issues",
            payload,
        )
        try:
            raw_url = data["html_url"]
            raw_number = data["number"]
            raw_node_id = data["node_id"]
            if not isinstance(raw_url, str) or not raw_url.strip():
                raise ValueError("missing issue URL")
            if isinstance(raw_number, bool):
                raise ValueError("invalid issue number")
            number = int(raw_number)
            if not isinstance(raw_node_id, str) or not raw_node_id.strip():
                raise ValueError("missing issue node id")
            if number <= 0:
                raise ValueError("missing issue identity")
        except (KeyError, TypeError, ValueError):
            raise GitHubReportingError("invalid_response") from None
        return GitHubIssue(url=raw_url.strip(), number=number, node_id=raw_node_id.strip())

    def _add_issue_to_project(self, issue_node_id: str) -> None:
        project_id = self._project_id()
        mutation = """
        mutation($projectId: ID!, $contentId: ID!) {
          addProjectV2ItemById(input: {projectId: $projectId, contentId: $contentId}) {
            item { id }
          }
        }
        """
        self._graphql(mutation, {"projectId": project_id, "contentId": issue_node_id})

    def _project_id(self) -> str:
        query = """
        query($login: String!, $number: Int!) {
          user(login: $login) { projectV2(number: $number) { id } }
          organization(login: $login) { projectV2(number: $number) { id } }
        }
        """
        data = self._graphql(query, {"login": self.project_owner, "number": self.project_number})
        project = (data.get("user") or {}).get("projectV2") or (data.get("organization") or {}).get("projectV2")
        if not project or not project.get("id"):
            raise GitHubReportingError("project_not_found", retry_safe=True)
        return str(project["id"])

    def _graphql(self, query: str, variables: dict[str, Any]) -> dict[str, Any]:
        response = self._request_json("https://api.github.com/graphql", {"query": query, "variables": variables})
        if response.get("errors"):
            raise GitHubReportingError("graphql_error")
        return dict(response.get("data") or {})

    def _request_json(self, url: str, payload: dict[str, Any]) -> dict[str, Any]:
        data = json.dumps(payload).encode("utf-8")
        request = urllib.request.Request(
            url,
            data=data,
            headers={
                "Accept": "application/vnd.github+json",
                "Authorization": f"Bearer {self.token}",
                "Content-Type": "application/json",
                "User-Agent": "Aigan-self-analysis",
                "X-GitHub-Api-Version": "2022-11-28",
            },
            method="POST",
        )
        try:
            with urllib.request.urlopen(request, timeout=self.timeout_seconds) as response:
                raw_response = response.read()
        except urllib.error.HTTPError as exc:
            status_code = int(exc.code)
            raise GitHubReportingError(
                http_error_category(status_code),
                retry_safe=400 <= status_code < 500 and status_code not in {408, 425, 429},
                status_code=status_code,
            ) from None
        except (TimeoutError, socket.timeout):
            raise GitHubReportingError("request_timeout") from None
        except urllib.error.URLError:
            raise GitHubReportingError("transport_error") from None
        try:
            decoded = json.loads(raw_response.decode("utf-8"))
        except (UnicodeDecodeError, json.JSONDecodeError):
            raise GitHubReportingError("invalid_response") from None
        if not isinstance(decoded, dict):
            raise GitHubReportingError("invalid_response")
        return decoded


def http_error_category(status_code: int) -> str:
    return {
        401: "authentication_failed",
        403: "permission_denied",
        404: "not_found",
        408: "request_timeout",
        422: "validation_failed",
        429: "rate_limited",
    }.get(status_code, "client_error" if 400 <= status_code < 500 else "server_error")
