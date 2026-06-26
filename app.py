import os
import csv
import io
import sqlite3
from datetime import date, datetime
from functools import wraps
from pathlib import Path

from flask import (
    Flask, Response, abort, flash, g, redirect, render_template, request,
    session, url_for
)
from werkzeug.security import check_password_hash, generate_password_hash
from werkzeug.utils import secure_filename

BASE_DIR = Path(__file__).resolve().parent
DATA_DIR = BASE_DIR / "data"
DB_PATH = DATA_DIR / "app.db"

UPLOAD_FOLDER = BASE_DIR / "static" / "uploads"
COVERS_FOLDER = UPLOAD_FOLDER / "covers"
AUDIO_FOLDER = UPLOAD_FOLDER / "audio"
AVATARS_FOLDER = UPLOAD_FOLDER / "avatars"

ALLOWED_IMAGE_EXTENSIONS = {"png", "jpg", "jpeg", "webp", "gif"}
ALLOWED_AUDIO_EXTENSIONS = {"mp3", "wav", "ogg", "m4a"}

app = Flask(__name__)
app.config["SECRET_KEY"] = os.environ.get("SECRET_KEY", "dev-secret-key-change-me")
app.config["MAX_CONTENT_LENGTH"] = 30 * 1024 * 1024  # 30 MB


#  Работа с базой данных

def get_db():
    """Возвращает соединение с SQLite для текущего запроса."""
    if "db" not in g:
        g.db = sqlite3.connect(DB_PATH)
        g.db.row_factory = sqlite3.Row
    return g.db


@app.teardown_appcontext
def close_db(error=None):
    """Закрывает соединение с БД после завершения запроса."""
    db = g.pop("db", None)
    if db is not None:
        db.close()


def init_db():
    """Создаёт таблицы и стартовые данные, if БД пустая"""
    DATA_DIR.mkdir(exist_ok=True)
    for folder in (COVERS_FOLDER, AUDIO_FOLDER, AVATARS_FOLDER):
        folder.mkdir(parents=True, exist_ok=True)

    db = sqlite3.connect(DB_PATH)
    db.row_factory = sqlite3.Row

    with open(BASE_DIR / "schema.sql", "r", encoding="utf-8") as file:
        db.executescript(file.read())

    users_count = db.execute("SELECT COUNT(*) AS count FROM users").fetchone()["count"]
    if users_count == 0:
        seed_db(db)

    db.commit()
    db.close()


