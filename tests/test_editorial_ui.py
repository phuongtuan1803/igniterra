"""UI contract tests for the Editorial Light server-rendered studio.

These checks assert rendering and preserved form names/routes, not Figma pixel matching.
"""
from pathlib import Path

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
    assert "The worlds you return to." in series.text
    assert 'action="/series"' in series.text
    assert 'name="from_task"' in series.text

    settings = client.get("/settings")
    assert settings.status_code == 200
    assert "Your tools. Your process." in settings.text
    assert 'action="/settings"' in settings.text
    assert 'name="gate_keyframes"' in settings.text
    assert 'name="llm_model"' in settings.text
    assert 'name="image_model"' in settings.text


def test_task_search_and_filters_preserve_workspace_totals(env):
    first = env.new_task()
    second = env.new_task(name="brief-01")
    env.store.set_status(first, "waiting", "paused")
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
