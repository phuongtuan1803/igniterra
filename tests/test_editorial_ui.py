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
    for name in (
        "base.html",
        "tasks.html",
        "series_list.html",
        "series.html",
        "storyboard.html",
        "detail.html",
        "settings.html",
        "_status.html",
        "_macros.html",
    ):
        assert env.get_template(name)


def test_editorial_navigation_and_form_contract(env):
    client = TestClient(create_app(env.config), follow_redirects=False)

    tasks = client.get("/")
    assert tasks.status_code == 200
    assert "Your production desk" in tasks.text
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