def seed_db(db):
    """Добавляет тестовые данные для демонстрации проекта."""
    admin_password = generate_password_hash("admin123")
    artist_password = generate_password_hash("artist123")

    db.execute(
        "INSERT INTO users (email, password_hash, role) VALUES (?, ?, ?)",
        ("admin@example.com", admin_password, "admin"),
    )
    db.execute(
        "INSERT INTO users (email, password_hash, role) VALUES (?, ?, ?)",
        ("artist@example.com", artist_password, "artist"),
    )
    artist_user_id = db.execute(
        "SELECT id FROM users WHERE email = ?", ("artist@example.com",)
    ).fetchone()["id"]

    db.execute(
        "INSERT INTO artists (user_id, nickname, full_name, bio) VALUES (?, ?, ?, ?)",
        (artist_user_id, "K1RIX", "Осыкин Кирилл Андреевич", "Независимый артист"),
    )
    artist_id = db.execute(
        "SELECT id FROM artists WHERE user_id = ?", (artist_user_id,)
    ).fetchone()["id"]

    releases = [
        ("Funk da Galáxia", "Сингл", "Запланированный сингл в стиле funk", "2026-07-10", "Запланирован"),
        ("RALLY FUNK", "Сингл", "Опубликованный энергичный funk-релиз", "2026-05-20", "Опубликован"),
        ("Vibra Diferente", "Сингл", "Опубликованный сингл с атмосферным звучанием", "2026-05-10", "Опубликован"),
        ("DEIXA BRILHAR", "Сингл", "Релиз, находящийся на модерации", "2026-05-01", "На модерации"),
        ("Альбом", "Альбом", "Альбом в разработке", "2026-04-15", "Черновик"),
    ]
    for title, release_type, description, release_date, status in releases:
        db.execute(
            """
            INSERT INTO releases (artist_id, title, release_type, description, release_date, status)
            VALUES (?, ?, ?, ?, ?, ?)
            """,
            (artist_id, title, release_type, description, release_date, status),
        )

    platforms = [
        ("Spotify", "https://spotify.com"),
        ("Apple Music", "https://music.apple.com"),
        ("YouTube", "https://youtube.com"),
        ("SoundCloud", "https://soundcloud.com"),
        ("Яндекс Музыка", "https://music.yandex.ru"),
    ]
    for name, url in platforms:
        db.execute("INSERT INTO platforms (name, url) VALUES (?, ?)", (name, url))

    first_release_id = db.execute(
        "SELECT id FROM releases WHERE title = ?", ("Летний вайб",)
    ).fetchone()["id"]
    second_release_id = db.execute(
        "SELECT id FROM releases WHERE title = ?", ("Ночные города",)
    ).fetchone()["id"]

    tracks = [
        (first_release_id, "Летний вайб", "03:21", "Pop", "Опубликован"),
        (first_release_id, "Закат", "02:58", "Pop", "Опубликован"),
        (second_release_id, "Ночные города", "04:05", "Electronic", "Опубликован"),
    ]
    for release_id, title, duration, genre, status in tracks:
        db.execute(
            """
            INSERT INTO tracks (release_id, title, duration, genre, status)
            VALUES (?, ?, ?, ?, ?)
            """,
            (release_id, title, duration, genre, status),
        )

    track_rows = db.execute("SELECT id, title FROM tracks").fetchall()
    platform_rows = db.execute("SELECT id, name FROM platforms").fetchall()
    streams = {
        "Spotify": 72340,
        "Apple Music": 38120,
        "YouTube": 22450,
        "SoundCloud": 11230,
        "Яндекс Музыка": 8200,
    }
    for track in track_rows:
        for platform in platform_rows:
            value = int(streams.get(platform["name"], 1000) / len(track_rows))
            db.execute(
                """
                INSERT INTO listening_statistics (track_id, platform_id, streams_count, report_date)
                VALUES (?, ?, ?, ?)
                """,
                (track["id"], platform["id"], value, "2026-05-20"),
            )


#  Вспомогательные функции
def allowed_file(filename, allowed_extensions):
    return "." in filename and filename.rsplit(".", 1)[1].lower() in allowed_extensions


def save_uploaded_file(file, folder, allowed_extensions):
    """Сохраняет файл и возвращает имя файла для записи в БД."""
    if not file or file.filename == "":
        return None
    if not allowed_file(file.filename, allowed_extensions):
        flash("Недопустимый формат файла", "danger")
        return None

    filename = secure_filename(file.filename)
    unique_name = f"{datetime.now().strftime('%Y%m%d%H%M%S')}_{filename}"
    file.save(folder / unique_name)
    return unique_name


def current_user():
    if "user_id" not in session:
        return None
    return get_db().execute(
        "SELECT * FROM users WHERE id = ?", (session["user_id"],)
    ).fetchone()


def current_artist():
    user = current_user()
    if not user:
        return None
    return get_db().execute(
        "SELECT * FROM artists WHERE user_id = ?", (user["id"],)
    ).fetchone()


@app.context_processor
def inject_user():
    return {"current_user": current_user(), "current_artist": current_artist()}


def login_required(view):
    @wraps(view)
    def wrapped_view(*args, **kwargs):
        if "user_id" not in session:
            flash("Сначала войдите в систему", "warning")
            return redirect(url_for("login"))
        return view(*args, **kwargs)
    return wrapped_view


def admin_required(view):
    @wraps(view)
    def wrapped_view(*args, **kwargs):
        user = current_user()
        if not user or user["role"] != "admin":
            abort(403)
        return view(*args, **kwargs)
    return wrapped_view


def can_edit_release(release):
    user = current_user()
    artist = current_artist()
    if not user:
        return False
    if user["role"] == "admin":
        return True
    return artist and release["artist_id"] == artist["id"]


#  Авторизация

