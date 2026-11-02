"""The JDBC connection form's login part: the login fields show only when the secret scope is the app's login scope, the
copy says any signed-in user may make a connection, and a key with {database} in it asks which database its login is
for. Plain source checks, no app run."""
from pathlib import Path

PAGES = Path(__file__).resolve().parents[1] / "src" / "frontend" / "src" / "pages"
FORM = (PAGES / "JdbcConnectionForm.tsx").read_text()
LOGIN = (PAGES / "JdbcLoginFields.tsx").read_text()


def test_the_form_says_any_signed_in_user_may_make_a_connection():
    words = " ".join(FORM.split())
    assert "Any signed-in user can make one" in words
    assert "Only an app manager" not in words


def test_the_login_fields_show_only_when_the_secret_scope_is_the_app_login_scope():
    assert 'queryFn: () => api.get<Row>("/jdbc-logins")' in FORM
    assert "const loginsGoHere = dedicatedScope !== null && form.secret_scope.trim() === dedicatedScope;" in FORM
    assert "{loginsGoHere && <LoginFields connection={form.name} secretKey={form.secret_key} />}" in FORM
    assert "A login in any other scope is put there by an admin." in FORM


def test_a_key_with_database_in_it_asks_which_database_its_login_is_for():
    assert 'const needsDatabase = template.includes(DATABASE_TOKEN);' in LOGIN
    assert 'name="jdbc-login-database"' in LOGIN and "Database the login is for" in LOGIN
    assert "Name the database this login is for, to write its login." in LOGIN


def test_the_login_posts_the_key_as_typed_and_the_database_apart():
    assert "secret_key: template," in LOGIN
    assert "database: needsDatabase ? databaseName : null," in LOGIN


def test_the_login_is_written_under_the_key_with_the_database_filled_in():
    assert "template.split(DATABASE_TOKEN).join(databaseName)" in LOGIN
