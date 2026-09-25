from __future__ import annotations

import asyncio
import json
import os
import secrets
import sqlite3
from contextlib import asynccontextmanager
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from fastapi import FastAPI, Header, HTTPException, WebSocket, WebSocketDisconnect
from fastapi.responses import FileResponse
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel, Field, model_validator
from dotenv import load_dotenv
try:
    from supabase import Client, create_client
except ImportError:
    Client = Any
    create_client = None

ROOT = Path(__file__).parent
DB_PATH = ROOT / "tournament.db"
load_dotenv(ROOT / ".env")
SUPABASE_URL = os.getenv("SUPABASE_URL", "").replace("/rest/v1", "").rstrip("/")
SUPABASE_SERVICE_ROLE_KEY = os.getenv("SUPABASE_SERVICE_ROLE_KEY", "")
SUPABASE: Any = create_client(SUPABASE_URL, SUPABASE_SERVICE_ROLE_KEY) if create_client and SUPABASE_URL and SUPABASE_SERVICE_ROLE_KEY else None


def now() -> str:
    return datetime.now(timezone.utc).isoformat()


def connect() -> sqlite3.Connection:
    connection = sqlite3.connect(DB_PATH)
    connection.row_factory = sqlite3.Row
    return connection


def initialize_database() -> None:
    with connect() as database:
        database.executescript(
            """
            CREATE TABLE IF NOT EXISTS tournaments (
                id INTEGER PRIMARY KEY,
                name TEXT NOT NULL,
                venue TEXT NOT NULL,
                status TEXT NOT NULL,
                courts INTEGER NOT NULL DEFAULT 4,
                updated_at TEXT NOT NULL
            );
            CREATE TABLE IF NOT EXISTS matches (
                id INTEGER PRIMARY KEY,
                tournament_id INTEGER NOT NULL,
                group_id INTEGER,
                stage TEXT NOT NULL DEFAULT 'knockout',
                court TEXT NOT NULL,
                round TEXT NOT NULL,
                team_one TEXT NOT NULL,
                team_two TEXT NOT NULL,
                score_one INTEGER NOT NULL DEFAULT 0,
                score_two INTEGER NOT NULL DEFAULT 0,
                status TEXT NOT NULL,
                scheduled_at TEXT NOT NULL,
                updated_at TEXT NOT NULL
            );
            CREATE TABLE IF NOT EXISTS match_events (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                match_id INTEGER NOT NULL,
                actor TEXT NOT NULL,
                score_one INTEGER NOT NULL,
                score_two INTEGER NOT NULL,
                created_at TEXT NOT NULL
            );
            CREATE TABLE IF NOT EXISTS groups (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                tournament_id INTEGER NOT NULL,
                name TEXT NOT NULL,
                created_at TEXT NOT NULL
            );
            CREATE TABLE IF NOT EXISTS players (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                group_id INTEGER NOT NULL,
                name TEXT NOT NULL,
                seed INTEGER NOT NULL,
                UNIQUE(group_id, name)
            );
            """
        )
        tournament_columns = {row["name"] for row in database.execute("PRAGMA table_info(tournaments)")}
        if "courts" not in tournament_columns:
            database.execute("ALTER TABLE tournaments ADD COLUMN courts INTEGER NOT NULL DEFAULT 4")
        match_columns = {row["name"] for row in database.execute("PRAGMA table_info(matches)")}
        if "group_id" not in match_columns:
            database.execute("ALTER TABLE matches ADD COLUMN group_id INTEGER")
        if "stage" not in match_columns:
            database.execute("ALTER TABLE matches ADD COLUMN stage TEXT NOT NULL DEFAULT 'knockout'")
        tournament = database.execute("SELECT id, name FROM tournaments LIMIT 1").fetchone()
        if tournament:
            if tournament["name"] == "Northside Rally Open":
                database.execute(
                    "UPDATE tournaments SET name = ?, venue = ?, updated_at = ? WHERE id = ?",
                    ("Singles Top Dawgs", "Tournament Control Room", now(), tournament["id"]),
                )
            demo_match = database.execute("SELECT id FROM matches WHERE team_one = 'The Dinks' OR team_two = 'The Dinks'").fetchone()
            if demo_match:
                database.execute("DELETE FROM match_events")
                database.execute("DELETE FROM matches")
                database.execute("UPDATE tournaments SET status = 'UPCOMING', updated_at = ? WHERE id = ?", (now(), tournament["id"]))
            return
        timestamp = now()
        database.execute(
            "INSERT INTO tournaments VALUES (?, ?, ?, ?, ?, ?)",
            (1, "Singles Top Dawgs", "Tournament Control Room", "UPCOMING", 4, timestamp),
        )


