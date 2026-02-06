"""
RealTime Prague Tour 

Run locally (dev):
  python realtimepythonweb.py 

Production (example):
  export SECRET_KEY="..." DATABASE_URL="postgresql+psycopg2://user:pass@127.0.0.1:5432/db"
  gunicorn -w 2 -k gthread -t 60 -b 0.0.0.0:5000 realtimepythonweb:app

NOTE:
- If you already created a SQLite db file with the old schema, you should delete it (prague_tour.db)
  or switch to Postgres and start fresh, because create_all() won’t auto-migrate schemas.
"""

import os
import random
from datetime import datetime
from pathlib import Path

from flask import (
    Flask, request, render_template_string, redirect, url_for,
    session, flash, abort, send_from_directory
)
from flask_sqlalchemy import SQLAlchemy
from werkzeug.utils import secure_filename


# ==========================================
# CONFIG
# ==========================================

app = Flask(__name__)

# Use env vars in production
app.config["SECRET_KEY"] = os.environ.get("SECRET_KEY") or os.urandom(32)

# Prefer Postgres on Hetzner:
#   export DATABASE_URL="postgresql+psycopg2://user:pass@127.0.0.1:5432/prague_tour"
app.config["SQLALCHEMY_DATABASE_URI"] = os.environ.get(
    "DATABASE_URL",
    "sqlite:///prague_tour.db"
)
app.config["SQLALCHEMY_TRACK_MODIFICATIONS"] = False

# Uploads (photos)
UPLOAD_DIR = Path(os.environ.get("UPLOAD_DIR", "./uploads")).resolve()
UPLOAD_DIR.mkdir(parents=True, exist_ok=True)
app.config["MAX_CONTENT_LENGTH"] = 10 * 1024 * 1024  # 10MB per upload

# Simple allowed image types
ALLOWED_EXTS = {".jpg", ".jpeg", ".png", ".webp"}

db = SQLAlchemy(app)


# ==========================================
# DATA MODELS
# ==========================================

class User(db.Model):
    id = db.Column(db.Integer, primary_key=True)
    name = db.Column(db.String(100), nullable=True)
    code = db.Column(db.String(50), unique=True, nullable=False)  # Login code
    is_admin = db.Column(db.Boolean, default=False)
    team_id = db.Column(db.Integer, db.ForeignKey("team.id"), nullable=True)

    team = db.relationship("Team", backref="members")


class Team(db.Model):
    id = db.Column(db.Integer, primary_key=True)
    name = db.Column(db.String(100), nullable=False)

    score = db.Column(db.Integer, default=0)

    # Route-based progression
    route_id = db.Column(db.Integer, db.ForeignKey("route.id"), nullable=True)
    route_step_index = db.Column(db.Integer, default=0)

    # Convenience pointer (what to show on player dashboard)
    current_poi_id = db.Column(db.Integer, db.ForeignKey("poi.id"), nullable=True)
    is_finished = db.Column(db.Boolean, default=False)

    current_poi = db.relationship("POI", foreign_keys=[current_poi_id])
    route = db.relationship("Route")

    progress = db.relationship("TeamPOIProgress", backref="team", lazy="dynamic")


class POI(db.Model):
    id = db.Column(db.Integer, primary_key=True)
    title = db.Column(db.String(200), nullable=False)  # Internal
    riddle = db.Column(db.Text, nullable=False)

    hint_1 = db.Column(db.String(255))
    hint_2 = db.Column(db.String(255))
    hint_3 = db.Column(db.String(255))

    # 'photo' or 'text'
    completion_type = db.Column(db.String(20), default="photo")
    answer_key = db.Column(db.String(200), nullable=True)  # For text answers

    points = db.Column(db.Integer, default=10)
    difficulty = db.Column(db.String(20), default="medium")


class Route(db.Model):
    id = db.Column(db.Integer, primary_key=True)
    name = db.Column(db.String(120), nullable=False)


class RouteStep(db.Model):
    id = db.Column(db.Integer, primary_key=True)
    route_id = db.Column(db.Integer, db.ForeignKey("route.id"), nullable=False)
    poi_id = db.Column(db.Integer, db.ForeignKey("poi.id"), nullable=False)
    step_index = db.Column(db.Integer, nullable=False)  # 0..N

    route = db.relationship("Route", backref="steps")
    poi = db.relationship("POI")


class TeamPOIProgress(db.Model):
    id = db.Column(db.Integer, primary_key=True)
    team_id = db.Column(db.Integer, db.ForeignKey("team.id"), nullable=False)
    poi_id = db.Column(db.Integer, db.ForeignKey("poi.id"), nullable=False)

    status = db.Column(db.String(20), default="assigned")  # assigned, completed
    hints_used = db.Column(db.Integer, default=0)
    completed_at = db.Column(db.DateTime, nullable=True)

    poi = db.relationship("POI")


class Submission(db.Model):
    id = db.Column(db.Integer, primary_key=True)
    team_id = db.Column(db.Integer, db.ForeignKey("team.id"), nullable=False)
    poi_id = db.Column(db.Integer, db.ForeignKey("poi.id"), nullable=False)

    type = db.Column(db.String(20))  # photo, text

    # For photo: store filename (relative) on disk
    # For text: store answer
    content = db.Column(db.Text)

    # We keep status for logging; photos/text are auto-approved in this version.
    status = db.Column(db.String(20), default="approved")  # approved, rejected
    timestamp = db.Column(db.DateTime, default=datetime.utcnow)

    team = db.relationship("Team")
    poi = db.relationship("POI")


