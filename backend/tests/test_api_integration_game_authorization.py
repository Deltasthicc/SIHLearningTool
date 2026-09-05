"""Lane 5 authorization contracts for state-changing game routes."""
from __future__ import annotations

from datetime import datetime, timezone

from fastapi import FastAPI
from fastapi.testclient import TestClient
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker
from sqlalchemy.pool import StaticPool

from db.database import Base, get_db
from models.accuracy_history import AccuracyHistory  # noqa: F401
from models.dungeon import Dungeon, Room  # noqa: F401
from models.guild import Guild  # noqa: F401
from models.learning import LearningMaterial  # noqa: F401
from models.player import Player  # noqa: F401
from models.question import Question  # noqa: F401
from models.session import GameSession  # noqa: F401
from models.submission import AnswerSubmission  # noqa: F401
from routes.authorization import require_principal
from routes.game import router as game_router
from security.rbac import BoundPrincipal


class Subject:
    issuer = "https://issuer.example/realm"
    subject_id = "subject-1"
    roles = frozenset({"learner"})


def _principal(player_id: str | None) -> BoundPrincipal:
    return BoundPrincipal(
        subject=Subject(),
        binding_id="binding-1",
        player_id=player_id,
        roles=Subject.roles,
    )


def _app(db, principal: BoundPrincipal | None = None) -> FastAPI:
    app = FastAPI()
    app.include_router(game_router)

    def override_db():
        yield db

    app.dependency_overrides[get_db] = override_db
    if principal is not None:
        app.dependency_overrides[require_principal] = lambda: principal
    return app


def _db():
    engine = create_engine(
        "sqlite:///:memory:",
        connect_args={"check_same_thread": False},
        poolclass=StaticPool,
    )
    Base.metadata.create_all(engine)
    return sessionmaker(bind=engine)()


def test_game_write_route_requires_verified_bearer():
    db = _db()
    player = Player(username="unauthenticated-player")
    db.add(player)
    db.commit()

    response = TestClient(_app(db)).post(
        "/game/session/start",
        json={"player_id": player.player_id, "dungeon_id": "dungeon-1"},
    )

    assert response.status_code == 401
    assert response.json() == {"detail": "Authentication required"}


def test_session_start_rejects_another_players_id():
    db = _db()
    owner = Player(username="session-owner")
    other = Player(username="session-other")
    db.add_all([owner, other])
    db.commit()

    response = TestClient(_app(db, _principal(owner.player_id))).post(
        "/game/session/start",
        json={"player_id": other.player_id, "dungeon_id": "dungeon-1"},
    )

    assert response.status_code == 403
    assert response.json() == {"detail": "Access denied"}


def test_answer_submit_rejects_another_players_id_before_mutation():
    db = _db()
    owner = Player(username="answer-owner")
    other = Player(username="answer-other")
    db.add_all([owner, other])
    db.commit()

    response = TestClient(_app(db, _principal(owner.player_id))).post(
        "/game/answer/submit",
        json={
            "player_id": other.player_id,
            "question_id": "question-1",
            "player_answer": "answer",
        },
    )

    assert response.status_code == 403
    assert response.json() == {"detail": "Access denied"}
    assert db.query(AnswerSubmission).count() == 0


def test_answer_submit_rejects_question_outside_active_room():
    db = _db()
    owner = Player(username="question-owner")
    dungeon = Dungeon(
        dungeon_id="dungeon-1",
        name="Arrays Dungeon",
        domain="DSA",
        curriculum_slug="dsa-fundamentals",
    )
    room = Room(
        room_id="room-1",
        dungeon_id=dungeon.dungeon_id,
        topic="arrays",
        enemy_count=3,
        is_unlocked=True,
        order_index=0,
    )
    question = Question(
        question_id="question-foreign-topic",
        topic="graphs",
        difficulty="medium",
        question_text="Graph question",
        expected_answer="A graph",
    )
    db.add_all([owner, dungeon, room])
    db.flush()
    session = GameSession(
        session_id="session-1",
        player_id=owner.player_id,
        dungeon_id=dungeon.dungeon_id,
        current_room_id=room.room_id,
        status="active",
    )
    db.add_all([session, question])
    db.commit()

    response = TestClient(_app(db, _principal(owner.player_id))).post(
        "/game/answer/submit",
        json={
            "player_id": owner.player_id,
            "question_id": question.question_id,
            "player_answer": "A graph",
        },
    )

    assert response.status_code == 403
    assert response.json() == {"detail": "Access denied"}
    assert db.query(AnswerSubmission).count() == 0


def test_path_player_write_route_rejects_another_players_id():
    db = _db()
    owner = Player(username="hero-owner")
    other = Player(username="hero-other")
    db.add_all([owner, other])
    db.commit()

    response = TestClient(_app(db, _principal(owner.player_id))).post(
        f"/game/player/{other.player_id}/hero",
        json={"hero_id": "titan_warrior"},
    )

    assert response.status_code == 403
    assert response.json() == {"detail": "Access denied"}
    db.refresh(other)
    assert other.hero_id is None


def test_by_username_lookup_never_leaks_the_full_profile():
    """The one route that stays deliberately unauthenticated (it's the
    pre-token bootstrap step for the demo login -- see routes/dev_auth.py)
    must still never disclose another player's level, XP, hero_id, powerup
    state or accuracy history to a caller who merely knows their username."""
    db = _db()
    player = Player(username="lookup-target", level=7, total_xp=999, hero_id="titan_warrior")
    db.add(player)
    db.flush()  # assigns player.player_id before it's referenced below
    db.add(AccuracyHistory(player_id=player.player_id, topic="arrays", recent_accuracy=0.9))
    db.commit()

    response = TestClient(_app(db)).get(f"/game/player/by-username/{player.username}")

    assert response.status_code == 200
    assert response.json() == {"player_id": player.player_id, "username": player.username}


def test_raid_join_rejects_another_players_id():
    db = _db()
    owner = Player(username="raid-owner")
    other = Player(username="raid-other")
    guild = Guild(name="raid-guild")
    db.add_all([owner, other, guild])
    db.commit()

    response = TestClient(_app(db, _principal(owner.player_id))).post(
        "/game/guild/raid/join",
        json={"guild_id": guild.guild_id, "player_id": other.player_id},
    )

    assert response.status_code == 403
    assert response.json() == {"detail": "Access denied"}
    db.refresh(guild)
    assert guild.raid_active is False