class ScoreUpdate(BaseModel):
    score_one: int = Field(ge=0, le=99)
    score_two: int = Field(ge=0, le=99)


class AccessRequest(BaseModel):
    access_code: str = Field(min_length=1, max_length=200)


class TournamentUpdate(BaseModel):
    name: str = Field(min_length=1, max_length=100)
    venue: str = Field(min_length=1, max_length=100)
    status: str = Field(pattern="^(UPCOMING|LIVE|FINAL)$")
    courts: int = Field(ge=1, le=64)


class TournamentCreate(BaseModel):
    name: str = Field(min_length=1, max_length=100)
    venue: str = Field(min_length=1, max_length=100)
    courts: int = Field(ge=1, le=64)


class PlayerInput(BaseModel):
    name: str = Field(min_length=1, max_length=100)
    seed: int = Field(ge=1, le=999)

    @model_validator(mode="before")
    @classmethod
    def accept_legacy_player_line(cls, value: Any) -> Any:
        if isinstance(value, str):
            parts = value.split("|", 1)
            if len(parts) == 2:
                return {"seed": int(parts[0].strip()), "name": parts[1].strip()}
        return value


class GroupCreate(BaseModel):
    name: str = Field(min_length=1, max_length=80)
    players: list[PlayerInput] = Field(min_length=2, max_length=32)


class GroupUpdate(GroupCreate):
    pass


class SeedRequest(BaseModel):
    group_id: int


class GroupStageRequest(BaseModel):
    pass


class KnockoutRequest(BaseModel):
    pass


class ConnectionManager:
    def __init__(self) -> None:
        self.connections: dict[WebSocket, int] = {}

    async def connect(self, websocket: WebSocket, tournament_id: str) -> None:
        await websocket.accept()
        self.connections[websocket] = tournament_id

    def disconnect(self, websocket: WebSocket) -> None:
        self.connections.pop(websocket, None)

    async def broadcast(self, tournament_id: str, payload: dict[str, Any]) -> None:
        message = json.dumps(payload)
        stale = []
        for connection, connection_tournament_id in self.connections.items():
            if connection_tournament_id != tournament_id:
                continue
            try:
                await connection.send_text(message)
            except Exception:
                stale.append(connection)
        for connection in stale:
            self.disconnect(connection)


manager = ConnectionManager()


@asynccontextmanager
async def lifespan(_: FastAPI):
    if SUPABASE is None:
        initialize_database()
    yield


app = FastAPI(title="Rally Control", lifespan=lifespan)
app.mount("/static", StaticFiles(directory=ROOT / "static"), name="static")


def require_admin(role: str | None) -> None:
    if not role or not secrets.compare_digest(role, os.getenv("DIRECTOR_ACCESS_CODE", "director-local")):
        raise HTTPException(status_code=403, detail="Director access is required")


def cloud_state(tournament_id: str) -> dict[str, Any]:
    assert SUPABASE is not None
    tournament_result = SUPABASE.table("tournaments").select("*").eq("id", tournament_id).single().execute()
    if not tournament_result.data:
        raise HTTPException(status_code=404, detail="Tournament not found")
    tournament = tournament_result.data
    matches = SUPABASE.table("matches").select("*").eq("tournament_id", tournament_id).order("id").execute().data or []
    groups = SUPABASE.table("groups").select("*").eq("tournament_id", tournament_id).order("id").execute().data or []
    standings = []
    for group in groups:
        players = SUPABASE.table("players").select("*").eq("group_id", group["id"]).order("seed").execute().data or []
        table = {player["name"]: {"name": player["name"], "seed": player["seed"], "played": 0, "wins": 0, "losses": 0, "points_for": 0, "points_against": 0} for player in players}
        for match in matches:
            if match.get("group_id") != group["id"] or match.get("stage") != "group" or match["status"] not in {"LIVE", "FINAL"}:
                continue
            if match["team_one"] not in table or match["team_two"] not in table:
                continue
            first, second = table[match["team_one"]], table[match["team_two"]]
            first["played"] += 1; second["played"] += 1
            first["points_for"] += match["score_one"]; first["points_against"] += match["score_two"]
            second["points_for"] += match["score_two"]; second["points_against"] += match["score_one"]
            if match["status"] == "FINAL":
                winner, loser = (first, second) if match["score_one"] > match["score_two"] else (second, first)
                winner["wins"] += 1; loser["losses"] += 1
        for player in table.values():
            player["point_difference"] = player["points_for"] - player["points_against"]
        standings.append({"group_id": group["id"], "group_name": group["name"], "players": sorted(table.values(), key=lambda player: (-player["wins"], -player["point_difference"], player["seed"]))})
        group["players"] = players
    return {"tournament": tournament, "matches": matches, "groups": groups, "standings": standings, "server_time": now()}