# ==========================================
# HTML TEMPLATES (Tailwind CSS)
# ==========================================

BASE_LAYOUT = """
<!DOCTYPE html>
<html lang="en">
<head>
    <meta charset="UTF-8">
    <meta name="viewport" content="width=device-width, initial-scale=1.0">
    <title>RealTime Prague Tour</title>
    <script src="https://cdn.tailwindcss.com"></script>
    <link href="https://cdnjs.cloudflare.com/ajax/libs/font-awesome/6.0.0/css/all.min.css" rel="stylesheet">
    <style>
        .fade-in { animation: fadeIn 0.5s ease-in; }
        @keyframes fadeIn { from { opacity: 0; } to { opacity: 1; } }
    </style>
</head>
<body class="bg-gray-900 text-gray-100 min-h-screen font-sans">
    <nav class="bg-gray-800 border-b border-gray-700 p-4 sticky top-0 z-50">
        <div class="container mx-auto flex justify-between items-center">
            <a href="/" class="text-xl font-bold text-yellow-500"><i class="fas fa-map-marked-alt mr-2"></i>Prague Tour</a>
            <div class="flex items-center gap-3">
                {% if session.get('user_id') %}
                    {% if session.get('is_admin') %}
                        <a href="/dashboard" class="text-sm text-gray-300 hover:text-white">Admin</a>
                    {% endif %}
                    <a href="/logout" class="text-sm text-gray-400 hover:text-white">Logout</a>
                {% endif %}
            </div>
        </div>
    </nav>

    <main class="container mx-auto p-4 pb-20">
        {% with messages = get_flashed_messages(with_categories=true) %}
          {% if messages %}
            {% for category, message in messages %}
              <div class="mb-4 p-4 rounded {{ 'bg-green-600' if category == 'success' else 'bg-red-600' }} text-white shadow-lg">
                {{ message }}
              </div>
            {% endfor %}
          {% endif %}
        {% endwith %}

        {% block content %}{% endblock %}
    </main>
</body>
</html>
"""

LOGIN_TEMPLATE = """
{% extends "base" %}
{% block content %}
<div class="flex items-center justify-center min-h-[60vh]">
    <div class="bg-gray-800 p-8 rounded-2xl shadow-xl w-full max-w-md border border-gray-700">
        <h2 class="text-2xl font-bold mb-6 text-center text-white">Prague City Tour</h2>
        <form method="POST">
            <label class="block text-gray-400 text-sm font-bold mb-2">Member Code / Admin Code</label>
            <input type="text" name="code" class="w-full p-3 rounded bg-gray-700 text-white border border-gray-600 focus:outline-none focus:border-yellow-500 mb-6 uppercase" placeholder="ENTER CODE" required>
            <button type="submit" class="w-full bg-yellow-500 hover:bg-yellow-600 text-gray-900 font-bold py-3 px-4 rounded transition duration-200">
                Enter App
            </button>
        </form>
        <p class="mt-4 text-xs text-center text-gray-500">Tip: admin code is ADMIN (change/remove in setup())</p>
    </div>
</div>
{% endblock %}
"""

TEAM_WAITING = """
{% extends "base" %}
{% block content %}
<div class="flex flex-col items-center justify-center min-h-[60vh] text-center">
    <div class="bg-gray-800 p-8 rounded-2xl shadow-xl max-w-sm border border-gray-700">
        <i class="fas fa-users text-5xl text-gray-600 mb-6"></i>
        <h2 class="text-2xl font-bold mb-4">Welcome, {{ user.code }}</h2>
        <p class="text-gray-400 mb-6">The organizers have not assigned teams yet. Please wait for the event to start.</p>
        <a href="/" class="text-blue-400 hover:underline"><i class="fas fa-sync mr-1"></i> Refresh Status</a>
    </div>
</div>
{% endblock %}
"""

