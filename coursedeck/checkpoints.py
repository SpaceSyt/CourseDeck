"""Material reading positions live with the documents they describe."""

from .domain import now


class MaterialCheckpoints:
    def __init__(self, knowledge):
        self.knowledge = knowledge

    def get(self, provider, scope):
        with self.knowledge.connection() as connection:
            row = connection.execute(
                "SELECT next_identity FROM material_checkpoints WHERE provider=? AND scope=?",
                (provider, scope),
            ).fetchone()
        return row[0] if row else None

    def put(self, provider, scope, next_identity):
        with self.knowledge.connection() as connection:
            connection.execute(
                "INSERT INTO material_checkpoints VALUES (?, ?, ?, ?) "
                "ON CONFLICT(provider,scope) DO UPDATE SET "
                "next_identity=excluded.next_identity,updated_at=excluded.updated_at",
                (provider, scope, str(next_identity), now().isoformat()),
            )

    def rotate(self, provider, scope, entries, identity):
        """Unknown/removed identity restarts discovery; never skips new entries forever."""
        next_identity = self.get(provider, scope)
        start = next(
            (index for index, entry in enumerate(entries) if identity(entry) == next_identity), 0
        )
        return entries[start:] + entries[:start]
