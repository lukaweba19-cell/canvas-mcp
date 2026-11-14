"""A course's Rails app zip reads as a Rails project, not as an archive."""

from __future__ import annotations

from helpers import call
from rails_app_fixture import build_rails_zip, rails_app_files

from canvas_mcp.core import extract

APP = build_rails_zip()


def _sections():
    result = extract.extract_bytes("study-app.zip", APP)
    assert result.status.readable, result.warnings
    return {section.label: section.text for section in result.sections}


def test_rails_zip_is_recognized_and_described():
    result = extract.extract_bytes("study-app.zip", APP)
    sections = {section.label: section.text for section in result.sections}

    assert list(sections)[0] == "Rails project"
    overview = sections["Rails project"]
    assert "study-app" in overview
    assert "Rails: 7.1.3" in overview
    assert "Database adapter: sqlite3" in overview
    assert result.status.readable


def test_routes_cover_resources_nesting_namespace_scope_and_members():
    routes = _sections()["Routes"]

    assert "GET / -> home#index" in routes
    assert "GET /courses -> courses#index" in routes
    assert "GET /courses/:id -> courses#show" in routes
    assert "GET /courses/:course_id/lessons -> lessons#index" in routes
    assert "POST /courses/:id/publish -> courses#publish" in routes
    assert "GET /courses/search -> courses#search" in routes
    assert "GET /admin/users -> admin/users#index" in routes
    assert "GET /admin/reports -> reports#index" in routes
    assert "GET /api/grades -> grades#index" in routes
    assert "GET /health -> health#show" in routes
    assert "MOUNT /sidekiq -> Sidekiq::Web" in routes
    # only: [:index, :show] must not invent create/destroy for lessons.
    assert "lessons#create" not in routes
    assert "lessons#destroy" not in routes


def test_controllers_list_public_actions_only():
    controllers = _sections()["Controllers"]

    assert "CoursesController: index, show, publish" in controllers
    assert "Admin::UsersController: index, destroy" in controllers
    assert "set_course" not in controllers, "private actions are not public routes"


def test_models_show_associations_and_validations():
    models = _sections()["Models"]

    assert "Course: has_many lessons" in models
    assert "has_many enrollments" in models
    assert "validates title" in models
    assert "presence" in models
    assert "Lesson: belongs_to course" in models
    assert "has_one quiz" in models


def test_schema_tables_columns_and_indexes():
    schema = _sections()["Schema"]

    assert "courses: title:string" in schema
    assert "description:text" in schema
    assert "timestamps" in schema
    assert "indexes: index_courses_on_title unique" in schema
    assert "lessons: course_id:integer" in schema
    assert "index_lessons_on_course_id" in schema


def test_views_layouts_and_migrations_are_listed():
    sections = _sections()

    assert sections["Views"].splitlines() == [
        "courses: index, show",
        "layouts: application",
    ]
    assert sections["Migrations"].startswith("2 migration(s):")
    assert "20240101000000_create_courses.rb" in sections["Migrations"]


def test_partial_app_degrades_honestly():
    files = {
        "Gemfile": rails_app_files()["Gemfile"],
        "config/application.rb": rails_app_files()["config/application.rb"],
    }
    result = extract.extract_bytes("partial.zip", build_rails_zip(files, root=None))
    sections = {section.label: section.text for section in result.sections}

    assert result.status.readable
    assert "Not in the archive: config/routes.rb, db/schema.rb" in sections["Rails project"]
    assert "Routes" not in sections
    assert "Schema" not in sections
    assert "Partial app" in sections
    assert "config/routes.rb" in sections["Partial app"]


def test_non_rails_zip_is_refused_with_the_reason():
    import io
    import zipfile

    buffer = io.BytesIO()
    with zipfile.ZipFile(buffer, "w") as archive:
        archive.writestr("notes.txt", "just a text file")

    result = extract.extract_bytes("photos.zip", buffer.getvalue())

    status = result.status
    assert not status.readable
    assert status.reason == "unsupported"
    assert "no Rails project" in status.label()
    assert "empty" not in status.label().lower()


async def test_rails_zip_reads_through_the_real_tool(canvas, monkeypatch, mcp):
    canvas.route("get", "/courses/231", {"id": 231, "course_code": "CS231", "name": "Web"})
    canvas.route(
        "get",
        "/courses/231/files",
        [{"id": 407, "display_name": "study-app.zip", "filename": "study-app.zip", "size": len(APP)}],
    )
    canvas.route(
        "get",
        "/courses/231/files/407",
        {
            "id": 407,
            "display_name": "study-app.zip",
            "filename": "study-app.zip",
            "url": "https://files.example/407",
        },
    )

    async def download(url: str):
        return APP, None

    from canvas_mcp.tools import materials as materials_module

    monkeypatch.setattr(materials_module, "_download_course_file_bytes", download)

    read = await call(
        mcp, "read_course_material", course_identifier=231, file_id=407, full_text=True
    )
    assert "Status: readable — " in read, read
    assert "## Rails project" in read
    assert "## Routes" in read
    assert "GET /courses -> courses#index" in read

    cut = await call(
        mcp,
        "read_course_material",
        course_identifier=231,
        file_id=407,
        full_text=True,
        max_chars=1200,
    )
    assert "more section(s) not shown" in cut
    assert "raise max_chars" in cut