PLAYER_DASHBOARD = """
{% extends "base" %}
{% block content %}

<!-- Header Stats -->
<div class="flex justify-between items-center mb-6 bg-gray-800 p-4 rounded-xl border border-gray-700">
    <div>
        <h1 class="text-lg font-bold text-white">{{ team.name }}</h1>
        <p class="text-xs text-gray-400">
            {% for m in team.members %}{{ m.name if m.name else m.code }}{{ ", " if not loop.last }}{% endfor %}
        </p>
        <p class="text-xs text-gray-500 mt-1">
            Route: {{ team.route.name if team.route else "Not assigned" }}
        </p>
    </div>
    <div class="text-right">
        <p class="text-xs text-gray-400 uppercase tracking-wider">Score</p>
        <p class="text-2xl font-bold text-yellow-500">{{ team.score }}</p>
    </div>
</div>

{% if not current_poi and team.is_finished %}
    <!-- Finished State -->
    <div class="text-center py-10 fade-in">
        <i class="fas fa-trophy text-6xl text-yellow-500 mb-4"></i>
        <h2 class="text-3xl font-bold text-white mb-2">Course Complete!</h2>
        <p class="text-gray-400">You have completed your route.</p>
        <div class="mt-8 bg-gray-800 p-6 rounded-xl border border-gray-700">
            <p class="text-xl">Final Score: <span class="text-yellow-500 font-bold">{{ team.score }}</span></p>
        </div>
    </div>
{% elif not current_poi %}
    <!-- Waiting State -->
    <div class="text-center py-10 fade-in">
        <i class="fas fa-hourglass-half text-5xl text-blue-400 mb-4 animate-pulse"></i>
        <h2 class="text-xl font-bold text-white mb-2">Waiting for mission.</h2>
        <p class="text-gray-400 text-sm">If this persists, ask an organizer to assign routes + generate teams.</p>
    </div>
{% else %}
    <!-- Current Mission -->
    <div class="grid grid-cols-1 lg:grid-cols-3 gap-6 fade-in">
        <!-- Mission Card -->
        <div class="lg:col-span-2 bg-gray-800 rounded-2xl border border-gray-700 shadow-xl overflow-hidden">
            <div class="p-6 border-b border-gray-700">
                <div class="flex justify-between items-center">
                    <h2 class="text-xl font-bold text-white">Current Challenge</h2>
                    <span class="px-3 py-1 rounded-full text-xs bg-gray-700 text-gray-300 uppercase">
                        {{ current_poi.difficulty }}
                    </span>
                </div>
            </div>
            <div class="p-6">
                <p class="text-gray-200 text-lg leading-relaxed mb-6">
                    {{ current_poi.riddle }}
                </p>

                <!-- Hints -->
                <div class="bg-gray-900 p-4 rounded-xl border border-gray-700 mb-6">
                    <h3 class="text-sm font-bold text-gray-300 mb-2">
                        Hints Used: {{ progress.hints_used if progress else 0 }}/3
                        <span class="text-xs text-gray-500">(each hint costs 2 points)</span>
                    </h3>
                    <div class="space-y-2 text-sm">
                        {% if progress and progress.hints_used >= 1 and current_poi.hint_1 %}
                            <div class="p-3 bg-gray-800 rounded border border-gray-700">
                                <span class="text-yellow-400 font-bold">Hint 1:</span> {{ current_poi.hint_1 }}
                            </div>
                        {% endif %}
                        {% if progress and progress.hints_used >= 2 and current_poi.hint_2 %}
                            <div class="p-3 bg-gray-800 rounded border border-gray-700">
                                <span class="text-yellow-400 font-bold">Hint 2:</span> {{ current_poi.hint_2 }}
                            </div>
                        {% endif %}
                        {% if progress and progress.hints_used >= 3 and current_poi.hint_3 %}
                            <div class="p-3 bg-gray-800 rounded border border-gray-700">
                                <span class="text-yellow-400 font-bold">Hint 3:</span> {{ current_poi.hint_3 }}
                            </div>
                        {% endif %}
                    </div>

                    {% if progress and progress.hints_used < 3 %}
                    <form method="POST" action="/action/hint" class="mt-4">
                        <button type="submit" class="w-full bg-blue-600 hover:bg-blue-700 text-white font-bold py-2 rounded-lg transition">
                            <i class="fas fa-lightbulb mr-2"></i>Request Next Hint
                        </button>
                    </form>
                    {% endif %}
                </div>

                <!-- Proof submission -->
                <div class="bg-gray-900 p-4 rounded-xl border border-gray-700">
                    <h3 class="text-sm font-bold text-gray-300 mb-3">Complete Challenge</h3>
                    <form method="POST" action="/action/submit" enctype="multipart/form-data">
                        {% if current_poi.completion_type == 'photo' %}
                            <label class="block w-full cursor-pointer bg-gray-800 border border-gray-600 rounded p-3 text-gray-200 mb-3 hover:border-yellow-500">
                                <input type="file" name="proof_file" accept="image/*" class="hidden"
                                       onchange="document.getElementById('file-name').innerText = this.files[0].name" required>
                                <i class="fas fa-camera mr-2 text-yellow-400"></i> Upload a photo (auto-accepted)
                                <p id="file-name" class="text-xs text-blue-400 mt-2"></p>
                            </label>
                        {% else %}
                            <input type="text" name="proof_text" placeholder="Type the answer found at location."
                                   class="w-full bg-gray-800 border border-gray-600 rounded p-3 text-white mb-3 focus:border-yellow-500 outline-none" required>
                        {% endif %}

                        <button type="submit" class="w-full bg-yellow-500 hover:bg-yellow-600 text-gray-900 font-bold py-3 rounded-lg shadow-lg transition transform active:scale-95">
                            Complete Challenge
                        </button>
                    </form>
                </div>

            </div>
        </div>

        <!-- Progress Sidebar -->
        <div class="bg-gray-800 rounded-2xl border border-gray-700 shadow-xl p-6">
            <h3 class="text-lg font-bold mb-4">Progress</h3>
            <div class="space-y-3">
                {% for item in completed %}
                <div class="p-3 bg-gray-900 rounded border border-gray-700">
                    <p class="text-sm font-bold text-green-400"><i class="fas fa-check mr-1"></i> {{ item.poi.title }}</p>
                    <p class="text-xs text-gray-500">Hints used: {{ item.hints_used }}</p>
                </div>
                {% endfor %}
                {% if completed|length == 0 %}
                    <p class="text-sm text-gray-400">No completed locations yet.</p>
                {% endif %}
            </div>
        </div>
    </div>
{% endif %}

{% endblock %}
"""