@app.route("/register", methods=("GET", "POST"))
def register():
    if request.method == "POST":
        email = request.form.get("email", "").strip().lower()
        password = request.form.get("password", "")
        nickname = request.form.get("nickname", "").strip() or "Новый артист"
        full_name = request.form.get("full_name", "").strip()

        if not email or not password:
            flash("Заполните email и пароль", "danger")
            return render_template("register.html")

        db = get_db()
        exists = db.execute("SELECT id FROM users WHERE email = ?", (email,)).fetchone()
        if exists:
            flash("Пользователь с таким email уже существует", "danger")
            return render_template("register.html")

        db.execute(
            "INSERT INTO users (email, password_hash, role) VALUES (?, ?, ?)",
            (email, generate_password_hash(password), "artist"),
        )
        user_id = db.execute("SELECT last_insert_rowid() AS id").fetchone()["id"]
        db.execute(
            "INSERT INTO artists (user_id, nickname, full_name) VALUES (?, ?, ?)",
            (user_id, nickname, full_name),
        )
        db.commit()
        flash("Регистрация выполнена. Теперь можно войти", "success")
        return redirect(url_for("login"))

    return render_template("register.html")


@app.route("/login", methods=("GET", "POST"))
def login():
    if request.method == "POST":
        email = request.form.get("email", "").strip().lower()
        password = request.form.get("password", "")
        user = get_db().execute("SELECT * FROM users WHERE email = ?", (email,)).fetchone()

        if not user or not check_password_hash(user["password_hash"], password):
            flash("Неверный email или пароль", "danger")
            return render_template("login.html")

        session.clear()
        session["user_id"] = user["id"]
        flash("Вы вошли в систему", "success")
        return redirect(url_for("dashboard"))

    return render_template("login.html")


@app.route("/logout")
def logout():
    session.clear()
    flash("Вы вышли из системы", "info")
    return redirect(url_for("index"))


#  Основные страницы

@app.route("/")
def index():
    if current_user():
        return redirect(url_for("dashboard"))
    releases = get_db().execute(
        """
        SELECT releases.*, artists.nickname AS artist_name
        FROM releases
        JOIN artists ON releases.artist_id = artists.id
        WHERE releases.status = 'Опубликован'
        ORDER BY releases.release_date DESC
        """
    ).fetchall()
    return render_template("public.html", releases=releases)


@app.route("/dashboard")
@login_required
def dashboard():
    db = get_db()
    user = current_user()
    artist = current_artist()

    if user["role"] == "admin":
        releases_filter = ""
        params = []
    else:
        releases_filter = "WHERE artist_id = ?"
        params = [artist["id"]]

    releases_count = db.execute(
        f"SELECT COUNT(*) AS count FROM releases {releases_filter}", params
    ).fetchone()["count"]
    tracks_count = db.execute(
        """
        SELECT COUNT(*) AS count
        FROM tracks
        JOIN releases ON tracks.release_id = releases.id
        """ + ("" if user["role"] == "admin" else "WHERE releases.artist_id = ?"),
        [] if user["role"] == "admin" else [artist["id"]],
    ).fetchone()["count"]
    streams_count = db.execute(
        """
        SELECT COALESCE(SUM(streams_count), 0) AS total
        FROM listening_statistics
        JOIN tracks ON listening_statistics.track_id = tracks.id
        JOIN releases ON tracks.release_id = releases.id
        """ + ("" if user["role"] == "admin" else "WHERE releases.artist_id = ?"),
        [] if user["role"] == "admin" else [artist["id"]],
    ).fetchone()["total"]
    platforms_count = db.execute("SELECT COUNT(*) AS count FROM platforms").fetchone()["count"]

    recent_releases = db.execute(
        """
        SELECT * FROM releases
        """ + ("" if user["role"] == "admin" else "WHERE artist_id = ?") +
        " ORDER BY release_date DESC LIMIT 5",
        [] if user["role"] == "admin" else [artist["id"]],
    ).fetchall()

    return render_template(
        "dashboard.html",
        releases_count=releases_count,
        tracks_count=tracks_count,
        streams_count=streams_count,
        platforms_count=platforms_count,
        recent_releases=recent_releases,
    )


#  Профиль

