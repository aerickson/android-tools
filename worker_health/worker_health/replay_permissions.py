"""Scope diagnostics and curated replay grants; never infer wildcard prefixes."""

import tempfile
from pathlib import Path
from urllib.parse import quote

CLIENTS_URL = "https://firefox-ci-tc.services.mozilla.com/auth/clients"
WORKLOAD_GRANTS = (
    "generic-worker:cache:gecko-level-1-*",
    "secrets:get:project/perftest/gecko/level-1/*",
    "secrets:get:project/releng/gecko/build/level-1/*",
    "queue:get-artifact:project/gecko/*",
)
ARTIFACT_NOTE = (
    "queue:get-artifact:project/gecko/* covers matching Gecko artifacts across SCM levels. "
    "Replay still requires level-1 sources."
)


def recommended_scopes(destination):
    return [
        f"queue:create-task:lowest:{destination}",
        "queue:create-task:project:none",
        "queue:scheduler-id:-",
        *WORKLOAD_GRANTS,
    ]


def permission_summary(rows, destination, client_id=None, checked_at=None):
    blocked = [
        {"name": row["name"], "missing_scopes": row["missing_scopes"]} for row in rows if row.get("missing_scopes")
    ]
    missing = sorted({scope for row in blocked for scope in row["missing_scopes"]})
    suggestions = set()
    for scope in missing:
        suggestions.add(next((grant for grant in WORKLOAD_GRANTS if scope.startswith(grant[:-1])), scope))
    return {
        "blocked_count": len(blocked),
        "workloads": blocked,
        "missing_scopes": missing,
        "suggested_scopes": sorted(suggestions),
        "recommended_scopes": recommended_scopes(destination),
        "client_url": f"{CLIENTS_URL}/{quote(client_id, safe='')}" if client_id else CLIENTS_URL,
        "checked_at": checked_at,
    }


def write_suggestions(path, scopes):
    """Atomically replace the copyable file, including when no grants are missing."""
    path = Path(path)
    temporary = None
    try:
        with tempfile.NamedTemporaryFile(mode="w", encoding="utf-8", dir=path.parent, delete=False) as output:
            temporary = Path(output.name)
            output.write("".join(scope + "\n" for scope in scopes))
        temporary.replace(path)
    finally:
        if temporary:
            temporary.unlink(missing_ok=True)