ADMIN_DASHBOARD = """
{% extends "base" %}
{% block content %}

<div class="grid grid-cols-1 md:grid-cols-2 lg:grid-cols-4 gap-4 mb-8">
    <div class="bg-gray-800 p-4 rounded-xl border border-gray-700">
        <h3 class="text-gray-400 text-sm">Total Teams</h3>
        <p class="text-2xl font-bold">{{ teams|length }}</p>
    </div>
    <div class="bg-gray-800 p-4 rounded-xl border border-gray-700">
        <h3 class="text-gray-400 text-sm">Active Users</h3>
        <p class="text-2xl font-bold">{{ users_count }}</p>
    </div>
    <div class="bg-gray-800 p-4 rounded-xl border border-gray-700">
        <h3 class="text-gray-400 text-sm">Routes</h3>
        <p class="text-2xl font-bold">{{ routes_count }}</p>
    </div>
</div>

<!-- Controls -->
<div class="flex flex-wrap gap-4 mb-8">
    <a href="{{ url_for('admin_generate_teams') }}"
       onclick="return confirm('This assigns teams and routes to unassigned users. Continue?')"
       class="bg-blue-600 hover:bg-blue-700 text-white px-4 py-2 rounded">
        <i class="fas fa-users mr-2"></i>Generate Teams
    </a>
    <a href="{{ url_for('admin_create_poi') }}" class="bg-green-600 hover:bg-green-700 text-white px-4 py-2 rounded">
        <i class="fas fa-plus mr-2"></i>Add POI
    </a>
    <a href="{{ url_for('admin_create_route') }}" class="bg-indigo-600 hover:bg-indigo-700 text-white px-4 py-2 rounded">
        <i class="fas fa-route mr-2"></i>Add Route
    </a>
    <a href="{{ url_for('admin_create_route_step') }}" class="bg-cyan-600 hover:bg-cyan-700 text-white px-4 py-2 rounded">
        <i class="fas fa-list-ol mr-2"></i>Add Route Step
    </a>
    <a href="{{ url_for('admin_create_user') }}" class="bg-purple-600 hover:bg-purple-700 text-white px-4 py-2 rounded">
        <i class="fas fa-user-plus mr-2"></i>Add User
    </a>
</div>

<!-- Recent Photo Feed -->
<h2 class="text-xl font-bold mb-4 border-b border-gray-700 pb-2">Recent Photos</h2>
{% if photos %}
<div class="grid grid-cols-1 md:grid-cols-2 lg:grid-cols-3 gap-6 mb-10">
    {% for sub in photos %}
    <div class="bg-gray-800 rounded-xl overflow-hidden border border-gray-600 shadow-lg">
        <img src="{{ url_for('media', sub_id=sub.id) }}" class="w-full h-52 object-cover">
        <div class="p-4">
            <h4 class="font-bold text-yellow-500">{{ sub.team.name }}</h4>
            <p class="text-sm text-gray-200">{{ sub.poi.title }}</p>
            <p class="text-xs text-gray-500 mt-1">{{ sub.timestamp.strftime('%Y-%m-%d %H:%M:%S') }} UTC</p>
        </div>
    </div>
    {% endfor %}
</div>
{% else %}
<p class="text-gray-400 mb-10">No photo submissions yet.</p>
{% endif %}

<!-- Live Standings -->
<h2 class="text-xl font-bold mb-4 border-b border-gray-700 pb-2">Live Standings</h2>
<div class="overflow-x-auto">
    <table class="w-full text-left text-gray-300">
        <thead class="bg-gray-700 text-gray-100 uppercase text-xs">
            <tr>
                <th class="p-3">Team</th>
                <th class="p-3">Score</th>
                <th class="p-3">Route</th>
                <th class="p-3">Current POI</th>
                <th class="p-3">Status</th>
            </tr>
        </thead>
        <tbody>
            {% for team in teams %}
            <tr class="border-b border-gray-700 hover:bg-gray-800">
                <td class="p-3 font-bold">
                    {{ team.name }}
                    <span class="text-xs text-gray-500 block">{{ team.members|length }} members</span>
                </td>
                <td class="p-3 text-yellow-500 font-bold">{{ team.score }}</td>
                <td class="p-3">{{ team.route.name if team.route else "-" }}</td>
                <td class="p-3">{{ team.current_poi.title if team.current_poi else '-' }}</td>
                <td class="p-3">
                    {% if team.is_finished %}
                        <span class="px-2 py-1 bg-green-900 text-green-300 rounded text-xs">Finished</span>
                    {% elif team.current_poi %}
                        <span class="px-2 py-1 bg-blue-900 text-blue-300 rounded text-xs">Active</span>
                    {% else %}
                        <span class="px-2 py-1 bg-gray-600 text-gray-300 rounded text-xs">Waiting</span>
                    {% endif %}
                </td>
            </tr>
            {% endfor %}
        </tbody>
    </table>
</div>

{% endblock %}
"""