@app.route("/profile", methods=("GET", "POST"))
@login_required
def profile():
    db = get_db()
    user = current_user()
    artist = current_artist()

    if request.method == "POST":
        email = request.form.get("email", "").strip().lower()
        nickname = request.form.get("nickname", "").strip()
        full_name = request.form.get("full_name", "").strip()
        bio = request.form.get("bio", "").strip()
        new_password = request.form.get("new_password", "")

        avatar_file = request.files.get("avatar")
        avatar_name = save_uploaded_file(avatar_file, AVATARS_FOLDER, ALLOWED_IMAGE_EXTENSIONS)

        db.execute("UPDATE users SET email = ? WHERE id = ?", (email, user["id"]))
        if new_password:
            db.execute(
                "UPDATE users SET password_hash = ? WHERE id = ?",
                (generate_password_hash(new_password), user["id"]),
            )

        if artist:
            if avatar_name:
                db.execute(
                    """
                    UPDATE artists
                    SET nickname = ?, full_name = ?, bio = ?, avatar = ?
                    WHERE id = ?
                    """,
                    (nickname, full_name, bio, avatar_name, artist["id"]),
                )
            else:
                db.execute(
                    "UPDATE artists SET nickname = ?, full_name = ?, bio = ? WHERE id = ?",
                    (nickname, full_name, bio, artist["id"]),
                )
        else:
            db.execute(
                "INSERT INTO artists (user_id, nickname, full_name, bio, avatar) VALUES (?, ?, ?, ?, ?)",
                (user["id"], nickname, full_name, bio, avatar_name),
            )

        db.commit()
        flash("Профиль обновлён", "success")
        return redirect(url_for("profile"))

    return render_template("profile.html", user=user, artist=artist)


# Релизы

@app.route("/releases")
def releases():
    db = get_db()
    user = current_user()
    artist = current_artist()
    search = request.args.get("search", "").strip()
    status = request.args.get("status", "")

    query = """
        SELECT releases.*, artists.nickname AS artist_name,
        COUNT(tracks.id) AS tracks_count
        FROM releases
        JOIN artists ON releases.artist_id = artists.id
        LEFT JOIN tracks ON tracks.release_id = releases.id
        WHERE 1 = 1
    """
    params = []

    if not user:
        query += " AND releases.status = 'Опубликован'"
    elif user["role"] != "admin":
        query += " AND releases.artist_id = ?"
        params.append(artist["id"])

    if search:
        query += " AND releases.title LIKE ?"
        params.append(f"%{search}%")
    if status:
        query += " AND releases.status = ?"
        params.append(status)

    query += " GROUP BY releases.id ORDER BY releases.release_date DESC"
    release_rows = db.execute(query, params).fetchall()
    return render_template("releases.html", releases=release_rows, search=search, status=status)


@app.route("/releases/new", methods=("GET", "POST"))
@login_required
def release_create():
    user = current_user()
    artist = current_artist()
    if user["role"] != "admin" and not artist:
        flash("Для создания релиза нужен профиль артиста", "danger")
        return redirect(url_for("profile"))

    if request.method == "POST":
        db = get_db()
        cover_name = save_uploaded_file(request.files.get("cover_image"), COVERS_FOLDER, ALLOWED_IMAGE_EXTENSIONS)
        db.execute(
            """
            INSERT INTO releases (artist_id, title, release_type, description, cover_image, release_date, status)
            VALUES (?, ?, ?, ?, ?, ?, ?)
            """,
            (
                artist["id"],
                request.form.get("title"),
                request.form.get("release_type"),
                request.form.get("description"),
                cover_name,
                request.form.get("release_date") or None,
                request.form.get("status"),
            ),
        )
        db.commit()
        flash("Релиз создан", "success")
        return redirect(url_for("releases"))

    return render_template("release_form.html", release=None)


