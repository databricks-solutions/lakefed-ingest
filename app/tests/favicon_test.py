"""The browser tab icon: the Inflow logo, a 16 px drawing of its own and a 32 px one, kept in frontend/public so the
build copies them to the top of static/, linked from index.html, and handed out by the app's catch-all route."""
from pathlib import Path

import pytest

FRONTEND = Path(__file__).resolve().parents[1] / "src" / "frontend"
INDEX = (FRONTEND / "index.html").read_text()
ICONS = ("favicon-16.svg", "16x16"), ("favicon-32.svg", "32x32")


def test_index_links_both_icons_and_the_files_are_there():
    for name, size in ICONS:
        assert f'<link rel="icon" type="image/svg+xml" sizes="{size}" href="/{name}" />' in INDEX
        drawing = (FRONTEND / "public" / name).read_text()
        assert drawing.startswith("<svg") and "<script" not in drawing


def test_the_app_serves_an_icon_from_the_built_static_folder(tmp_path, monkeypatch):
    pytest.importorskip("fastapi")
    import main

    (tmp_path / "favicon-16.svg").write_text((FRONTEND / "public" / "favicon-16.svg").read_text())
    (tmp_path / "index.html").write_text(INDEX)
    monkeypatch.setattr(main, "STATIC", tmp_path)
    response = main.spa("favicon-16.svg")
    assert Path(response.path) == tmp_path / "favicon-16.svg" and response.media_type == "image/svg+xml"