def current_state(tournament_id: int | str = 1) -> dict[str, Any]:
    if SUPABASE is not None:
        return cloud_state(str(tournament_id))
    with connect() as database:
        tournament_row = database.execute("SELECT * FROM tournaments WHERE id = ?", (tournament_id,)).fetchone()
        if not tournament_row:
            raise HTTPException(status_code=404, detail="Tournament not found")
        tournament = dict(tournament_row)
        matches = [dict(row) for row in database.execute("SELECT * FROM matches WHERE tournament_id = ? ORDER BY id", (tournament_id,))]
        groups = []
        standings = []
        for group in database.execute("SELECT * FROM groups WHERE tournament_id = ? ORDER BY id", (tournament_id,)):
            item = dict(group)
            item["players"] = [dict(player) for player in database.execute("SELECT * FROM players WHERE group_id = ? ORDER BY seed", (group["id"],))]
            groups.append(item)
            table = {player["name"]: {"name": player["name"], "seed": player["seed"], "played": 0, "wins": 0, "losses": 0, "points_for": 0, "points_against": 0} for player in item["players"]}
            for match in database.execute("SELECT * FROM matches WHERE group_id = ? AND stage = 'group'", (group["id"],)):
                if match["status"] not in {"LIVE", "FINAL"}:
                    continue
                if match["team_one"] not in table or match["team_two"] not in table:
                    continue
                first, second = table[match["team_one"]], table[match["team_two"]]
                first["played"] += 1; second["played"] += 1
                first["points_for"] += match["score_one"]; first["points_against"] += match["score_two"]
                second["points_for"] += match["score_two"]; second["points_against"] += match["score_one"]
                if match["status"] == "FINAL":
                    winner, loser = (first, second) if match["score_one"] > match["score_two"] else (second, first)
                    winner["wins"] += 1; loser["losses"] += 1
            for player in table.values():
                player["point_difference"] = player["points_for"] - player["points_against"]
            standings.append({"group_id": group["id"], "group_name": group["name"], "players": sorted(table.values(), key=lambda player: (-player["wins"], -player["point_difference"], player["seed"]))})
    return {"tournament": tournament, "matches": matches, "groups": groups, "standings": standings, "server_time": now()}


@app.get("/")
def index() -> FileResponse:
    return FileResponse(ROOT / "static" / "index.html")


@app.get("/api/state")
def get_state(tournament_id: str = "1") -> dict[str, Any]:
    return current_state(tournament_id)


@app.get("/api/tournaments")
def get_tournaments() -> list[dict[str, Any]]:
    if SUPABASE is not None:
        return SUPABASE.table("tournaments").select("id, name, venue, status, courts").order("id", desc=True).execute().data or []
    with connect() as database:
        return [dict(row) for row in database.execute("SELECT id, name, venue, status, courts FROM tournaments ORDER BY id DESC")]


@app.post("/api/auth")
def authenticate(request: AccessRequest) -> dict[str, bool]:
    require_admin(request.access_code)
    return {"ok": True}


@app.post("/api/tournaments")
async def create_tournament(tournament: TournamentCreate, x_director_key: str | None = Header(default=None)) -> list[dict[str, Any]]:
    require_admin(x_director_key)
    if SUPABASE is not None:
        SUPABASE.table("tournaments").insert({"name": tournament.name, "venue": tournament.venue, "status": "UPCOMING", "courts": tournament.courts}).execute()
        return get_tournaments()
    with connect() as database:
        database.execute("INSERT INTO tournaments (name, venue, status, courts, updated_at) VALUES (?, ?, 'UPCOMING', ?, ?)", (tournament.name, tournament.venue, tournament.courts, now()))
    return get_tournaments()
    return get_tournaments()