ADMIN_FORMS = """
{% extends "base" %}
{% block content %}
<div class="max-w-lg mx-auto bg-gray-800 p-6 rounded-xl border border-gray-700">
    <h2 class="text-xl font-bold mb-4">{{ title }}</h2>

    <form method="POST">
        {% if form_type == 'poi' %}
            <div class="mb-4">
                <label class="block text-xs text-gray-400 uppercase">Internal Title</label>
                <input name="title" class="w-full bg-gray-700 border border-gray-600 p-2 rounded text-white" required>
            </div>
            <div class="mb-4">
                <label class="block text-xs text-gray-400 uppercase">Riddle</label>
                <textarea name="riddle" class="w-full bg-gray-700 border border-gray-600 p-2 rounded text-white" rows="3" required></textarea>
            </div>
            <div class="grid grid-cols-1 gap-2 mb-4">
                <input name="hint1" placeholder="Hint 1 (Mild)" class="bg-gray-700 border border-gray-600 p-2 rounded text-white">
                <input name="hint2" placeholder="Hint 2 (Clearer)" class="bg-gray-700 border border-gray-600 p-2 rounded text-white">
                <input name="hint3" placeholder="Hint 3 (Giveaway)" class="bg-gray-700 border border-gray-600 p-2 rounded text-white">
            </div>
            <div class="grid grid-cols-2 gap-3 mb-4">
                <div>
                    <label class="block text-xs text-gray-400 uppercase">Type</label>
                    <select name="type" class="w-full bg-gray-700 border border-gray-600 p-2 rounded text-white">
                        <option value="photo">Photo Upload</option>
                        <option value="text">Text Answer</option>
                    </select>
                </div>
                <div>
                    <label class="block text-xs text-gray-400 uppercase">Difficulty</label>
                    <select name="difficulty" class="w-full bg-gray-700 border border-gray-600 p-2 rounded text-white">
                        <option value="easy">easy</option>
                        <option value="medium" selected>medium</option>
                        <option value="hard">hard</option>
                    </select>
                </div>
            </div>
            <div class="mb-4">
                <label class="block text-xs text-gray-400 uppercase">Correct Answer (if text type)</label>
                <input name="answer" class="w-full bg-gray-700 border border-gray-600 p-2 rounded text-white">
            </div>
            <div class="mb-4">
                <label class="block text-xs text-gray-400 uppercase">Points</label>
                <input name="points" type="number" min="0" value="10" class="w-full bg-gray-700 border border-gray-600 p-2 rounded text-white">
            </div>

        {% elif form_type == 'route' %}
            <div class="mb-4">
                <label class="block text-xs text-gray-400 uppercase">Route Name</label>
                <input name="name" class="w-full bg-gray-700 border border-gray-600 p-2 rounded text-white" required>
            </div>

        {% elif form_type == 'route_step' %}
            <div class="mb-4">
                <label class="block text-xs text-gray-400 uppercase">Route</label>
                <select name="route_id" class="w-full bg-gray-700 border border-gray-600 p-2 rounded text-white" required>
                    {% for r in routes %}
                        <option value="{{ r.id }}">{{ r.name }}</option>
                    {% endfor %}
                </select>
            </div>
            <div class="mb-4">
                <label class="block text-xs text-gray-400 uppercase">POI</label>
                <select name="poi_id" class="w-full bg-gray-700 border border-gray-600 p-2 rounded text-white" required>
                    {% for p in pois %}
                        <option value="{{ p.id }}">{{ p.title }}</option>
                    {% endfor %}
                </select>
            </div>
            <div class="mb-4">
                <label class="block text-xs text-gray-400 uppercase">Step Index (0-based)</label>
                <input name="step_index" type="number" min="0" value="0" class="w-full bg-gray-700 border border-gray-600 p-2 rounded text-white" required>
            </div>

        {% elif form_type == 'user' %}
            <div class="mb-4">
                <label class="block text-xs text-gray-400 uppercase">User Code (Login ID)</label>
                <input name="code" class="w-full bg-gray-700 border border-gray-600 p-2 rounded text-white" required>
            </div>
            <div class="mb-4">
                <label class="block text-xs text-gray-400 uppercase">Real Name (Optional)</label>
                <input name="name" class="w-full bg-gray-700 border border-gray-600 p-2 rounded text-white">
            </div>
        {% endif %}

        <button type="submit" class="w-full bg-green-600 text-white font-bold py-2 rounded hover:bg-green-700">Create</button>
    </form>

    <div class="mt-4 text-center">
        <a href="/dashboard" class="text-gray-400 text-sm">Back to Dashboard</a>
    </div>
</div>
{% endblock %}
"""

template_map = {
    "base": BASE_LAYOUT,
    "login": LOGIN_TEMPLATE,
    "admin_dashboard": ADMIN_DASHBOARD,
    "player_dashboard": PLAYER_DASHBOARD,
    "team_waiting": TEAM_WAITING,
    "admin_form": ADMIN_FORMS,
}


