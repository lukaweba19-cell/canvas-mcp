"""A minimal but real Rails 7 app tree, zipped the way a course ships it."""

from __future__ import annotations

import io
import zipfile

ROUTES = '''Rails.application.routes.draw do
  root "home#index"

  resources :courses do
    resources :lessons, only: [:index, :show]

    member do
      post :publish
    end

    collection do
      get :search
    end
  end

  namespace :admin do
    resources :users
    get "reports", to: "reports#index"
  end

  scope "/api" do
    resources :grades, only: [:index]
  end

  get "/health", to: "health#show"
  mount Sidekiq::Web => "/sidekiq"
end
'''

SCHEMA = '''ActiveRecord::Schema[7.1].define(version: 2024_01_02_000000) do
  create_table "courses", force: :cascade do |t|
    t.string "title", null: false
    t.text "description"
    t.datetime "created_at", null: false
    t.timestamps
    t.index ["title"], name: "index_courses_on_title", unique: true
  end

  create_table "lessons", force: :cascade do |t|
    t.integer "course_id", null: false
    t.string "name"
    t.timestamps
    t.index ["course_id"], name: "index_lessons_on_course_id"
  end
end
'''

GEMFILE = '''source "https://rubygems.org"

gem "rails", "~> 7.1.3"
gem "sqlite3"
'''

GEMFILE_LOCK = '''GEM
  remote: https://rubygems.org/
  specs:
    rails (7.1.3)
      actioncable (= 7.1.3)
      activerecord (= 7.1.3)
    sqlite3 (1.7.3)

PLATFORMS
  x86_64-linux

DEPENDENCIES
  rails (~> 7.1.3)
  sqlite3
'''

APPLICATION_RB = '''require_relative "boot"
require "rails/all"

module StudyApp
  class Application < Rails::Application
    config.load_defaults 7.1
  end
end
'''

DATABASE_YML = '''default: &default
  adapter: sqlite3
  pool: 5

development:
  <<: *default
  database: db/development.sqlite3
'''

COURSES_CONTROLLER = '''class CoursesController < ApplicationController
  before_action :set_course, only: [:show, :edit]

  def index
  end

  def show
  end

  def publish
    @course.publish!
  end

  private

  def set_course
    @course = Course.find(params[:id])
  end
end
'''

ADMIN_USERS_CONTROLLER = '''class Admin::UsersController < ApplicationController
  def index
  end

  def destroy
  end
end
'''

COURSE_MODEL = '''class Course < ApplicationRecord
  has_many :lessons, dependent: :destroy
  has_many :enrollments

  validates :title, presence: true, length: { maximum: 120 }
  validates :seats, numericality: { greater_than: 0 }

  scope :published, -> { where(published: true) }
end
'''

LESSON_MODEL = '''class Lesson < ApplicationRecord
  belongs_to :course
  has_one :quiz

  validates :name, presence: true
end
'''


def rails_app_files() -> dict[str, str]:
    """Every file a course would ship for this app, keyed by relative path."""
    return {
        "Gemfile": GEMFILE,
        "Gemfile.lock": GEMFILE_LOCK,
        "config/application.rb": APPLICATION_RB,
        "config/routes.rb": ROUTES,
        "config/database.yml": DATABASE_YML,
        "app/controllers/courses_controller.rb": COURSES_CONTROLLER,
        "app/controllers/admin/users_controller.rb": ADMIN_USERS_CONTROLLER,
        "app/models/course.rb": COURSE_MODEL,
        "app/models/lesson.rb": LESSON_MODEL,
        "app/views/courses/index.html.erb": "<h1>Courses</h1>\n",
        "app/views/courses/show.html.erb": "<h1><%= @course.title %></h1>\n",
        "app/views/layouts/application.html.erb": "<html><body><%= yield %></body></html>\n",
        "db/schema.rb": SCHEMA,
        "db/migrate/20240101000000_create_courses.rb": (
            "class CreateCourses < ActiveRecord::Migration[7.1]\n"
            "  def change\n"
            '    create_table :courses do |t|\n'
            '      t.string :title\n'
            "      t.timestamps\n"
            "    end\n"
            "  end\n"
            "end\n"
        ),
        "db/migrate/20240102000000_create_lessons.rb": (
            "class CreateLessons < ActiveRecord::Migration[7.1]\n"
            "  def change\n"
            '    create_table :lessons do |t|\n'
            '      t.references :course, foreign_key: true\n'
            '      t.string :name\n'
            "      t.timestamps\n"
            "    end\n"
            "  end\n"
            "end\n"
        ),
    }


def build_rails_zip(
    files: dict[str, str] | None = None, root: str | None = "study-app"
) -> bytes:
    """Zip an app tree; ``root`` mirrors the single folder real zips contain."""
    tree = rails_app_files() if files is None else files
    buffer = io.BytesIO()
    with zipfile.ZipFile(buffer, "w", zipfile.ZIP_DEFLATED) as archive:
        for path, content in tree.items():
            archive.writestr(f"{root}/{path}" if root else path, content)
    return buffer.getvalue()