@app.route("/releases/<int:release_id>")
def release_detail(release_id):
    db = get_db()
    release = db.execute(
        """
        SELECT releases.*, artists.nickname AS artist_name
        FROM releases
        JOIN artists ON releases.artist_id = artists.id
        WHERE releases.id = ?
        """,
        (release_id,),
    ).fetchone()
    if not release:
        abort(404)
    if release["status"] != "Опубликован" and not can_edit_release(release):
        abort(403)

    tracks = db.execute("SELECT * FROM tracks WHERE release_id = ? ORDER BY id", (release_id,)).fetchall()
    platforms = db.execute("SELECT * FROM platforms ORDER BY name").fetchall()
    stats = db.execute(
        """
        SELECT listening_statistics.*, tracks.title AS track_title, platforms.name AS platform_name
        FROM listening_statistics
        JOIN tracks ON listening_statistics.track_id = tracks.id
        JOIN platforms ON listening_statistics.platform_id = platforms.id
        WHERE tracks.release_id = ?
        ORDER BY report_date DESC
        """,
        (release_id,),
    ).fetchall()
    return render_template(
        "release_detail.html",
        release=release,
        tracks=tracks,
        platforms=platforms,
        stats=stats,
        can_edit=can_edit_release(release),
    )


@app.route("/releases/<int:release_id>/edit", methods=("GET", "POST"))
@login_required
def release_edit(release_id):
    db = get_db()
    release = db.execute("SELECT * FROM releases WHERE id = ?", (release_id,)).fetchone()
    if not release:
        abort(404)
    if not can_edit_release(release):
        abort(403)

    if request.method == "POST":
        cover_name = save_uploaded_file(request.files.get("cover_image"), COVERS_FOLDER, ALLOWED_IMAGE_EXTENSIONS)
        if cover_name:
            db.execute(
                """
                UPDATE releases
                SET title = ?, release_type = ?, description = ?, cover_image = ?, release_date = ?, status = ?, updated_at = CURRENT_TIMESTAMP
                WHERE id = ?
                """,
                (
                    request.form.get("title"),
                    request.form.get("release_type"),
                    request.form.get("description"),
                    cover_name,
                    request.form.get("release_date") or None,
                    request.form.get("status"),
                    release_id,
                ),
            )
        else:
            db.execute(
                """
                UPDATE releases
                SET title = ?, release_type = ?, description = ?, release_date = ?, status = ?, updated_at = CURRENT_TIMESTAMP
                WHERE id = ?
                """,
                (
                    request.form.get("title"),
                    request.form.get("release_type"),
                    request.form.get("description"),
                    request.form.get("release_date") or None,
                    request.form.get("status"),
                    release_id,
                ),
            )
        db.commit()
        flash("Релиз обновлён", "success")
        return redirect(url_for("release_detail", release_id=release_id))

    return render_template("release_form.html", release=release)


@app.route("/releases/<int:release_id>/delete", methods=("POST",))
@login_required
def release_delete(release_id):
    db = get_db()
    release = db.execute("SELECT * FROM releases WHERE id = ?", (release_id,)).fetchone()
    if not release:
        abort(404)
    if not can_edit_release(release):
        abort(403)
    db.execute("DELETE FROM releases WHERE id = ?", (release_id,))
    db.commit()
    flash("Релиз удалён", "info")
    return redirect(url_for("releases"))


#   Треки

@app.route("/releases/<int:release_id>/tracks/new", methods=("GET", "POST"))
@login_required
def track_create(release_id):
    db = get_db()
    release = db.execute("SELECT * FROM releases WHERE id = ?", (release_id,)).fetchone()
    if not release:
        abort(404)
    if not can_edit_release(release):
        abort(403)

    if request.method == "POST":
        audio_name = save_uploaded_file(request.files.get("audio_file"), AUDIO_FOLDER, ALLOWED_AUDIO_EXTENSIONS)
        db.execute(
            """
            INSERT INTO tracks (release_id, title, duration, genre, audio_file, lyrics, status)
            VALUES (?, ?, ?, ?, ?, ?, ?)
            """,
            (
                release_id,
                request.form.get("title"),
                request.form.get("duration"),
                request.form.get("genre"),
                audio_name,
                request.form.get("lyrics"),
                request.form.get("status"),
            ),
        )
        db.commit()
        flash("Трек добавлен", "success")
        return redirect(url_for("release_detail", release_id=release_id))

    return render_template("track_form.html", track=None, release=release)