def render_view(template_name, **kwargs):
    tmpl = template_map.get(template_name)
    if not tmpl:
        abort(500)

    # Simple inheritance workaround for single-file templates
    if '{% extends "base" %}' in tmpl:
        content_start = tmpl.find("{% block content %}") + len("{% block content %}")
        content_end = tmpl.find("{% endblock %}")
        inner_content = tmpl[content_start:content_end]
        final_html = BASE_LAYOUT.replace("{% block content %}{% endblock %}", inner_content)
        return render_template_string(final_html, **kwargs)

    return render_template_string(tmpl, **kwargs)


# ==========================================
# HELPERS
# ==========================================

def _is_logged_in() -> bool:
    return bool(session.get("user_id"))


def _current_user() -> User:
    uid = session.get("user_id")
    if not uid:
        return None
    return User.query.get(uid)


def _require_admin():
    if not session.get("is_admin"):
        abort(403)


def allowed_image_filename(filename: str) -> bool:
    ext = Path(filename).suffix.lower()
    return ext in ALLOWED_EXTS


def assign_next_poi(team: Team):
    """
    Route-based assignment:
    - Look up RouteSteps for team's route ordered by step_index
    - team.route_step_index selects the next step
    - If done, mark finished
    """
    if not team.route_id:
        team.current_poi = None
        team.is_finished = False
        db.session.commit()
        return

    steps = (
        RouteStep.query.filter_by(route_id=team.route_id)
        .order_by(RouteStep.step_index.asc())
        .all()
    )

    if team.route_step_index >= len(steps):
        team.current_poi = None
        team.is_finished = True
        db.session.commit()
        return

    next_step = steps[team.route_step_index]
    next_poi = next_step.poi

    team.current_poi = next_poi
    team.is_finished = False

    # Create progress record if it doesn't exist
    existing = TeamPOIProgress.query.filter_by(team_id=team.id, poi_id=next_poi.id).first()
    if not existing:
        db.session.add(TeamPOIProgress(team_id=team.id, poi_id=next_poi.id, status="assigned"))

    db.session.commit()


def complete_current_poi(team: Team, poi: POI):
    """
    Marks progress completed, awards points (minus hint penalty),
    advances route index, assigns next POI.
    """
    progress = TeamPOIProgress.query.filter_by(team_id=team.id, poi_id=poi.id).first()
    if not progress:
        progress = TeamPOIProgress(team_id=team.id, poi_id=poi.id, status="assigned", hints_used=0)
        db.session.add(progress)
        db.session.flush()

    progress.status = "completed"
    progress.completed_at = datetime.utcnow()

    penalty = (progress.hints_used or 0) * 2
    points = max(0, (poi.points or 0) - penalty)
    team.score += points

    team.route_step_index += 1
    assign_next_poi(team)

    db.session.commit()
    return points


# ==========================================
# SETUP / SAMPLE DATA
# ==========================================

def setup():
    db.create_all()

    # Admin user
    if not User.query.filter_by(code="ADMIN").first():
        db.session.add(User(code="ADMIN", is_admin=True, name="Organizer"))
        db.session.commit()

    # Sample POIs (only if none exist)
    if not POI.query.first():
        p1 = POI(
            title="Charles Bridge Statue",
            riddle="I am a saint touched by many for luck, standing on a bridge of stone. Find me and snap a selfie.",
            hint_1="Look for the bronze plaques.",
            hint_2="I am John of Nepomuk.",
            hint_3="On Charles Bridge.",
            completion_type="photo",
            difficulty="easy",
            points=10,
        )
        p2 = POI(
            title="Clock Tower",
            riddle="I chime every hour and death rings the bell. What year was I installed?",
            hint_1="Old Town Square.",
            hint_2="Astronomical Clock.",
            hint_3="Google knows: 14xx.",
            completion_type="text",
            answer_key="1410",
            difficulty="medium",
            points=10,
        )
        p3 = POI(
            title="Dancing House",
            riddle="Fred and Ginger captured in glass and concrete.",
            hint_1="By the river.",
            hint_2="Modern architecture.",
            hint_3="Often called the Dancing House.",
            completion_type="photo",
            difficulty="hard",
            points=12,
        )
        db.session.add_all([p1, p2, p3])
        db.session.commit()

    # Sample players (only if none exist besides ADMIN)
    if User.query.filter_by(is_admin=False).count() == 0:
        db.session.add_all([
            User(code="PLAYER1", name="Alice"),
            User(code="PLAYER2", name="Bob"),
            User(code="PLAYER3", name="Charlie"),
            User(code="PLAYER4", name="David"),
            User(code="PLAYER5", name="Eve"),
        ])
        db.session.commit()

    # Sample routes (only if none exist)
    if not Route.query.first():
        r1 = Route(name="Route A (Sample)")
        r2 = Route(name="Route B (Sample)")
        db.session.add_all([r1, r2])
        db.session.commit()

        # Put the 3 sample POIs into both routes, in a slightly different order
        pois = POI.query.order_by(POI.id.asc()).all()
        if len(pois) >= 3:
            db.session.add_all([
                RouteStep(route_id=r1.id, poi_id=pois[0].id, step_index=0),
                RouteStep(route_id=r1.id, poi_id=pois[1].id, step_index=1),
                RouteStep(route_id=r1.id, poi_id=pois[2].id, step_index=2),

                RouteStep(route_id=r2.id, poi_id=pois[1].id, step_index=0),
                RouteStep(route_id=r2.id, poi_id=pois[2].id, step_index=1),
                RouteStep(route_id=r2.id, poi_id=pois[0].id, step_index=2),
            ])
            db.session.commit()