@app.delete("/api/tournaments/{tournament_id}")
async def delete_tournament(tournament_id: str, x_director_key: str | None = Header(default=None)) -> list[dict[str, Any]]:
    require_admin(x_director_key)
    if SUPABASE is not None:
        tournaments = get_tournaments()
        if not any(str(item["id"]) == str(tournament_id) for item in tournaments):
            raise HTTPException(status_code=404, detail="Tournament not found")
        if len(tournaments) <= 1:
            raise HTTPException(status_code=400, detail="The last tournament cannot be deleted")
        SUPABASE.table("tournaments").delete().eq("id", tournament_id).execute()
        return get_tournaments()
    with connect() as database:
        if not database.execute("SELECT id FROM tournaments WHERE id = ?", (tournament_id,)).fetchone():
            raise HTTPException(status_code=404, detail="Tournament not found")
        if database.execute("SELECT COUNT(*) AS count FROM tournaments").fetchone()["count"] <= 1:
            raise HTTPException(status_code=400, detail="The last tournament cannot be deleted")
        group_ids = [row["id"] for row in database.execute("SELECT id FROM groups WHERE tournament_id = ?", (tournament_id,))]
        match_ids = [row["id"] for row in database.execute("SELECT id FROM matches WHERE tournament_id = ?", (tournament_id,))]
        if match_ids:
            database.executemany("DELETE FROM match_events WHERE match_id = ?", [(match_id,) for match_id in match_ids])
            database.execute("DELETE FROM matches WHERE tournament_id = ?", (tournament_id,))
        if group_ids:
            database.executemany("DELETE FROM players WHERE group_id = ?", [(group_id,) for group_id in group_ids])
            database.execute("DELETE FROM groups WHERE tournament_id = ?", (tournament_id,))
        database.execute("DELETE FROM tournaments WHERE id = ?", (tournament_id,))
    return get_tournaments()


@app.post("/api/matches/{match_id}/score")
async def update_score(match_id: str, update: ScoreUpdate, x_director_key: str | None = Header(default=None), x_tournament_id: str = Header(default="1")) -> dict[str, Any]:
    require_admin(x_director_key)
    timestamp = now()
    if SUPABASE is not None:
        match = SUPABASE.table("matches").select("id").eq("id", match_id).eq("tournament_id", x_tournament_id).single().execute().data
        if not match:
            raise HTTPException(status_code=404, detail="Match not found")
        SUPABASE.table("matches").update({"score_one": update.score_one, "score_two": update.score_two, "status": "LIVE", "updated_at": timestamp}).eq("id", match_id).execute()
        SUPABASE.table("match_events").insert({"match_id": match_id, "score_one": update.score_one, "score_two": update.score_two, "created_at": timestamp}).execute()
        state = cloud_state(x_tournament_id)
        await manager.broadcast(x_tournament_id, {"type": "state_updated", "state": state})
        return state
    with connect() as database:
        match = database.execute("SELECT * FROM matches WHERE id = ?", (match_id,)).fetchone()
        if not match:
            raise HTTPException(status_code=404, detail="Match not found")
        database.execute(
            "UPDATE matches SET score_one = ?, score_two = ?, status = 'LIVE', updated_at = ? WHERE id = ?",
            (update.score_one, update.score_two, timestamp, match_id),
        )
        database.execute(
            "INSERT INTO match_events (match_id, actor, score_one, score_two, created_at) VALUES (?, ?, ?, ?, ?)",
            (match_id, "director", update.score_one, update.score_two, timestamp),
        )
    state = current_state(x_tournament_id)
    await manager.broadcast(x_tournament_id, {"type": "state_updated", "state": state})
    return state


