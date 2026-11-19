"""SQLite persistence for runs, frozen snapshots, predictions and chart artifacts."""

import json
import sqlite3
from collections.abc import Iterator
from contextlib import contextmanager
from datetime import date
from decimal import Decimal
from hashlib import sha256
from pathlib import Path

from chart_lens.domain import Bar, ExperimentError, History
from chart_lens.report import Row


def canonical(value: object) -> str:
    """Canonical representation for cache keys and manifest hashes."""
    return json.dumps(value, sort_keys=True, separators=(",", ":"), allow_nan=False)


def digest(value: object) -> str:
    """Hash a canonical JSON value."""
    return sha256(canonical(value).encode()).hexdigest()


def pack(histories: dict[str, History]) -> dict:
    """Serialize canonical prices without converting decimals to binary floats."""
    return {
        symbol: {
            "bars": {
                d.isoformat(): [str(b.high), str(b.low), str(b.close)]
                for d, b in history.bars.items()
            },
            "splits": {d.isoformat(): str(ratio) for d, ratio in history.splits.items()},
        }
        for symbol, history in histories.items()
    }


def unpack(data: dict) -> dict[str, History]:
    """Recover the exact inputs used by a run."""
    return {
        symbol: History(
            {
                date.fromisoformat(d): Bar(*map(Decimal, values))
                for d, values in item["bars"].items()
            },
            {date.fromisoformat(d): Decimal(ratio) for d, ratio in item["splits"].items()},
        )
        for symbol, item in data.items()
    }


