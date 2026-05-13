from __future__ import annotations

import json
import urllib.error
import urllib.request
from dataclasses import dataclass
from typing import Any

from system_log import sanitize_text


class GitHubReportingError(RuntimeError):
    pass


@dataclass(frozen=True)
class GitHubIssue:
    url: str
    number: int
    node_id: str


class GitHubReporter:
    def __init__(
        self,
        *,
        enabled: bool,
        token: str,
        repository: str,
        project_owner: str,
        project_number: int,
        timeout_seconds: int = 20,
    ) -> None:
        self.enabled = enabled
        self.token = token.strip()
        self.repository = repository.strip()
        self.project_owner = project_owner.strip()
        self.project_number = int(project_number)
        self.timeout_seconds = max(1, int(timeout_seconds))

    @property
    def is_configured(self) -> bool:
        return bool(self.enabled and self.token and self.repository and self.project_owner and self.project_number > 0)

    def create_self_report_issue(
        self,
        *,
        title: str,
        body: str,
        labels: list[str] | None = None,
    ) -> GitHubIssue | None:
        if not self.is_configured:
            return None

        issue = self._create_issue(title=title, body=body, labels=labels or [])
        try:
            self._add_issue_to_project(issue.node_id)
        except GitHubReportingError:
            raise
        except Exception as exc:  # pragma: no cover - defensive wrapper
            raise GitHubReportingError(str(exc)) from exc
        return issue

    def _create_issue(self, *, title: str, body: str, labels: list[str]) -> GitHubIssue:
        payload = {"title": sanitize_text(title, 200), "body": body, "labels": labels}
        data = self._request_json(
            f"https://api.github.com/repos/{self.repository}/issues",
            payload,
        )
        return GitHubIssue(url=str(data["html_url"]), number=int(data["number"]), node_id=str(data["node_id"]))

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
            raise GitHubReportingError("GitHub project not found")
        return str(project["id"])

    def _graphql(self, query: str, variables: dict[str, Any]) -> dict[str, Any]:
        response = self._request_json("https://api.github.com/graphql", {"query": query, "variables": variables})
        if response.get("errors"):
            raise GitHubReportingError(sanitize_text(json.dumps(response["errors"]), 500))
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
                return json.loads(response.read().decode("utf-8"))
        except urllib.error.HTTPError as exc:
            body = exc.read().decode("utf-8", "replace")
            raise GitHubReportingError(f"GitHub API {exc.code}: {sanitize_text(body, 500)}") from exc
        except urllib.error.URLError as exc:
            raise GitHubReportingError(f"GitHub API unavailable: {sanitize_text(str(exc), 300)}") from exc