@app.post("/api/matches/{match_id}/status")
async def update_status(match_id: str, status: str, x_director_key: str | None = Header(default=None), x_tournament_id: str = Header(default="1")) -> dict[str, Any]:
    require_admin(x_director_key)
    if status not in {"UPCOMING", "LIVE", "FINAL"}:
        raise HTTPException(status_code=400, detail="Invalid match status")
    if SUPABASE is not None:
        result = SUPABASE.table("matches").update({"status": status, "updated_at": now()}).eq("id", match_id).eq("tournament_id", x_tournament_id).execute()
        if not result.data:
            raise HTTPException(status_code=404, detail="Match not found")
        state = cloud_state(x_tournament_id)
        await manager.broadcast(x_tournament_id, {"type": "state_updated", "state": state})
        return state
    with connect() as database:
        result = database.execute("UPDATE matches SET status = ?, updated_at = ? WHERE id = ?", (status, now(), match_id))
        if result.rowcount == 0:
            raise HTTPException(status_code=404, detail="Match not found")
    state = current_state(x_tournament_id)
    await manager.broadcast(x_tournament_id, {"type": "state_updated", "state": state})
    return state


@app.post("/api/matches/{match_id}/reset")
async def reset_match(match_id: str, x_director_key: str | None = Header(default=None), x_tournament_id: str = Header(default="1")) -> dict[str, Any]:
    require_admin(x_director_key)
    timestamp = now()
    if SUPABASE is not None:
        match = SUPABASE.table("matches").select("id").eq("id", match_id).eq("tournament_id", x_tournament_id).single().execute().data
        if not match:
            raise HTTPException(status_code=404, detail="Match not found")
        SUPABASE.table("matches").update({"score_one": 0, "score_two": 0, "status": "UPCOMING", "updated_at": timestamp}).eq("id", match_id).execute()
        SUPABASE.table("match_events").delete().eq("match_id", match_id).execute()
        state = cloud_state(x_tournament_id)
        await manager.broadcast(x_tournament_id, {"type": "state_updated", "state": state})
        return state
    with connect() as database:
        match = database.execute("SELECT id FROM matches WHERE id = ? AND tournament_id = ?", (match_id, x_tournament_id)).fetchone()
        if not match:
            raise HTTPException(status_code=404, detail="Match not found")
        database.execute("UPDATE matches SET score_one = 0, score_two = 0, status = 'UPCOMING', updated_at = ? WHERE id = ?", (timestamp, match_id))
        database.execute("DELETE FROM match_events WHERE match_id = ?", (match_id,))
    state = current_state(x_tournament_id)
    await manager.broadcast(x_tournament_id, {"type": "state_updated", "state": state})
    return state


@app.delete("/api/matches/{match_id}")
async def delete_match(match_id: str, x_director_key: str | None = Header(default=None), x_tournament_id: str = Header(default="1")) -> dict[str, Any]:
    require_admin(x_director_key)
    if SUPABASE is not None:
        match = SUPABASE.table("matches").select("id").eq("id", match_id).eq("tournament_id", x_tournament_id).single().execute().data
        if not match:
            raise HTTPException(status_code=404, detail="Match not found")
        SUPABASE.table("match_events").delete().eq("match_id", match_id).execute()
        SUPABASE.table("matches").delete().eq("id", match_id).execute()
        state = cloud_state(x_tournament_id)
        await manager.broadcast(x_tournament_id, {"type": "state_updated", "state": state})
        return state
    with connect() as database:
        match = database.execute("SELECT id FROM matches WHERE id = ? AND tournament_id = ?", (match_id, x_tournament_id)).fetchone()
        if not match:
            raise HTTPException(status_code=404, detail="Match not found")
        database.execute("DELETE FROM match_events WHERE match_id = ?", (match_id,))
        database.execute("DELETE FROM matches WHERE id = ?", (match_id,))
    state = current_state(x_tournament_id)
    await manager.broadcast(x_tournament_id, {"type": "state_updated", "state": state})
    return state


@app.put("/api/tournament")
async def update_tournament(update: TournamentUpdate, x_director_key: str | None = Header(default=None), x_tournament_id: str = Header(default="1")) -> dict[str, Any]:
    require_admin(x_director_key)
    if SUPABASE is not None:
        SUPABASE.table("tournaments").update({"name": update.name, "venue": update.venue, "status": update.status, "courts": update.courts, "updated_at": now()}).eq("id", x_tournament_id).execute()
        state = cloud_state(x_tournament_id)
        await manager.broadcast(x_tournament_id, {"type": "state_updated", "state": state})
        return state
    with connect() as database:
        database.execute("UPDATE tournaments SET name = ?, venue = ?, status = ?, courts = ?, updated_at = ? WHERE id = ?", (update.name, update.venue, update.status, update.courts, now(), x_tournament_id))
    state = current_state(x_tournament_id)
    await manager.broadcast(x_tournament_id, {"type": "state_updated", "state": state})
    return state