@app.route("/tracks/<int:track_id>/edit", methods=("GET", "POST"))
@login_required
def track_edit(track_id):
    db = get_db()
    track = db.execute("SELECT * FROM tracks WHERE id = ?", (track_id,)).fetchone()
    if not track:
        abort(404)
    release = db.execute("SELECT * FROM releases WHERE id = ?", (track["release_id"],)).fetchone()
    if not can_edit_release(release):
        abort(403)

    if request.method == "POST":
        audio_name = save_uploaded_file(request.files.get("audio_file"), AUDIO_FOLDER, ALLOWED_AUDIO_EXTENSIONS)
        if audio_name:
            db.execute(
                """
                UPDATE tracks SET title = ?, duration = ?, genre = ?, audio_file = ?, lyrics = ?, status = ? WHERE id = ?
                """,
                (
                    request.form.get("title"),
                    request.form.get("duration"),
                    request.form.get("genre"),
                    audio_name,
                    request.form.get("lyrics"),
                    request.form.get("status"),
                    track_id,
                ),
            )
        else:
            db.execute(
                """
                UPDATE tracks SET title = ?, duration = ?, genre = ?, lyrics = ?, status = ? WHERE id = ?
                """,
                (
                    request.form.get("title"),
                    request.form.get("duration"),
                    request.form.get("genre"),
                    request.form.get("lyrics"),
                    request.form.get("status"),
                    track_id,
                ),
            )
        db.commit()
        flash("Трек обновлён", "success")
        return redirect(url_for("release_detail", release_id=release["id"]))

    return render_template("track_form.html", track=track, release=release)


@app.route("/tracks/<int:track_id>/delete", methods=("POST",))
@login_required
def track_delete(track_id):
    db = get_db()
    track = db.execute("SELECT * FROM tracks WHERE id = ?", (track_id,)).fetchone()
    if not track:
        abort(404)
    release = db.execute("SELECT * FROM releases WHERE id = ?", (track["release_id"],)).fetchone()
    if not can_edit_release(release):
        abort(403)
    db.execute("DELETE FROM tracks WHERE id = ?", (track_id,))
    db.commit()
    flash("Трек удалён", "info")
    return redirect(url_for("release_detail", release_id=release["id"]))


#  Площадки и статистика

@app.route("/platforms", methods=("GET", "POST"))
@login_required
def platforms():
    db = get_db()
    if request.method == "POST":
        db.execute(
            "INSERT INTO platforms (name, url) VALUES (?, ?)",
            (request.form.get("name"), request.form.get("url")),
        )
        db.commit()
        flash("Площадка добавлена", "success")
        return redirect(url_for("platforms"))

    platform_rows = db.execute("SELECT * FROM platforms ORDER BY name").fetchall()
    return render_template("platforms.html", platforms=platform_rows)


@app.route("/platforms/<int:platform_id>/delete", methods=("POST",))
@login_required
def platform_delete(platform_id):
    get_db().execute("DELETE FROM platforms WHERE id = ?", (platform_id,))
    get_db().commit()
    flash("Площадка удалена", "info")
    return redirect(url_for("platforms"))


@app.route("/statistics/add", methods=("POST",))
@login_required
def statistic_add():
    db = get_db()
    track_id = request.form.get("track_id")
    platform_id = request.form.get("platform_id")
    streams_count = request.form.get("streams_count") or 0
    report_date = request.form.get("report_date") or date.today().isoformat()

    track = db.execute("SELECT * FROM tracks WHERE id = ?", (track_id,)).fetchone()
    if not track:
        abort(404)
    release = db.execute("SELECT * FROM releases WHERE id = ?", (track["release_id"],)).fetchone()
    if not can_edit_release(release):
        abort(403)

    db.execute(
        """
        INSERT INTO listening_statistics (track_id, platform_id, streams_count, report_date)
        VALUES (?, ?, ?, ?)
        """,
        (track_id, platform_id, streams_count, report_date),
    )
    db.commit()
    flash("Статистика добавлена", "success")
    return redirect(url_for("release_detail", release_id=release["id"]))


