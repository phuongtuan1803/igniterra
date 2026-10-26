"""UI contract tests for the approved server-rendered studio.

These checks assert rendering and preserved form names/routes, not Figma pixel matching.
"""
from pathlib import Path
from urllib.parse import parse_qs, urlsplit

import pytest

from fastapi.testclient import TestClient
from jinja2 import Environment, FileSystemLoader, select_autoescape

from studio.web import create_app


TEMPLATE_DIR = Path(__file__).resolve().parents[1] / "studio" / "web" / "templates"


def test_web_templates_compile():
    env = Environment(
        loader=FileSystemLoader(TEMPLATE_DIR),
        autoescape=select_autoescape(["html"]),
    )
    for name in env.list_templates():
        assert env.get_template(name)


def test_editorial_navigation_and_form_contract(env):
    client = TestClient(create_app(env.config), follow_redirects=False)

    tasks = client.get("/")
    assert tasks.status_code == 200
    assert "Không bỏ lỡ bước quan trọng" in tasks.text
    assert 'class="app-sidebar"' in tasks.text
    for field in ('name="brief_file"', 'name="brief_json"', 'name="series"', 'name="overrides"'):
        assert field in tasks.text
    assert 'action="/tasks"' in tasks.text

    series = client.get("/series")
    assert series.status_code == 200
    assert "Nhất quán từ scene đầu đến cuối" in series.text
    assert 'action="/series"' in series.text
    assert 'name="from_task"' in series.text

    settings = client.get("/settings")
    assert settings.status_code == 200
    assert "Công cụ và quy trình của bạn" in settings.text
    assert 'action="/settings"' in settings.text
    assert 'name="gate_keyframes"' in settings.text
    assert 'name="llm_model"' in settings.text
    assert 'name="image_model"' in settings.text


def test_task_search_and_filters_preserve_workspace_totals(env):
    first = env.new_task()
    second = env.new_task(name="brief-01")
    env.store.set_status(first, "waiting", "paused")
    env.store.update_task(first, series="search-series")
    client = TestClient(create_app(env.config))
    filtered = client.get("/", params={"status": "review", "q": first}).text
    assert f'href="/tasks/{first}"' in filtered
    assert f'href="/tasks/{second}"' not in filtered
    assert "1 / 2 task" in filtered
    assert 'value="review"' in filtered
    empty = client.get("/", params={"q": "<script>alert(1)</script>"}).text
    assert "Không tìm thấy task phù hợp." in empty
    assert '<script>alert(1)</script>' not in empty
    assert "&lt;script&gt;alert(1)&lt;/script&gt;" in empty
    assert "2 / 2 task" in client.get("/?status=invalid").text
    by_series = client.get("/", params={"q": "search-series"}).text
    assert "1 / 2 task" in by_series and "search-series" in by_series
    assert f'href="/tasks/{second}"' not in by_series


def test_monitor_task_attention_filter_and_refresh_controls(env):
    task_id = env.new_task()
    other = env.new_task(name="brief-01")
    env.store.set_status(task_id, "waiting", "paused")
    client = TestClient(create_app(env.config))
    page = client.get("/monitor", params={"task": task_id, "refresh": "off"}).text
    assert f'href="/tasks/{task_id}"' in page
    assert f'href="/tasks/{other}"' not in page
    assert '<meta http-equiv="refresh"' not in page
    assert f'task={task_id}&amp;refresh=off' in page
    assert '<meta http-equiv="refresh" content="5">' in client.get("/monitor").text


@pytest.mark.parametrize("htmx", [False, True])
def test_scene_actions_keep_scene_and_feedback_in_redirect(env, htmx):
    from studio.contracts import ScenePlan

    task_id = env.new_task()
    plan = ScenePlan(title="Redirect contract", scenes=[{
        "id": "s01", "narration": "Test narration", "visual": "Initial visual",
        "shot": {"size": "wide", "angle": "eye_level", "movement": "static"},
    }])
    env.store.update_task(task_id, plan_rev=env.td(task_id).write_plan(plan))
    client = TestClient(create_app(env.config), follow_redirects=False)
    headers = {"HX-Request": "true"} if htmx else {}
    response = client.post(f"/tasks/{task_id}/scenes/s01/edit", data={"visual": "Changed visual"}, headers=headers)
    assert response.status_code == (204 if htmx else 303)
    target = response.headers["hx-redirect" if htmx else "location"]
    query = parse_qs(urlsplit(target).query)
    assert query["scene"] == ["s01"] and query["msg"] == ["ScenePlan rev 2"]
    page = client.get(target).text
    assert 'class="scene active"' in page and "Changed visual" in page
    response = client.post(f"/tasks/{task_id}/scenes/s01/select", data={"candidate": "missing"}, headers=headers)
    target = response.headers["hx-redirect" if htmx else "location"]
    query = parse_qs(urlsplit(target).query)
    assert query["scene"] == ["s01"] and query["err"]
    assert 'role="alert"' in client.get(target).text