@app.post("/api/groups")
async def create_group(group: GroupCreate, x_director_key: str | None = Header(default=None), x_tournament_id: str = Header(default="1")) -> dict[str, Any]:
    require_admin(x_director_key)
    players = sorted(group.players, key=lambda player: player.seed)
    if len({player.seed for player in players}) != len(players):
        raise HTTPException(status_code=400, detail="Each player needs a unique seed")
    if len(players) < 2:
        raise HTTPException(status_code=400, detail="Add at least two players")
    if SUPABASE is not None:
        try:
            created_rows = SUPABASE.table("groups").insert({"tournament_id": x_tournament_id, "name": group.name.strip()}).select().execute().data or []
            if not created_rows:
                raise HTTPException(status_code=500, detail="Supabase did not return the new group")
            created = created_rows[0]
            SUPABASE.table("players").insert([{"group_id": created["id"], "name": player.name.strip(), "seed": player.seed} for player in players]).execute()
            state = cloud_state(x_tournament_id)
            await manager.broadcast(x_tournament_id, {"type": "state_updated", "state": state})
            return state
        except HTTPException:
            raise
        except Exception as exc:
            raise HTTPException(status_code=502, detail=f"Supabase group save failed: {exc}") from exc
    with connect() as database:
        cursor = database.execute("INSERT INTO groups (tournament_id, name, created_at) VALUES (?, ?, ?)", (x_tournament_id, group.name.strip(), now()))
        database.executemany("INSERT INTO players (group_id, name, seed) VALUES (?, ?, ?)", [(cursor.lastrowid, player.name.strip(), player.seed) for player in players])
    state = current_state(x_tournament_id)
    await manager.broadcast(x_tournament_id, {"type": "state_updated", "state": state})
    return state


@app.put("/api/groups/{group_id}")
async def update_group(group_id: str, group: GroupUpdate, x_director_key: str | None = Header(default=None), x_tournament_id: str = Header(default="1")) -> dict[str, Any]:
    require_admin(x_director_key)
    players = sorted(group.players, key=lambda player: player.seed)
    if len({player.seed for player in players}) != len(players):
        raise HTTPException(status_code=400, detail="Each player needs a unique seed")
    if SUPABASE is not None:
        group_result = SUPABASE.table("groups").select("id").eq("id", group_id).eq("tournament_id", x_tournament_id).single().execute().data
        if not group_result:
            raise HTTPException(status_code=404, detail="Group not found")
        SUPABASE.table("groups").update({"name": group.name.strip()}).eq("id", group_id).execute()
        SUPABASE.table("players").delete().eq("group_id", group_id).execute()
        SUPABASE.table("players").insert([{"group_id": group_id, "name": player.name.strip(), "seed": player.seed} for player in players]).execute()
        state = cloud_state(x_tournament_id)
        await manager.broadcast(x_tournament_id, {"type": "state_updated", "state": state})
        return state
    with connect() as database:
        if not database.execute("SELECT id FROM groups WHERE id = ? AND tournament_id = ?", (group_id, x_tournament_id)).fetchone():
            raise HTTPException(status_code=404, detail="Group not found")
        database.execute("UPDATE groups SET name = ? WHERE id = ?", (group.name.strip(), group_id))
        database.execute("DELETE FROM players WHERE group_id = ?", (group_id,))
        database.executemany("INSERT INTO players (group_id, name, seed) VALUES (?, ?, ?)", [(group_id, player.name.strip(), player.seed) for player in players])
    state = current_state(x_tournament_id)
    await manager.broadcast(x_tournament_id, {"type": "state_updated", "state": state})
    return state


