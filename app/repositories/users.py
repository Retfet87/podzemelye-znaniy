"""Пользователи и учебные группы."""


class UserRepository:
    def __init__(self, db):
        self.db = db

    def start(self, user_id):
        with self.db.connect() as conn:
            conn.execute(
                "INSERT INTO users (max_user_id) VALUES (%s) ON CONFLICT DO NOTHING",
                (user_id,),
            )
            return self._group(conn, user_id)

    @staticmethod
    def _group(conn, user_id):
        return conn.execute(
            """SELECT g.name FROM users u JOIN groups g ON g.id = u.group_id
               WHERE u.max_user_id = %s""",
            (user_id,),
        ).fetchone()

    def active_groups(self):
        with self.db.connect() as conn:
            return conn.execute(
                "SELECT id, name FROM groups WHERE active ORDER BY name"
            ).fetchall()

    def choose_group(self, user_id, group_id):
        with self.db.connect() as conn:
            chosen = conn.execute(
                """UPDATE users u SET group_id = g.id, institution_code = g.institution_code FROM groups g
                   WHERE u.max_user_id = %s AND u.group_id IS NULL
                     AND g.id = %s AND g.active
                     AND (u.institution_code IS NULL OR u.institution_code = g.institution_code)
                   RETURNING g.name""",
                (user_id, group_id),
            ).fetchone()
            if chosen:
                return "saved", chosen[0]
            existing = self._group(conn, user_id)
            if existing:
                return "existing", existing[0]
            return "unavailable", None

    def registered_group(self, user_id):
        with self.db.connect() as conn:
            row = self._group(conn, user_id)
            return row[0] if row else None

    def add_groups(self, names):
        with self.db.connect() as conn, conn.cursor() as cur:
            cur.executemany(
                "INSERT INTO groups (name) VALUES (%s) ON CONFLICT (name) DO NOTHING",
                [(name,) for name in names],
            )

    def institutions(self):
        with self.db.connect() as conn:
            return conn.execute(
                "SELECT code, name FROM institutions WHERE active ORDER BY name"
            ).fetchall()

    def institution(self, user_id):
        with self.db.connect() as conn:
            row = conn.execute(
                "SELECT institution_code FROM users WHERE max_user_id = %s", (user_id,)
            ).fetchone()
            return row[0] if row else None

    def choose_institution(self, user_id, code):
        with self.db.connect() as conn:
            return bool(
                conn.execute(
                    """UPDATE users SET institution_code = %s WHERE max_user_id = %s AND group_id IS NULL
                   AND EXISTS (SELECT 1 FROM institutions WHERE code = %s AND active)""",
                    (code, user_id, code),
                ).rowcount
            )

    def institution_groups(self, code):
        with self.db.connect() as conn:
            return conn.execute(
                "SELECT id, name FROM groups WHERE active AND institution_code = %s ORDER BY name",
                (code,),
            ).fetchall()

    def welcome_needed(self, user_id):
        with self.db.connect() as conn:
            return conn.execute(
                "SELECT NOT welcome_seen FROM users WHERE max_user_id = %s", (user_id,)
            ).fetchone() == (True,)

    def mark_welcome(self, user_id):
        with self.db.connect() as conn:
            conn.execute(
                "UPDATE users SET welcome_seen = true WHERE max_user_id = %s",
                (user_id,),
            )