@app.route("/analytics")
@login_required
def analytics():
    db = get_db()
    user = current_user()
    artist = current_artist()
    where = ""
    params = []
    if user["role"] != "admin":
        where = "WHERE releases.artist_id = ?"
        params = [artist["id"]]

    totals = db.execute(
        f"""
        SELECT COALESCE(SUM(listening_statistics.streams_count), 0) AS streams,
               COUNT(DISTINCT releases.id) AS releases_count,
               COUNT(DISTINCT platforms.id) AS platforms_count
        FROM releases
        LEFT JOIN tracks ON tracks.release_id = releases.id
        LEFT JOIN listening_statistics ON listening_statistics.track_id = tracks.id
        LEFT JOIN platforms ON platforms.id = listening_statistics.platform_id
        {where}
        """,
        params,
    ).fetchone()

    popular_releases = db.execute(
        f"""
        SELECT releases.title, releases.release_date,
               COALESCE(SUM(listening_statistics.streams_count), 0) AS streams
        FROM releases
        LEFT JOIN tracks ON tracks.release_id = releases.id
        LEFT JOIN listening_statistics ON listening_statistics.track_id = tracks.id
        {where}
        GROUP BY releases.id
        ORDER BY streams DESC
        LIMIT 5
        """,
        params,
    ).fetchall()

    platform_stats = db.execute(
        f"""
        SELECT platforms.name, COALESCE(SUM(listening_statistics.streams_count), 0) AS streams
        FROM platforms
        LEFT JOIN listening_statistics ON listening_statistics.platform_id = platforms.id
        LEFT JOIN tracks ON tracks.id = listening_statistics.track_id
        LEFT JOIN releases ON releases.id = tracks.release_id
        {where if where else ''}
        GROUP BY platforms.id
        ORDER BY streams DESC
        """,
        params,
    ).fetchall()

    top_release = popular_releases[0] if popular_releases else None
    return render_template(
        "analytics.html",
        totals=totals,
        popular_releases=popular_releases,
        platform_stats=platform_stats,
        top_release=top_release,
    )



@app.route("/export/releases.csv")
@login_required
def export_releases_csv():
    """Выгружает релизы и статистику в CSV-файл."""
    db = get_db()
    user = current_user()
    artist = current_artist()

    where = ""
    params = []
    if user["role"] != "admin":
        where = "WHERE releases.artist_id = ?"
        params = [artist["id"]]

    rows = db.execute(
        f"""
        SELECT releases.title, releases.release_type, releases.release_date, releases.status,
               artists.nickname AS artist_name,
               COALESCE(SUM(listening_statistics.streams_count), 0) AS streams
        FROM releases
        JOIN artists ON artists.id = releases.artist_id
        LEFT JOIN tracks ON tracks.release_id = releases.id
        LEFT JOIN listening_statistics ON listening_statistics.track_id = tracks.id
        {where}
        GROUP BY releases.id
        ORDER BY releases.release_date DESC
        """,
        params,
    ).fetchall()

    output = io.StringIO()
    writer = csv.writer(output, delimiter=";")
    writer.writerow(["Артист", "Релиз", "Тип", "Дата выхода", "Статус", "Прослушивания"])
    for row in rows:
        writer.writerow([
            row["artist_name"],
            row["title"],
            row["release_type"],
            row["release_date"],
            row["status"],
            row["streams"],
        ])

    return Response(
        output.getvalue(),
        mimetype="text/csv; charset=utf-8",
        headers={"Content-Disposition": "attachment; filename=releases_report.csv"},
    )


# Администратор

@app.route("/admin/users", methods=("GET", "POST"))
@login_required
@admin_required
def admin_users():
    db = get_db()
    if request.method == "POST":
        user_id = request.form.get("user_id")
        role = request.form.get("role")
        db.execute("UPDATE users SET role = ? WHERE id = ?", (role, user_id))
        db.commit()
        flash("Роль пользователя обновлена", "success")
        return redirect(url_for("admin_users"))

    users = db.execute(
        """
        SELECT users.*, artists.nickname
        FROM users
        LEFT JOIN artists ON artists.user_id = users.id
        ORDER BY users.created_at DESC
        """
    ).fetchall()
    return render_template("admin_users.html", users=users)


# Запуск приложения

@app.errorhandler(403)
def forbidden(error):
    return render_template("error.html", title="Доступ запрещён", message="У вас нет прав для этой страницы"), 403


@app.errorhandler(404)
def not_found(error):
    return render_template("error.html", title="Страница не найдена", message="Такой страницы не существует"), 404


init_db()

if __name__ == "__main__":
    app.run(debug=True)