@app.post("/api/tournament/group-stage")
async def create_group_stage(_: GroupStageRequest, x_director_key: str | None = Header(default=None), x_tournament_id: str = Header(default="1")) -> dict[str, Any]:
    require_admin(x_director_key)
    timestamp = now()
    if SUPABASE is not None:
        groups = SUPABASE.table("groups").select("id, name").eq("tournament_id", x_tournament_id).order("created_at").execute().data or []
        if not groups:
            raise HTTPException(status_code=400, detail="Add at least one player group first")
        group_players = {}
        for group in groups:
            players = [row["name"] for row in (SUPABASE.table("players").select("name").eq("group_id", group["id"]).order("seed").execute().data or [])]
            if len(players) < 2:
                raise HTTPException(status_code=400, detail=f"{group['name']} needs at least two players")
            group_players[group["id"]] = players
        tournament = SUPABASE.table("tournaments").select("courts").eq("id", x_tournament_id).single().execute().data
        old_matches = SUPABASE.table("matches").select("id").eq("tournament_id", x_tournament_id).execute().data or []
        for old_match in old_matches:
            SUPABASE.table("match_events").delete().eq("match_id", old_match["id"]).execute()
        SUPABASE.table("matches").delete().eq("tournament_id", x_tournament_id).execute()
        rows = []
        for group in groups:
            players = group_players[group["id"]]
            for first_index in range(len(players)):
                for second_index in range(first_index + 1, len(players)):
                    rows.append({"tournament_id": x_tournament_id, "group_id": group["id"], "stage": "group", "court": f"Court {(len(rows) % tournament['courts']) + 1:02d}", "round": f"Group stage · {group['name']}", "team_one": players[first_index], "team_two": players[second_index], "score_one": 0, "score_two": 0, "status": "UPCOMING", "scheduled_at": "12:00", "updated_at": timestamp})
        SUPABASE.table("matches").insert(rows).execute()
        state = cloud_state(x_tournament_id)
        await manager.broadcast(x_tournament_id, {"type": "state_updated", "state": state})
        return state
    with connect() as database:
        groups = list(database.execute("SELECT id, name FROM groups WHERE tournament_id = ? ORDER BY id", (x_tournament_id,)))
        if not groups:
            raise HTTPException(status_code=400, detail="Add at least one player group first")
        group_players = {}
        for group in groups:
            players = [row["name"] for row in database.execute("SELECT name FROM players WHERE group_id = ? ORDER BY seed", (group["id"],))]
            if len(players) < 2:
                raise HTTPException(status_code=400, detail=f"{group['name']} needs at least two players")
            group_players[group["id"]] = players
        courts = database.execute("SELECT courts FROM tournaments WHERE id = ?", (x_tournament_id,)).fetchone()["courts"]
        next_id = database.execute("SELECT COALESCE(MAX(id), 0) + 1 FROM matches").fetchone()[0]
        match_rows = []
        match_id = next_id
        database.execute("DELETE FROM match_events WHERE match_id IN (SELECT id FROM matches WHERE tournament_id = ?)", (x_tournament_id,))
        database.execute("DELETE FROM matches WHERE tournament_id = ?", (x_tournament_id,))
        for group in groups:
            players = group_players[group["id"]]
            for first_index in range(len(players)):
                for second_index in range(first_index + 1, len(players)):
                    match_rows.append((match_id, x_tournament_id, group["id"], "group", f"Court {(len(match_rows) % courts) + 1:02d}", f"Group stage · {group['name']}", players[first_index], players[second_index], 0, 0, "UPCOMING", "12:00", timestamp))
                    match_id += 1
        database.executemany("INSERT INTO matches (id, tournament_id, group_id, stage, court, round, team_one, team_two, score_one, score_two, status, scheduled_at, updated_at) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)", match_rows)
    state = current_state(x_tournament_id)
    await manager.broadcast(x_tournament_id, {"type": "state_updated", "state": state})
    return state