# ==========================================
# AUTH / BASIC ROUTES
# ==========================================

@app.route("/", methods=["GET", "POST"])
def index():
    if request.method == "POST":
        code = request.form.get("code", "").upper().strip()
        user = User.query.filter_by(code=code).first()
        if user:
            session["user_id"] = user.id
            session["is_admin"] = bool(user.is_admin)
            return redirect(url_for("dashboard"))
        flash("Invalid Code", "error")

    if session.get("user_id"):
        return redirect(url_for("dashboard"))

    return render_view("login")


@app.route("/logout")
def logout():
    session.clear()
    return redirect(url_for("index"))


@app.route("/dashboard")
def dashboard():
    if not _is_logged_in():
        return redirect(url_for("index"))

    user = _current_user()
    if not user:
        session.clear()
        return redirect(url_for("index"))

    if user.is_admin:
        return admin_dashboard()

    if not user.team_id:
        return render_view("team_waiting", user=user)

    team = Team.query.get(user.team_id)
    if not team:
        # Safety: if team deleted
        user.team_id = None
        db.session.commit()
        return render_view("team_waiting", user=user)

    current_poi = None
    progress = None

    if team.current_poi_id:
        current_poi = POI.query.get(team.current_poi_id)
        if current_poi:
            progress = TeamPOIProgress.query.filter_by(team_id=team.id, poi_id=current_poi.id).first()

    completed = TeamPOIProgress.query.filter_by(team_id=team.id, status="completed").order_by(TeamPOIProgress.completed_at.desc().nullslast()).all()

    return render_view(
        "player_dashboard",
        team=team,
        current_poi=current_poi,
        progress=progress,
        completed=completed,
    )


# ==========================================
# MEDIA (photos on disk)
# ==========================================

@app.route("/media/<int:sub_id>")
def media(sub_id: int):
    """
    Serve a photo by submission id.
    - Admin: can view everything
    - Player: can only view their team's photos
    """
    if not _is_logged_in():
        abort(403)

    user = _current_user()
    sub = Submission.query.get_or_404(sub_id)

    if not user.is_admin:
        if not user.team_id or sub.team_id != user.team_id:
            abort(403)

    if sub.type != "photo":
        abort(404)

    filename = sub.content
    if not filename:
        abort(404)

    return send_from_directory(str(UPLOAD_DIR), filename)


# ==========================================
# PLAYER ACTIONS
# ==========================================

@app.route("/action/hint", methods=["POST"])
def request_hint():
    if not _is_logged_in():
        return redirect(url_for("index"))

    user = _current_user()
    if not user or user.is_admin:
        return redirect(url_for("dashboard"))

    team = user.team
    if not team or not team.current_poi:
        return redirect(url_for("dashboard"))

    progress = TeamPOIProgress.query.filter_by(team_id=team.id, poi_id=team.current_poi.id).first()
    if not progress:
        progress = TeamPOIProgress(team_id=team.id, poi_id=team.current_poi.id, status="assigned", hints_used=0)
        db.session.add(progress)
        db.session.commit()

    if progress.hints_used < 3:
        progress.hints_used += 1
        db.session.commit()
        flash("Hint revealed! (2 points penalty will apply on completion.)", "success")

    return redirect(url_for("dashboard"))


@app.route("/action/submit", methods=["POST"])
def submit_proof():
    if not _is_logged_in():
        return redirect(url_for("index"))

    user = _current_user()
    if not user or user.is_admin:
        return redirect(url_for("dashboard"))

    team = user.team
    poi = team.current_poi if team else None
    if not team or not poi:
        return redirect(url_for("dashboard"))

    # PHOTO (auto-accept, store on disk)
    if poi.completion_type == "photo":
        if "proof_file" not in request.files:
            flash("No file uploaded", "error")
            return redirect(url_for("dashboard"))

        file = request.files["proof_file"]
        if not file or file.filename == "":
            flash("No file selected", "error")
            return redirect(url_for("dashboard"))

        safe_name = secure_filename(file.filename)
        if not safe_name:
            flash("Invalid filename", "error")
            return redirect(url_for("dashboard"))

        if not allowed_image_filename(safe_name):
            flash("Unsupported file type. Use JPG/PNG/WebP.", "error")
            return redirect(url_for("dashboard"))

        # unique filename
        ext = Path(safe_name).suffix.lower()
        unique = f"team{team.id}_poi{poi.id}_{int(datetime.utcnow().timestamp())}{ext}"
        save_path = UPLOAD_DIR / unique
        file.save(save_path)

        # record submission (approved)
        sub = Submission(team_id=team.id, poi_id=poi.id, type="photo", content=unique, status="approved")
        db.session.add(sub)
        db.session.commit()

        points = complete_current_poi(team, poi)
        flash(f"Photo accepted! {points} points added.", "success")
        return redirect(url_for("dashboard"))

    # TEXT (auto-check)
    answer = request.form.get("proof_text", "").strip().lower()
    correct = (poi.answer_key or "").strip().lower()

    if correct and answer == correct:
        sub = Submission(team_id=team.id, poi_id=poi.id, type="text", content=answer, status="approved")
        db.session.add(sub)
        db.session.commit()

        points = complete_current_poi(team, poi)
        flash(f"Correct! {points} points added.", "success")
    else:
        flash("Incorrect answer. Try again.", "error")

    return redirect(url_for("dashboard"))