class Store:
    """Local single-process store with transactional writes and bounded run history."""

    def __init__(self, path: Path) -> None:
        path.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
        self.path = path
        with self.connection() as db:
            db.executescript("""
                CREATE TABLE IF NOT EXISTS runs (
                    id TEXT PRIMARY KEY, created TEXT NOT NULL, data TEXT NOT NULL
                );
                CREATE TABLE IF NOT EXISTS rows (
                    run TEXT REFERENCES runs(id) ON DELETE CASCADE,
                    origin TEXT NOT NULL, data TEXT NOT NULL, png BLOB, cache_key TEXT,
                    PRIMARY KEY (run, origin)
                );
                CREATE INDEX IF NOT EXISTS row_cache ON rows(cache_key);
                CREATE TABLE IF NOT EXISTS snapshots (
                    id TEXT PRIMARY KEY, mode TEXT NOT NULL, symbols TEXT NOT NULL,
                    start TEXT NOT NULL, end TEXT NOT NULL, version TEXT NOT NULL,
                    retrieved TEXT NOT NULL, data TEXT NOT NULL
                );
            """)

    @contextmanager
    def connection(self) -> Iterator[sqlite3.Connection]:
        """Open a transactional connection scoped to one calling thread."""
        db = sqlite3.connect(self.path, timeout=15)
        try:
            with db:
                db.execute("PRAGMA foreign_keys = ON")
                yield db
        finally:
            db.close()

    def runs(self) -> list[dict]:
        """Return saved manifests in reverse chronological order."""
        with self.connection() as db:
            return [
                json.loads(data)
                for (data,) in db.execute(
                    "SELECT data FROM runs ORDER BY created DESC",
                )
            ]

    def get(self, run_id: str) -> dict:
        """Return one manifest or a typed not-found error."""
        with self.connection() as db:
            row = db.execute("SELECT data FROM runs WHERE id = ?", (run_id,)).fetchone()
        if row is None:
            raise ExperimentError("run_not_found", "That run no longer exists.", 404)
        return json.loads(row[0])

    def create(self, run: dict, rows: list[Row]) -> None:
        """Create a run and every pending row in one transaction."""
        with self.connection() as db:
            db.execute(
                "INSERT INTO runs VALUES (?, ?, ?)", (run["id"], run["created_at"], canonical(run))
            )
            db.executemany(
                "INSERT INTO rows (run, origin, data) VALUES (?, ?, ?)",
                [(run["id"], row.origin, row.model_dump_json()) for row in rows],
            )

    def update(self, run_id: str, **fields: object) -> dict:
        """Merge manifest changes without overwriting concurrent field updates."""
        with self.connection() as db:
            db.execute("BEGIN IMMEDIATE")
            current = db.execute("SELECT data FROM runs WHERE id = ?", (run_id,)).fetchone()
            if current is None:
                raise ExperimentError("run_not_found", "That run no longer exists.", 404)
            run = json.loads(current[0])
            run.update(fields)
            db.execute("UPDATE runs SET data = ? WHERE id = ?", (canonical(run), run_id))
            return run

    def rows(self, run_id: str, offset: int = 0, limit: int = 252) -> list[Row]:
        """Read a chronological page of results."""
        with self.connection() as db:
            return [
                Row.model_validate_json(data)
                for (data,) in db.execute(
                    "SELECT data FROM rows WHERE run = ? ORDER BY origin LIMIT ? OFFSET ?",
                    (run_id, limit, offset),
                )
            ]

    def save(self, run_id: str, row: Row, png: bytes | None = None) -> None:
        """Persist a row, preserving an image saved at an earlier stage."""
        with self.connection() as db:
            db.execute(
                "UPDATE rows SET data = ?, png = coalesce(?, png), cache_key = ? "
                "WHERE run = ? AND origin = ?",
                (
                    row.model_dump_json(),
                    png,
                    row.cache_key if row.choice else None,
                    run_id,
                    row.origin,
                ),
            )

    def replace_prediction_origin(self, run_id: str, row: Row) -> None:
        """Replace a placeholder prediction with its resolved, older origin."""
        with self.connection() as db:
            db.execute("DELETE FROM rows WHERE run = ?", (run_id,))
            db.execute(
                "INSERT INTO rows (run, origin, data) VALUES (?, ?, ?)",
                (run_id, row.origin, row.model_dump_json()),
            )

    def image(self, run_id: str, origin: str) -> bytes:
        """Read exact PNG bytes for an existing row."""
        with self.connection() as db:
            value = db.execute(
                "SELECT png FROM rows WHERE run = ? AND origin = ?", (run_id, origin)
            ).fetchone()
        if value is None or value[0] is None:
            raise ExperimentError("image_not_found", "No chart is available for that origin.", 404)
        return value[0]

    def cached(self, key: str) -> Row | None:
        """Retrieve an earlier successful classification for the chart experiment."""
        with self.connection() as db:
            value = db.execute(
                "SELECT data FROM rows WHERE cache_key = ? LIMIT 1", (key,)
            ).fetchone()
        return Row.model_validate_json(value[0]) if value else None

    def lookup_snapshot(
        self,
        mode: str,
        symbols: str,
        start: str,
        end: str,
        version: str,
        fresh_since: str,
    ) -> tuple[str, str, dict] | None:
        """Reuse only an immutable snapshot that covers the interval and is fresh enough."""
        with self.connection() as db:
            row = db.execute(
                "SELECT id, retrieved, data FROM snapshots WHERE mode = ? AND symbols = ? "
                "AND start <= ? AND end >= ? AND version = ? AND retrieved >= ? "
                "ORDER BY retrieved DESC LIMIT 1",
                (mode, symbols, start, end, version, fresh_since),
            ).fetchone()
        return (row[0], row[1], json.loads(row[2])) if row else None

    def insert_snapshot(
        self,
        mode: str,
        symbols: str,
        start: str,
        end: str,
        version: str,
        retrieved: str,
        data: dict,
    ) -> tuple[str, str, dict]:
        """Save frozen market inputs and return their unique snapshot identity."""
        identity = digest([mode, symbols, start, end, version, retrieved, data])
        with self.connection() as db:
            db.execute(
                "INSERT INTO snapshots VALUES (?, ?, ?, ?, ?, ?, ?, ?)",
                (identity, mode, symbols, start, end, version, retrieved, canonical(data)),
            )
            self._prune_snapshots(db, keep=16)
        return identity, retrieved, data

    def snapshot(self, identity: str) -> tuple[str, str, dict]:
        """Load a run's original data without hitting a provider."""
        with self.connection() as db:
            row = db.execute(
                "SELECT retrieved, data FROM snapshots WHERE id = ?", (identity,)
            ).fetchone()
        if row is None:
            raise ExperimentError("snapshot_missing", "The saved data snapshot is missing.", 409)
        return identity, row[0], json.loads(row[1])

    @staticmethod
    def _prune_snapshots(db: sqlite3.Connection, keep: int) -> None:
        db.execute(
            "DELETE FROM snapshots WHERE id IN (SELECT id FROM snapshots "
            "WHERE id NOT IN (SELECT json_extract(data, '$.snapshot_id') FROM runs "
            "WHERE json_extract(data, '$.snapshot_id') IS NOT NULL) "
            "ORDER BY retrieved DESC LIMIT -1 OFFSET ?)",
            (keep,),
        )

    def delete(self, run_id: str) -> None:
        """Delete the local run and any artifacts owned only by it."""
        run = self.get(run_id)
        with self.connection() as db:
            db.execute("DELETE FROM runs WHERE id = ?", (run_id,))
            snapshot = run.get("snapshot_id")
            if snapshot:
                db.execute(
                    "DELETE FROM snapshots WHERE id = ? AND NOT EXISTS "
                    "(SELECT 1 FROM runs WHERE json_extract(data, '$.snapshot_id') = ?)",
                    (snapshot, snapshot),
                )