@app.post("/api/tournament/knockout")
async def create_knockout(_: KnockoutRequest, x_director_key: str | None = Header(default=None), x_tournament_id: str = Header(default="1")) -> dict[str, Any]:
    require_admin(x_director_key)
    state = current_state(x_tournament_id)
    if not state["standings"] or len(state["standings"]) < 2:
        raise HTTPException(status_code=400, detail="Create at least two groups first")
    if any(match["stage"] == "group" and match["status"] != "FINAL" for match in state["matches"]):
        raise HTTPException(status_code=400, detail="Finish every group-stage match before qualifying players")
    qualifiers = []
    for standing in state["standings"]:
        if len(standing["players"]) < 2:
            raise HTTPException(status_code=400, detail=f"{standing['group_name']} needs two ranked players")
        qualifiers.extend([(standing["group_name"], standing["players"][0]["name"]), (standing["group_name"], standing["players"][1]["name"])])
    bracket_size = 1
    while bracket_size < len(qualifiers):
        bracket_size *= 2
    qualifiers += [("Qualifier", "TBD")] * (bracket_size - len(qualifiers))
    pairings = list(zip(qualifiers[: bracket_size // 2], reversed(qualifiers[bracket_size // 2 :])))
    timestamp = now()
    if SUPABASE is not None:
        tournament = SUPABASE.table("tournaments").select("courts").eq("id", x_tournament_id).single().execute().data
        old_knockout = SUPABASE.table("matches").select("id").eq("tournament_id", x_tournament_id).eq("stage", "knockout").execute().data or []
        for old_match in old_knockout:
            SUPABASE.table("match_events").delete().eq("match_id", old_match["id"]).execute()
        SUPABASE.table("matches").delete().eq("tournament_id", x_tournament_id).eq("stage", "knockout").execute()
        rows = []
        round_size = len(pairings)
        round_number = 1
        while round_size:
            round_name = {1: "Final", 2: "Semi-final", 4: "Quarter-final"}.get(round_size, f"Knockout round {round_number}")
            names = pairings if round_number == 1 else [("Qualifier", "TBD") for _ in range(round_size)]
            for first, second in names:
                rows.append({"tournament_id": x_tournament_id, "stage": "knockout", "court": f"Court {(len(rows) % tournament['courts']) + 1:02d}", "round": round_name, "team_one": first[1] if isinstance(first, tuple) else first, "team_two": second[1] if isinstance(second, tuple) else second, "score_one": 0, "score_two": 0, "status": "UPCOMING", "scheduled_at": "13:00", "updated_at": timestamp})
            if round_size == 1:
                break
            round_size //= 2
            round_number += 1
        SUPABASE.table("matches").insert(rows).execute()
        state = cloud_state(x_tournament_id)
        await manager.broadcast(x_tournament_id, {"type": "state_updated", "state": state})
        return state
    with connect() as database:
        courts = database.execute("SELECT courts FROM tournaments WHERE id = ?", (x_tournament_id,)).fetchone()["courts"]
        next_id = database.execute("SELECT COALESCE(MAX(id), 0) + 1 FROM matches").fetchone()[0]
        database.execute("DELETE FROM matches WHERE tournament_id = ? AND stage = 'knockout'", (x_tournament_id,))
        rows = []
        match_id = next_id
        round_size = len(pairings)
        round_number = 1
        while round_size:
            round_name = {1: "Final", 2: "Semi-final", 4: "Quarter-final"}.get(round_size, f"Knockout round {round_number}")
            names = pairings if round_number == 1 else [("Qualifier", "TBD"), ("Qualifier", "TBD")] if round_size == 2 else [("Qualifier", "TBD") for _ in range(round_size)]
            for index, (first, second) in enumerate(names):
                first_name = first[1] if isinstance(first, tuple) else first
                second_name = second[1] if isinstance(second, tuple) else second
                rows.append((match_id, x_tournament_id, None, "knockout", f"Court {(len(rows) % courts) + 1:02d}", round_name, first_name, second_name, 0, 0, "UPCOMING", "13:00", timestamp))
                match_id += 1
            if round_size == 1:
                break
            round_size //= 2
            round_number += 1
        database.executemany("INSERT INTO matches (id, tournament_id, group_id, stage, court, round, team_one, team_two, score_one, score_two, status, scheduled_at, updated_at) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)", rows)
    state = current_state(x_tournament_id)
    await manager.broadcast(x_tournament_id, {"type": "state_updated", "state": state})
    return state


@app.websocket("/ws")
async def websocket_endpoint(websocket: WebSocket) -> None:
    tournament_id = websocket.query_params.get("tournament_id", "1")
    await manager.connect(websocket, tournament_id)
    try:
        await websocket.send_text(json.dumps({"type": "state_updated", "state": current_state(tournament_id)}))
        while True:
            await websocket.receive_text()
    except WebSocketDisconnect:
        manager.disconnect(websocket)