# ==========================================
# ADMIN ROUTES
# ==========================================

def admin_dashboard():
    teams = Team.query.order_by(Team.score.desc()).all()
    users_count = User.query.filter_by(is_admin=False).count()
    routes_count = Route.query.count()

    photos = (
        Submission.query.filter_by(type="photo")
        .order_by(Submission.timestamp.desc())
        .limit(30)
        .all()
    )

    return render_view(
        "admin_dashboard",
        teams=teams,
        users_count=users_count,
        routes_count=routes_count,
        photos=photos
    )


@app.route("/admin/generate_teams")
def admin_generate_teams():
    _require_admin()

    users = User.query.filter_by(is_admin=False).all()
    ungrouped = [u for u in users if not u.team_id]

    if not ungrouped:
        flash("No users to assign.", "error")
        return redirect(url_for("dashboard"))

    routes = Route.query.order_by(Route.id.asc()).all()
    if not routes:
        flash("No routes exist yet. Create a Route + Route Steps first.", "error")
        return redirect(url_for("dashboard"))

    # Basic chunking to 4–5 for up to 24 players
    random.shuffle(ungrouped)

    chunk_size = 4
    chunks = [ungrouped[i:i + chunk_size] for i in range(0, len(ungrouped), chunk_size)]

    # If last chunk is too small, merge into previous
    if len(chunks) > 1 and len(chunks[-1]) < 3:
        leftovers = chunks.pop()
        chunks[-1].extend(leftovers)

    existing_team_count = Team.query.count()

    for i, group in enumerate(chunks):
        new_team = Team(name=f"Team {existing_team_count + i + 1}")

        # Assign routes round-robin so teams get fixed routes
        assigned_route = routes[i % len(routes)]
        new_team.route_id = assigned_route.id
        new_team.route_step_index = 0

        db.session.add(new_team)
        db.session.commit()

        for u in group:
            u.team = new_team

        assign_next_poi(new_team)

    db.session.commit()
    flash(f"Created {len(chunks)} new teams (routes assigned).", "success")
    return redirect(url_for("dashboard"))


@app.route("/admin/poi/new", methods=["GET", "POST"])
def admin_create_poi():
    _require_admin()

    if request.method == "POST":
        p = POI(
            title=request.form["title"].strip(),
            riddle=request.form["riddle"].strip(),
            hint_1=request.form.get("hint1") or None,
            hint_2=request.form.get("hint2") or None,
            hint_3=request.form.get("hint3") or None,
            completion_type=request.form["type"],
            answer_key=(request.form.get("answer") or None),
            difficulty=request.form.get("difficulty") or "medium",
            points=int(request.form.get("points") or 10),
        )
        db.session.add(p)
        db.session.commit()
        flash("POI created", "success")
        return redirect(url_for("dashboard"))

    return render_view("admin_form", title="Create New POI", form_type="poi")


@app.route("/admin/route/new", methods=["GET", "POST"])
def admin_create_route():
    _require_admin()

    if request.method == "POST":
        name = request.form["name"].strip()
        if not name:
            flash("Route name required", "error")
            return redirect(url_for("admin_create_route"))
        db.session.add(Route(name=name))
        db.session.commit()
        flash("Route created", "success")
        return redirect(url_for("dashboard"))

    return render_view("admin_form", title="Create New Route", form_type="route")


@app.route("/admin/route_step/new", methods=["GET", "POST"])
def admin_create_route_step():
    _require_admin()

    routes = Route.query.order_by(Route.id.asc()).all()
    pois = POI.query.order_by(POI.id.asc()).all()

    if request.method == "POST":
        route_id = int(request.form["route_id"])
        poi_id = int(request.form["poi_id"])
        step_index = int(request.form["step_index"])

        # ensure uniqueness: one step_index per route
        exists = RouteStep.query.filter_by(route_id=route_id, step_index=step_index).first()
        if exists:
            flash("That step_index is already used in this route.", "error")
            return redirect(url_for("admin_create_route_step"))

        db.session.add(RouteStep(route_id=route_id, poi_id=poi_id, step_index=step_index))
        db.session.commit()
        flash("Route step added", "success")
        return redirect(url_for("dashboard"))

    return render_view(
        "admin_form",
        title="Add Route Step",
        form_type="route_step",
        routes=routes,
        pois=pois
    )


@app.route("/admin/user/new", methods=["GET", "POST"])
def admin_create_user():
    _require_admin()

    if request.method == "POST":
        code = request.form["code"].upper().strip()
        if User.query.filter_by(code=code).first():
            flash("Code already exists", "error")
        else:
            u = User(code=code, name=request.form.get("name"))
            db.session.add(u)
            db.session.commit()
            flash(f"User {code} created", "success")
            return redirect(url_for("dashboard"))

    return render_view("admin_form", title="Add Participant", form_type="user")


# ==========================================
# BOOT
# ==========================================

if __name__ == "__main__":
    with app.app_context():
        setup()
    app.run(debug=True, port=5000)
