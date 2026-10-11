"""Guards against drift: every metric the Grafana dashboard queries must really exist."""

import json
import re
from pathlib import Path

import yaml

import app.metrics  # noqa: F401  (registers the workflow metrics)
from app.core.observability.metrics import render

ROOT = Path(__file__).resolve().parents[2]
DASHBOARD = ROOT / "deploy" / "grafana" / "dashboards" / "workflow-platform.json"
# Metric names used by this project all start with one of these words.
_METRIC = re.compile(
    r"\b((?:http|workflow|llm|tool|approval|cache|circuit)_[a-z_]+?)"
    r"(?:_bucket|_count|_sum)?\b(?=[\s{\[(),]|$)"
)


def exposed_names() -> set[str]:
    body = render()[0].decode()
    return set(re.findall(r"^# TYPE (\S+) ", body, flags=re.MULTILINE))


def test_every_dashboard_query_uses_an_existing_metric() -> None:
    dashboard = json.loads(DASHBOARD.read_text())
    exprs = [t["expr"] for p in dashboard["panels"] for t in p["targets"]]
    assert len(exprs) >= 20
    exposed = exposed_names()
    # "# TYPE x_total counter" lists the counter as x (without _total) in some versions.
    known = exposed | {n + "_total" for n in exposed}
    missing = set()
    for expr in exprs:
        for name in _METRIC.findall(expr):
            if name not in known and name.removesuffix("_total") not in known:
                missing.add(name)
    assert not missing, f"dashboard queries unknown metrics: {sorted(missing)}"


def test_dashboard_is_provisioned_with_unique_panel_ids() -> None:
    dashboard = json.loads(DASHBOARD.read_text())
    ids = [p["id"] for p in dashboard["panels"]]
    assert len(ids) == len(set(ids)) and dashboard["uid"] == "workflow-platform"


def test_compose_profiles_keep_the_core_stack_small() -> None:
    compose = yaml.safe_load((ROOT / "docker-compose.yml").read_text())
    services = compose["services"]
    for optional in ("prometheus", "grafana", "worker", "tests"):
        assert services[optional]["profiles"], optional
    assert "profiles" not in services["api"] and "profiles" not in services["db"]
    prometheus = yaml.safe_load((ROOT / "deploy/prometheus/prometheus.yml").read_text())
    assert {j["job_name"] for j in prometheus["scrape_configs"]} == {"api", "workers"}
