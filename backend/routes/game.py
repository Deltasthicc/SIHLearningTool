"""
Game routes — all /game/ endpoints.
"""
import os
import uuid
import random
import httpx
from datetime import datetime, timedelta, timezone
from fastapi import APIRouter, Depends, HTTPException, Query
from sqlalchemy import func
from sqlalchemy.orm import Session

from db.database import get_db
from models.player import Player
from models.dungeon import Dungeon, Room
from models.session import GameSession
from models.question import Question
from models.submission import AnswerSubmission
from models.accuracy_history import AccuracyHistory
from models.guild import Guild
from routes.authorization import require_own_player_dependency, require_permission_dependency
from schemas.player import PlayerCreate
from schemas.dungeon import (
    DungeonResponse, SessionStartRequest, SessionStartResponse,
    RoomEnterRequest, RoomEnterResponse,
)
from schemas.submission import AnswerSubmitRequest, AnswerSubmitResponse
from schemas.guild import GuildCreate, GuildResponse, GuildJoinRequest, RaidJoinRequest
from services.game_logic import (
    calculate_xp, calculate_level, calculate_damage,
    check_room_clear, update_streak, update_accuracy_history,
    calculate_streak_bonus, BOSS_XP_BONUS, DUNGEON_COMPLETE_BONUS,
    check_dungeon_complete,
)
from services.ai_client import call_generate_question, call_judge_answer, call_next_difficulty, call_next_topic
from services.heroes import (
    HEROES, DEFAULT_HERO_ID, POWERUP_MAX_USES_PER_WINDOW, POWERUP_WINDOW_HOURS, hero_or_default,
)
from services.monsters import monster_name_for
from security.rbac import AuthorizationError, BoundPrincipal, Permission, scoped_to_own_player

router = APIRouter(prefix="/game", tags=["Game"])

MAX_HINT_TOKENS = int(os.getenv("MAX_HINT_TOKENS", "3"))

# Raid boss HP per member -- roughly 3 medium-difficulty hits' worth of damage
# points, preserving the old "3 questions per member" design intent now that
# raid damage is continuous (score-scaled), matching the rest of the app's
# combat model instead of a flat 1-per-correct-answer count.
RAID_BOSS_HP_PER_MEMBER = 270

# Mirrors services/config.py's JUDGE_CORRECT_THRESHOLD/JUDGE_PARTIAL_THRESHOLD
# defaults -- used here only to re-derive a verdict after Shadow Step's score
# boost changes the score post-judging (see submit_answer).
JUDGE_CORRECT_THRESHOLD = float(os.getenv("JUDGE_CORRECT_THRESHOLD", "0.65"))
JUDGE_PARTIAL_THRESHOLD = float(os.getenv("JUDGE_PARTIAL_THRESHOLD", "0.30"))


def _require_body_player(principal: BoundPrincipal, player_id: str) -> None:
    try:
        scoped_to_own_player(principal, player_id)
    except AuthorizationError as exc:
        raise HTTPException(status_code=403, detail="Access denied") from exc


def _require_guild_member(principal: BoundPrincipal, guild: Guild, db: Session) -> None:
    if principal.player_id is None:
        raise HTTPException(status_code=403, detail="Access denied")
    member = db.query(Player).filter(
        Player.player_id == principal.player_id,
        Player.guild_id == guild.guild_id,
    ).first()
    if member is None:
        raise HTTPException(status_code=403, detail="Access denied")


def _verdict_from_score(score: float) -> str:
    if score >= JUDGE_CORRECT_THRESHOLD:
        return "correct"
    if score >= JUDGE_PARTIAL_THRESHOLD:
        return "partial"
    return "incorrect"


# ─── Player Management ───

@router.post("/player/create")
async def create_player(body: PlayerCreate, db: Session = Depends(get_db)):
    """Create a new player with a unique username."""
    existing = db.query(Player).filter(Player.username == body.username).first()
    if existing:
        raise HTTPException(status_code=400, detail="Username already taken")
    player = Player(username=body.username)
    db.add(player)
    db.commit()
    db.refresh(player)
    # A brand-new player has no accuracy_history rows yet (those are created
    # in start_session) -- pass [] rather than querying for rows that can't
    # exist. Serialized via _serialize_player (not response_model=PlayerResponse)
    # so hero_id/powerup fields aren't silently stripped from the response.
    return _serialize_player(player, [])


def _as_utc(dt: datetime | None) -> datetime | None:
    """SQLite doesn't reliably round-trip tzinfo through DateTime(timezone=True)
    -- a value written as UTC can come back naive on the next request's fresh
    query. Every powerup_window_start value this app ever writes is UTC
    (datetime.now(timezone.utc)), so a naive read is safely assumed UTC too."""
    if dt is not None and dt.tzinfo is None:
        return dt.replace(tzinfo=timezone.utc)
    return dt


def _powerup_status(player: Player) -> dict:
    """Read-only view of the cooldown window -- does not mutate/reset it.
    The actual reset-on-expiry write only happens in use_powerup(), which is
    the only place a stale window matters."""
    now = datetime.now(timezone.utc)
    window_start = _as_utc(player.powerup_window_start)
    window_active = (
        window_start is not None
        and (now - window_start).total_seconds() < POWERUP_WINDOW_HOURS * 3600
    )
    uses = player.powerup_uses_this_window or 0 if window_active else 0
    return {
        # Deliberately NOT defaulted to DEFAULT_HERO_ID here: the frontend's
        # register/login redirect uses "is hero_id set?" to decide whether to
        # send a new player to character-select. hero_or_default() (used by
        # use_powerup, and by the frontend's own heroOrDefault() for display)
        # is the right place for the "no hero chosen yet" fallback, not here.
        "hero_id": player.hero_id,
        "powerup_uses_remaining": max(0, POWERUP_MAX_USES_PER_WINDOW - uses),
        "powerup_resets_at": (
            (window_start + timedelta(hours=POWERUP_WINDOW_HOURS)).isoformat()
            if window_active else None
        ),
    }


def _topic_damage_total(db: Session, player_id: str, topic: str) -> int:
    """Cumulative damage dealt to a topic's boss -- read from the running
    total on AccuracyHistory (updated every submit_answer), the single
    source of truth both the in-fight HP bar and the room-clear/unlock-proof
    checks use, so they can never disagree about whether the boss is dead."""
    acc = db.query(AccuracyHistory).filter(
        AccuracyHistory.player_id == player_id, AccuracyHistory.topic == topic,
    ).first()
    return acc.damage_dealt if acc and acc.damage_dealt else 0


def _serialize_player(player: Player, histories: list[AccuracyHistory]) -> dict:
    return {
        "player_id": player.player_id, "username": player.username,
        "level": player.level, "total_xp": player.total_xp,
        "streak_days": player.streak_days,
        "last_active": player.last_active.isoformat() if player.last_active else None,
        "guild_id": player.guild_id,
        # Clamp defensively: hint_tokens can be storing a value from before
        # MAX_HINT_TOKENS was enforced on writes (e.g. this repo's seeded demo
        # player). Clamping on read fixes the display without a DB migration.
        "hint_tokens": min(player.hint_tokens, MAX_HINT_TOKENS),
        "accuracy_history": [
            {
                "topic": h.topic, "attempts": h.attempts, "correct": h.correct,
                "recent_accuracy": h.recent_accuracy, "mastered": h.mastered,
                "damage_dealt": h.damage_dealt or 0,
            }
            for h in histories
        ],
        **_powerup_status(player),
    }


@router.get("/player/{player_id}")
async def get_player(
    player_id: str,
    db: Session = Depends(get_db),
    principal: BoundPrincipal = Depends(
        require_own_player_dependency(Permission.PLAYER_SELF_READ)
    ),
):
    """Fetch a player's stats and per-topic accuracy history."""
    player = db.query(Player).filter(Player.player_id == player_id).first()
    if not player:
        raise HTTPException(status_code=404, detail="Player not found")
    histories = db.query(AccuracyHistory).filter(AccuracyHistory.player_id == player_id).all()
    return _serialize_player(player, histories)


@router.get("/player/by-username/{username}")
async def get_player_by_username(username: str, db: Session = Depends(get_db)):
    """Resolve a username to a player_id — the first step of the demo
    username-only login bootstrap (see routes/dev_auth.py). Deliberately
    unauthenticated: the client has no bearer token yet at this point, so it
    can't be gated behind require_own_player_dependency the way every other
    player route now is.

    Deliberately returns only {player_id, username}, never the full
    _serialize_player() payload — that would let anyone who merely knows or
    guesses a username read that player's level, XP, hero_id, powerup state
    and per-topic accuracy history with no proof of identity at all. The
    frontend uses this player_id to mint a real token via /auth/dev-login,
    then fetches the actual profile through the properly authenticated
    GET /player/{player_id}, which only that bound identity can read.
    """
    player = db.query(Player).filter(Player.username == username).first()
    if not player:
        raise HTTPException(status_code=404, detail="Player not found")
    return {"player_id": player.player_id, "username": player.username}


# ─── Character selection ───

@router.post("/player/{player_id}/hero")
async def select_hero(
    player_id: str,
    body: dict,
    db: Session = Depends(get_db),
    principal: BoundPrincipal = Depends(
        require_own_player_dependency(Permission.PLAYER_SELF_WRITE)
    ),
):
    hero_id = body.get("hero_id")
    if hero_id not in HEROES:
        raise HTTPException(status_code=422, detail=f"Unknown hero: {hero_id!r}")
    player = db.query(Player).filter(Player.player_id == player_id).first()
    if not player:
        raise HTTPException(status_code=404, detail="Player not found")
    player.hero_id = hero_id
    db.commit()
    return {"hero_id": hero_id}


# ─── Powerups ───

@router.post("/powerup/use")
async def use_powerup(
    body: dict,
    db: Session = Depends(get_db),
    principal: BoundPrincipal = Depends(
        require_permission_dependency(Permission.PRACTICE_SELF_WRITE)
    ),
):
    """
    Apply the player's chosen hero's powerup, subject to a rolling
    POWERUP_MAX_USES_PER_WINDOW-per-POWERUP_WINDOW_HOURS cooldown persisted on
    the player row (so it survives refresh/logout, same as hint_tokens).

    Every effect mutates server-authoritative state directly here (XP,
    hint_tokens, or a "pending" flag consumed by the next /answer/submit
    call) -- including force_correct/force_correct_heal (forces the next
    answer's score to a perfect 1.0, dealing that question's full max_damage)
    and score_boost_next (adds a flat boost to the next answer's actual
    score, potentially upgrading its verdict) -- rather than poking an
    arbitrary client-side HP number. This keeps the villain's HP bar
    (hits_required/hits_landed, computed from real cumulative damage) as the
    single source of truth, in sync with the real room-clear condition.
    heal_to_full is the one exception: player HP during a fight has no
    server-side pool at all (see enter_room's boss_max_hp for the enemy
    side's equivalent), so it's still returned as an instruction for the
    frontend to apply to its own client-tracked HP.
    """
    player_id = body.get("player_id")
    _require_body_player(principal, player_id)
    question_id = body.get("question_id")
    player = db.query(Player).filter(Player.player_id == player_id).first()
    if not player:
        raise HTTPException(status_code=404, detail="Player not found")

    hero = hero_or_default(player.hero_id)

    now = datetime.now(timezone.utc)
    window_start = _as_utc(player.powerup_window_start)
    window_expired = (
        window_start is None
        or (now - window_start).total_seconds() >= POWERUP_WINDOW_HOURS * 3600
    )
    if window_expired:
        player.powerup_window_start = now
        player.powerup_uses_this_window = 0
        window_start = now

    if player.powerup_uses_this_window >= POWERUP_MAX_USES_PER_WINDOW:
        resets_at = window_start + timedelta(hours=POWERUP_WINDOW_HOURS)
        raise HTTPException(
            status_code=429,
            detail=f"{hero['powerup_name']} is on cooldown until {resets_at.isoformat()}.",
        )

    response = {
        "hero_id": player.hero_id or DEFAULT_HERO_ID,
        "powerup_name": hero["powerup_name"],
        "effect": hero["effect"],
    }

    effect = hero["effect"]
    if effect == "force_correct":
        player.pending_force_correct = True
        response["queued"] = True
    elif effect == "force_correct_heal":
        player.pending_force_correct = True
        response["queued"] = True
        response["heal_to_full"] = True
    elif effect == "double_xp_next":
        player.pending_xp_multiplier = 2.0
    elif effect == "score_boost_next":
        player.pending_verdict_boost = True
        response["queued"] = True
    elif effect == "free_hint_bonus_xp":
        if not question_id:
            raise HTTPException(status_code=422, detail="question_id is required for this powerup")
        question = db.query(Question).filter(Question.question_id == question_id).first()
        if not question:
            raise HTTPException(status_code=404, detail="Question not found")
        xp = hero["xp"]
        player.total_xp += xp
        player.level = calculate_level(player.total_xp)
        response["hint_text"] = question.hint
        response["xp_awarded"] = xp
    elif effect == "refill_hints":
        player.hint_tokens = MAX_HINT_TOKENS
        response["hint_tokens"] = player.hint_tokens

    player.powerup_uses_this_window += 1
    db.commit()

    response["powerup_uses_remaining"] = POWERUP_MAX_USES_PER_WINDOW - player.powerup_uses_this_window
    response["powerup_resets_at"] = (window_start + timedelta(hours=POWERUP_WINDOW_HOURS)).isoformat()
    return response


# ─── Dungeon & Session ───

@router.get("/dungeons")
async def list_dungeons(
    db: Session = Depends(get_db),
    principal: BoundPrincipal = Depends(
        require_permission_dependency(Permission.PLAYER_SELF_READ)
    ),
):
    """List all available dungeons — needed for dungeon selection screen."""
    dungeons = db.query(Dungeon).all()
    return [
        {
            "dungeon_id": d.dungeon_id, "name": d.name, "domain": d.domain,
            "slug": d.curriculum_slug, "room_count": len(d.rooms),
        }
        for d in dungeons
    ]


@router.get("/dungeon/{dungeon_id}", response_model=DungeonResponse)
async def get_dungeon(
    dungeon_id: str,
    db: Session = Depends(get_db),
    principal: BoundPrincipal = Depends(
        require_permission_dependency(Permission.PLAYER_SELF_READ)
    ),
):
    """Return a dungeon and its rooms."""
    dungeon = db.query(Dungeon).filter(Dungeon.dungeon_id == dungeon_id).first()
    if not dungeon:
        raise HTTPException(status_code=404, detail="Dungeon not found")
    return dungeon


@router.post("/session/start", response_model=SessionStartResponse)
async def start_session(
    body: SessionStartRequest,
    db: Session = Depends(get_db),
    principal: BoundPrincipal = Depends(
        require_permission_dependency(Permission.PRACTICE_SELF_WRITE)
    ),
):
    """Start a dungeon run: seed missing accuracy rows, bump streak, open the first room."""
    _require_body_player(principal, body.player_id)
    player = db.query(Player).filter(Player.player_id == body.player_id).first()
    if not player:
        raise HTTPException(status_code=404, detail="Player not found")
    dungeon = db.query(Dungeon).filter(Dungeon.dungeon_id == body.dungeon_id).first()
    if not dungeon:
        raise HTTPException(status_code=404, detail="Dungeon not found")

    existing_topics = {
        history.topic
        for history in db.query(AccuracyHistory).filter(
            AccuracyHistory.player_id == body.player_id
        ).all()
    }
    for room in dungeon.rooms:
        if room.topic not in existing_topics:
            db.add(AccuracyHistory(player_id=body.player_id, topic=room.topic))

    new_streak, new_last = update_streak(player.last_active, player.streak_days)
    player.streak_days = new_streak
    player.last_active = new_last

    # Only one session should be "active" per player, or submit_answer's
    # unscoped active-session lookup can pick a stale one.
    db.query(GameSession).filter(
        GameSession.player_id == body.player_id, GameSession.status == "active"
    ).update({"status": "abandoned"})

    first_room = db.query(Room).filter(
        Room.dungeon_id == dungeon.dungeon_id, Room.is_unlocked == True
    ).order_by(Room.order_index).first()

    session = GameSession(
        player_id=body.player_id, dungeon_id=body.dungeon_id,
        current_room_id=first_room.room_id if first_room else None,
    )
    db.add(session)
    db.commit()
    db.refresh(session)
    return SessionStartResponse(session_id=session.session_id, dungeon=dungeon, current_room_id=session.current_room_id)


# ─── Room Entry ───

@router.post("/room/enter", response_model=RoomEnterResponse)
async def enter_room(
    body: RoomEnterRequest,
    db: Session = Depends(get_db),
    principal: BoundPrincipal = Depends(
        require_permission_dependency(Permission.PRACTICE_SELF_WRITE)
    ),
):
    """Enter a room, pick a difficulty, and generate its question via AI."""
    session = db.query(GameSession).filter(GameSession.session_id == body.session_id).first()
    if not session:
        raise HTTPException(status_code=404, detail="Session not found")
    _require_body_player(principal, session.player_id)
    room = db.query(Room).filter(Room.room_id == body.room_id).first()
    if not room:
        raise HTTPException(status_code=404, detail="Room not found")
    if not _is_room_unlocked_for_player(db, session.player_id, room, session.dungeon_id):
        raise HTTPException(status_code=400, detail="Room is locked")

    session.current_room_id = room.room_id

    # Get dungeon info for context
    dungeon = db.query(Dungeon).filter(Dungeon.dungeon_id == session.dungeon_id).first()
    domain = dungeon.domain if dungeon else None

    # Boss rooms: force hard difficulty + cycle topics from across the dungeon
    if room.is_boss:
        difficulty = "hard"
        # Pick a random topic from all rooms in this dungeon (multi-topic boss fight)
        all_rooms = db.query(Room).filter(
            Room.dungeon_id == session.dungeon_id, Room.is_boss == False
        ).all()
        question_topic = random.choice(all_rooms).topic if all_rooms else room.topic
    else:
        # Normal rooms: use RL difficulty tuner
        histories = db.query(AccuracyHistory).filter(
            AccuracyHistory.player_id == session.player_id
        ).all()
        accuracy_map = {h.topic: h.recent_accuracy for h in histories}
        try:
            difficulty_data = await call_next_difficulty(session.player_id, room.topic, accuracy_map)
        except httpx.HTTPError:
            difficulty_data = {}
        difficulty = difficulty_data.get("difficulty", "medium")
        question_topic = room.topic

    # The player fights whichever monster is actually shown on screen for
    # this room (the boss fight always shows the final boss, even though its
    # questions are drawn from a randomly-cycled sub-topic above) -- passing
    # this into question generation keeps the AI in character as that one
    # named villain instead of inventing a new monster persona per question.
    monster_name = monster_name_for("boss" if room.is_boss else room.topic)

    try:
        question_data = await call_generate_question(
            session.player_id, question_topic, difficulty, domain, monster_name=monster_name,
        )
    except httpx.HTTPError:
        raise HTTPException(status_code=503, detail="The question generator is unreachable. Try again.")
    question = Question(
        question_id=question_data.get("question_id", str(uuid.uuid4())), topic=room.topic, difficulty=difficulty,
        question_text=question_data.get("question", "Describe this concept."),
        expected_answer=question_data.get("expected_answer", ""),
        hint=question_data.get("hint", ""),
        max_damage=question_data.get("max_damage", 80),
    )
    db.add(question)
    db.commit()

    # `room.enemy_count` is repurposed to store the boss's total HP pool
    # (see db/seed.py) -- the name is a holdover from the old "N correct
    # answers" design, kept to avoid a column rename, but it's damage points
    # now, not a count of anything.
    boss_max_hp = room.enemy_count
    hits_landed = min(boss_max_hp, _topic_damage_total(db, session.player_id, room.topic))
    return RoomEnterResponse(
        room=room,
        question={"question_id": question.question_id, "question": question.question_text,
                  "hint": question.hint, "topic": question.topic, "difficulty": question.difficulty,
                  "max_damage": question.max_damage},
        enemy_hp=boss_max_hp,
        hits_required=boss_max_hp,
        hits_landed=hits_landed,
    )


# ─── Answer Submission (CRITICAL PATH) ───

@router.post("/answer/submit", response_model=AnswerSubmitResponse)
async def submit_answer(
    body: AnswerSubmitRequest,
    db: Session = Depends(get_db),
    principal: BoundPrincipal = Depends(
        require_permission_dependency(Permission.PRACTICE_SELF_WRITE)
    ),
):
    """Judge the answer, award XP/damage, and update accuracy history + room/dungeon progress."""
    _require_body_player(principal, body.player_id)
    player = db.query(Player).filter(Player.player_id == body.player_id).first()
    if not player:
        raise HTTPException(status_code=404, detail="Player not found")
    question = db.query(Question).filter(Question.question_id == body.question_id).first()
    if not question:
        raise HTTPException(status_code=404, detail="Question not found")

    active_session = db.query(GameSession).filter(
        GameSession.player_id == body.player_id,
        GameSession.status == "active",
    ).first()
    if not active_session:
        raise HTTPException(status_code=403, detail="Access denied")
    active_room = db.query(Room).filter(
        Room.room_id == active_session.current_room_id,
    ).first()
    if not active_room or active_room.topic != question.topic:
        raise HTTPException(status_code=403, detail="Access denied")

    # Each generated question is meant to be answered exactly once. Without this
    # guard, a retried/double-clicked request replays XP, damage, and room-clear
    # progress for the same question every time it is resubmitted.
    already_answered = db.query(AnswerSubmission).filter(
        AnswerSubmission.question_id == body.question_id
    ).first()
    if already_answered:
        raise HTTPException(status_code=409, detail="This question has already been answered")

    try:
        judge = await call_judge_answer(body.question_id, body.player_answer, question.expected_answer)
    except httpx.HTTPError:
        raise HTTPException(status_code=503, detail="The judge is unreachable. Try again.")
    score = judge.get("score", 0.0)
    damage_multiplier = judge.get("damage_multiplier", 0.0)
    verdict = judge.get("verdict", "incorrect")
    feedback = judge.get("feedback", "")

    # Consume a pending Titan's Smash/Valkyrie's Charge-style powerup: forces
    # this answer to a perfect (score 1.0) critical hit outright, regardless
    # of what the judge actually said -- dealing this question's full
    # max_damage. Checked before Shadow Step's boost since a forced perfect
    # score is already the strongest possible outcome.
    if player.pending_force_correct:
        verdict = "correct"
        score = 1.0
        damage_multiplier = 2.0
        player.pending_force_correct = False
    # Consume a pending Shadow Step-style powerup: adds a flat boost to this
    # answer's score (capped at a perfect 1.0), then re-derives the verdict
    # from the boosted score -- can turn an incorrect answer partial, or a
    # partial answer correct, rather than a flat tier-jump that ignored how
    # close the original score actually was.
    elif player.pending_verdict_boost:
        boost = hero_or_default(player.hero_id).get("boost", 0.3)
        score = min(1.0, score + boost)
        verdict = _verdict_from_score(score)
        damage_multiplier = {"correct": 2.0, "partial": 1.0, "incorrect": 0.0}[verdict]
        player.pending_verdict_boost = False

    xp_gained = calculate_xp(question.difficulty, verdict, body.response_time_ms)

    # Streak bonus XP
    streak_bonus = calculate_streak_bonus(player.streak_days) if verdict in ("correct", "partial") else 0
    xp_gained += streak_bonus

    # Consume a pending Arcane Surge-style powerup.
    if player.pending_xp_multiplier and player.pending_xp_multiplier != 1.0:
        xp_gained = int(xp_gained * player.pending_xp_multiplier)
        player.pending_xp_multiplier = 1.0

    # Damage scales continuously with the judge's actual score against this
    # specific question's max_damage ceiling -- a perfect score deals full
    # damage, a 0.7 deals 70% of it, and a harder question's higher ceiling
    # always outdamages an easier one at the same score. Only "incorrect"
    # deals zero; "partial" still chips in proportionally to its score.
    damage_dealt = 0 if verdict == "incorrect" else calculate_damage(question.max_damage, score)
    original_level = player.level
    player.total_xp += xp_gained
    player.level = calculate_level(player.total_xp)

    old_streak = player.streak_days
    new_streak, new_last = update_streak(player.last_active, player.streak_days)
    player.streak_days = new_streak
    player.last_active = new_last

    # Hint replenishment: +1 token every time streak crosses a multiple of 5
    if new_streak > 0 and new_streak % 5 == 0 and old_streak < new_streak:
        player.hint_tokens = min(MAX_HINT_TOKENS, player.hint_tokens + 1)

    # *** CRITICAL: Update accuracy_history ***
    acc = db.query(AccuracyHistory).filter(
        AccuracyHistory.player_id == body.player_id, AccuracyHistory.topic == question.topic
    ).first()
    if not acc:
        acc = AccuracyHistory(player_id=body.player_id, topic=question.topic)
        db.add(acc)
        db.flush()

    new_l5, new_att, new_cor, new_acc = update_accuracy_history(
        acc.last_5_results or [], verdict, acc.attempts, acc.correct
    )
    acc.last_5_results = new_l5
    acc.attempts = new_att
    acc.correct = new_cor
    acc.recent_accuracy = new_acc
    acc.damage_dealt = (acc.damage_dealt or 0) + damage_dealt
    # One-way ratchet -- never unset once true. last_5_results is a rolling
    # window, so recent_accuracy can legitimately dip back below the unlock
    # threshold later; without this, a room the player already unlocked
    # (and may already be mid-way through) could silently re-lock behind
    # them because of an unrelated later question.
    if new_acc > 0.65:
        acc.mastered = True

    submission = AnswerSubmission(
        player_id=body.player_id, question_id=body.question_id,
        player_answer=body.player_answer, score=score, damage_multiplier=damage_multiplier,
        damage_dealt=damage_dealt, verdict=verdict, response_time_ms=body.response_time_ms,
    )
    db.add(submission)
    db.flush()

    # Check room clear
    room_cleared = False
    dungeon_completed = False
    hits_required = None
    hits_landed = None
    active_session = db.query(GameSession).filter(
        GameSession.player_id == body.player_id, GameSession.status == "active"
    ).order_by(GameSession.started_at.desc()).first()
    if active_session and active_session.current_room_id:
        room = db.query(Room).filter(Room.room_id == active_session.current_room_id).first()
        if room:
            # `room.enemy_count` is repurposed to store the boss's total HP
            # pool (see db/seed.py) -- damage points now, not a hit count.
            boss_max_hp = room.enemy_count
            topic_damage = _topic_damage_total(db, body.player_id, room.topic)
            hits_required = boss_max_hp
            hits_landed = min(boss_max_hp, topic_damage)
            room_cleared = check_room_clear(topic_damage, boss_max_hp)
            if room_cleared:
                # Check dungeon completion — all rooms cleared? One query
                # across every topic's running damage total instead of one
                # per room.
                all_rooms = db.query(Room).filter(Room.dungeon_id == active_session.dungeon_id).all()
                damage_by_topic = {
                    h.topic: h.damage_dealt or 0
                    for h in db.query(AccuracyHistory).filter(
                        AccuracyHistory.player_id == body.player_id
                    ).all()
                }
                room_statuses = [(damage_by_topic.get(r.topic, 0), r.enemy_count) for r in all_rooms]

                dungeon_completed = check_dungeon_complete(room_statuses)
                if dungeon_completed:
                    active_session.status = "completed"
                    # Boss defeat bonus if boss room
                    if room.is_boss:
                        xp_gained += BOSS_XP_BONUS
                        player.total_xp += BOSS_XP_BONUS
                    # Dungeon completion bonus
                    xp_gained += DUNGEON_COMPLETE_BONUS
                    player.total_xp += DUNGEON_COMPLETE_BONUS
                    player.level = calculate_level(player.total_xp)
                    # Hint replenishment: +1 on dungeon complete
                    player.hint_tokens = min(MAX_HINT_TOKENS, player.hint_tokens + 1)

    # Computed last so a dungeon-completion bonus that pushes the player up an
    # extra level (on top of the base XP gain) is reflected in the response.
    new_level = player.level if player.level > original_level else None

    db.commit()
    return AnswerSubmitResponse(
        submission_id=submission.submission_id, score=score, damage_multiplier=damage_multiplier,
        verdict=verdict, feedback=feedback, xp_gained=xp_gained, damage_dealt=damage_dealt,
        room_cleared=room_cleared, new_level=new_level,
        dungeon_completed=dungeon_completed,
        hits_required=hits_required, hits_landed=hits_landed,
    )


def _is_room_unlocked_for_player(
    db: Session, player_id: str, room: Room, dungeon_id: str
) -> bool:
    from services.knowledge_graph import TOPIC_GRAPH

    histories = db.query(AccuracyHistory).filter(
        AccuracyHistory.player_id == player_id
    ).all()
    # `mastered` is a one-way ratchet (see AccuracyHistory model) -- a topic
    # counts as proven if it's mastered OR currently above the threshold, so
    # a later dip in the rolling recent_accuracy window can never re-lock a
    # room the player already legitimately opened.
    proven_by_accuracy = {h.topic for h in histories if h.mastered or h.recent_accuracy > 0.65}
    damage_by_topic = {h.topic: h.damage_dealt or 0 for h in histories}

    all_rooms = db.query(Room).filter(Room.dungeon_id == dungeon_id).all()
    # `enemy_count` is repurposed to store each room's boss HP pool (see
    # db/seed.py) -- damage points now, not a hit count.
    boss_max_hp_by_topic = {r.topic: r.enemy_count for r in all_rooms}

    def is_proven(topic: str) -> bool:
        if topic in proven_by_accuracy:
            return True
        # A player who actually clears a room (enough cumulative damage to
        # drop its boss to 0 HP) has earned the unlock regardless of what
        # their rolling last-5 accuracy says -- recent_accuracy and "did I
        # beat this room's villain" are different measures that can diverge,
        # and a cleared room should never stay a locked gate for downstream
        # topics just because of a rough patch mixed into that clear.
        required = boss_max_hp_by_topic.get(topic)
        return required is not None and required > 0 and damage_by_topic.get(topic, 0) >= required

    if room.is_boss:
        topic_rooms = [r for r in all_rooms if not r.is_boss]
        return all(is_proven(candidate.topic) for candidate in topic_rooms)

    prerequisites = TOPIC_GRAPH.get(room.topic)
    if prerequisites is None:
        # Not a DSA topic -- look it up in the cross-domain competency
        # catalog instead (services/curricula.py) rather than defaulting to
        # permanently locked. Without this, every non-DSA dungeon seeded by
        # db/seed.py's seed_curricula_dungeons() would be unenterable: this
        # function is the live gate /game/room/enter checks on every call.
        from services.curricula import curriculum_for_topic

        _, curriculum = curriculum_for_topic(room.topic)
        competency = next(
            (c for c in curriculum["competencies"] if c["id"] == room.topic),
            None,
        ) if curriculum else None
        prerequisites = competency["prerequisites"] if competency else None
    if prerequisites is None:
        return False
    return all(is_proven(topic) for topic in prerequisites)


# ─── Next Topic Routing (Knowledge Graph AI) ───

@router.get("/dungeon/{dungeon_id}/next-topic")
async def get_next_topic_for_player(
    dungeon_id: str,
    player_id: str,
    db: Session = Depends(get_db),
    principal: BoundPrincipal = Depends(
        require_permission_dependency(Permission.PRACTICE_SELF_WRITE)
    ),
):
    """Use the knowledge graph AI to recommend the next room/topic for a player."""
    _require_body_player(principal, player_id)
    player = db.query(Player).filter(Player.player_id == player_id).first()
    if not player:
        raise HTTPException(status_code=404, detail="Player not found")
    dungeon = db.query(Dungeon).filter(Dungeon.dungeon_id == dungeon_id).first()
    if not dungeon:
        raise HTTPException(status_code=404, detail="Dungeon not found")

    # Build accuracy map
    histories = db.query(AccuracyHistory).filter(
        AccuracyHistory.player_id == player_id
    ).all()
    accuracy_map = {h.topic: h.recent_accuracy for h in histories}

    # Call AI graph routing
    result = await call_next_topic(player_id, accuracy_map)
    next_topic = result.get("next_topic", "arrays")
    weak_topics = result.get("weak_topics", [])

    # Find the corresponding room in this dungeon
    recommended_room = db.query(Room).filter(
        Room.dungeon_id == dungeon_id, Room.topic == next_topic
    ).first()

    return {
        "next_topic": next_topic,
        "weak_topics": weak_topics,
        "recommended_room": {
            "room_id": recommended_room.room_id,
            "topic": recommended_room.topic,
            "is_unlocked": recommended_room.is_unlocked,
            "is_boss": recommended_room.is_boss,
        } if recommended_room else None,
    }


# ─── Hint System ───

@router.post("/hint/use")
async def use_hint(
    body: dict,
    db: Session = Depends(get_db),
    principal: BoundPrincipal = Depends(
        require_permission_dependency(Permission.PRACTICE_SELF_WRITE)
    ),
):
    """Spend one hint token to reveal a question's hint."""
    player_id = body.get("player_id")
    _require_body_player(principal, player_id)
    player = db.query(Player).filter(Player.player_id == player_id).first()
    if not player:
        raise HTTPException(status_code=404, detail="Player not found")
    if player.hint_tokens <= 0:
        raise HTTPException(status_code=400, detail="No hint tokens remaining")
    question = db.query(Question).filter(Question.question_id == body.get("question_id")).first()
    if not question:
        raise HTTPException(status_code=404, detail="Question not found")
    player.hint_tokens -= 1
    db.commit()
    return {"hint": question.hint, "hint_tokens_remaining": player.hint_tokens}


# ─── Leaderboard ───

@router.get("/leaderboard")
async def get_leaderboard(
    limit: int = Query(10, ge=1, le=100),
    offset: int = Query(0, ge=0),
    db: Session = Depends(get_db),
    principal: BoundPrincipal = Depends(
        require_permission_dependency(Permission.PLAYER_SELF_READ)
    ),
):
    """Rank players by total XP."""
    players = db.query(Player).order_by(Player.total_xp.desc()).offset(offset).limit(limit).all()
    return [{"rank": offset + i + 1, "player_id": p.player_id,
             "username": p.username, "level": p.level,
             "total_xp": p.total_xp, "streak_days": p.streak_days,
             "hero_id": p.hero_id} for i, p in enumerate(players)]


@router.get("/leaderboard/guild")
async def get_guild_leaderboard(
    limit: int = Query(10, ge=1, le=100),
    db: Session = Depends(get_db),
    principal: BoundPrincipal = Depends(
        require_permission_dependency(Permission.PLAYER_SELF_READ)
    ),
):
    """Guild leaderboard — ranked by combined member XP."""
    from sqlalchemy import func
    results = (
        db.query(
            Guild.guild_id, Guild.name,
            func.count(Player.player_id).label("member_count"),
            func.sum(Player.total_xp).label("combined_xp"),
        )
        .join(Player, Player.guild_id == Guild.guild_id)
        .group_by(Guild.guild_id, Guild.name)
        .order_by(func.sum(Player.total_xp).desc())
        .limit(limit)
        .all()
    )
    return [
        {"rank": i + 1, "guild_id": r.guild_id, "name": r.name,
         "member_count": r.member_count, "combined_xp": r.combined_xp or 0}
        for i, r in enumerate(results)
    ]


# ─── Guild ───

@router.post("/guild/create", response_model=GuildResponse)
async def create_guild(
    body: GuildCreate,
    db: Session = Depends(get_db),
    principal: BoundPrincipal = Depends(
        require_permission_dependency(Permission.PRACTICE_SELF_WRITE)
    ),
):
    """Create a guild and make the creator its first member."""
    _require_body_player(principal, body.creator_player_id)
    player = db.query(Player).filter(Player.player_id == body.creator_player_id).first()
    if not player:
        raise HTTPException(status_code=404, detail="Player not found")
    if db.query(Guild).filter(Guild.name == body.name).first():
        raise HTTPException(status_code=400, detail="Guild name taken")
    guild = Guild(name=body.name)
    db.add(guild)
    db.flush()
    player.guild_id = guild.guild_id
    db.commit()
    db.refresh(guild)
    return GuildResponse(guild_id=guild.guild_id, name=guild.name,
                         members=[{"player_id": player.player_id, "username": player.username}],
                         raid_active=False)

@router.post("/guild/join")
async def join_guild(
    body: GuildJoinRequest,
    db: Session = Depends(get_db),
    principal: BoundPrincipal = Depends(
        require_permission_dependency(Permission.PRACTICE_SELF_WRITE)
    ),
):
    """Add a player to an existing guild."""
    _require_body_player(principal, body.player_id)
    player = db.query(Player).filter(Player.player_id == body.player_id).first()
    if not player:
        raise HTTPException(status_code=404, detail="Player not found")
    guild = db.query(Guild).filter(Guild.guild_id == body.guild_id).first()
    if not guild:
        raise HTTPException(status_code=404, detail="Guild not found")
    player.guild_id = guild.guild_id
    db.commit()
    return {"message": "Joined guild", "guild_id": guild.guild_id}

@router.post("/guild/raid/join")
async def join_raid(
    body: RaidJoinRequest,
    db: Session = Depends(get_db),
    principal: BoundPrincipal = Depends(
        require_permission_dependency(Permission.PRACTICE_SELF_WRITE)
    ),
):
    """Join (or start) the guild's raid and assign the player their weakest topic."""
    _require_body_player(principal, body.player_id)
    guild = db.query(Guild).filter(Guild.guild_id == body.guild_id).first()
    if not guild:
        raise HTTPException(status_code=404, detail="Guild not found")
    player = db.query(Player).filter(Player.player_id == body.player_id).first()
    if not player or player.guild_id != guild.guild_id:
        raise HTTPException(status_code=400, detail="Player not in this guild")

    if not guild.raid_active:
        guild.raid_active = True
        guild.raid_boss_id = str(uuid.uuid4())
        members = db.query(Player).filter(Player.guild_id == guild.guild_id).all()
        guild.raid_boss_hp = len(members) * RAID_BOSS_HP_PER_MEMBER
        guild.raid_boss_damage = 0

    # Assign topic to player based on weakest area
    histories = db.query(AccuracyHistory).filter(
        AccuracyHistory.player_id == body.player_id
    ).all()
    if histories:
        weakest = min(histories, key=lambda h: h.recent_accuracy)
        assigned_topic = weakest.topic
    else:
        assigned_topic = "arrays"  # default for new players

    assignments = dict(guild.raid_topic_assignments or {})
    assignments[body.player_id] = assigned_topic
    guild.raid_topic_assignments = assignments

    db.commit()
    return {
        "message": "Joined raid", "raid_boss_id": guild.raid_boss_id,
        "raid_active": True, "assigned_topic": assigned_topic,
        "raid_boss_hp": guild.raid_boss_hp,
        "raid_boss_damage": guild.raid_boss_damage,
    }


@router.post("/guild/raid/submit")
async def submit_raid_answer(
    body: dict,
    db: Session = Depends(get_db),
    principal: BoundPrincipal = Depends(
        require_permission_dependency(Permission.PRACTICE_SELF_WRITE)
    ),
):
    """Submit an answer during a guild raid. Tracks combined damage to raid boss."""
    guild_id = body.get("guild_id")
    player_id = body.get("player_id")
    question_id = body.get("question_id")
    player_answer = body.get("player_answer", "")
    response_time_ms = body.get("response_time_ms", 0)

    _require_body_player(principal, player_id)

    guild = db.query(Guild).filter(Guild.guild_id == guild_id).first()
    if not guild or not guild.raid_active:
        raise HTTPException(status_code=400, detail="No active raid")
    player = db.query(Player).filter(Player.player_id == player_id).first()
    if not player or player.guild_id != guild.guild_id:
        raise HTTPException(status_code=400, detail="Player not in this guild")
    question = db.query(Question).filter(Question.question_id == question_id).first()
    if not question:
        raise HTTPException(status_code=404, detail="Question not found")

    # Judge the answer
    judge = await call_judge_answer(question_id, player_answer, question.expected_answer)
    verdict = judge["verdict"]
    score = judge["score"]

    # Award XP to the player
    xp_gained = calculate_xp(question.difficulty, verdict, response_time_ms)
    player.total_xp += xp_gained
    player.level = calculate_level(player.total_xp)

    # Deal damage to raid boss -- same continuous, score-scaled model as the
    # solo dungeon (see submit_answer): a perfect score deals this question's
    # full max_damage, "partial" still chips in proportionally, "incorrect"
    # deals zero. Matches the rest of the app instead of a flat 1-per-correct
    # count against a "3 questions per member" HP pool.
    damage = 0 if verdict == "incorrect" else calculate_damage(question.max_damage, score)
    guild.raid_boss_damage = (guild.raid_boss_damage or 0) + damage

    raid_complete = guild.raid_boss_damage >= guild.raid_boss_hp
    if raid_complete:
        guild.raid_active = False
        # Bonus XP for all guild members on raid success
        raid_members = db.query(Player).filter(Player.guild_id == guild.guild_id).all()
        for m in raid_members:
            m.total_xp += 50  # raid completion bonus
            m.level = calculate_level(m.total_xp)

    db.commit()
    return {
        "verdict": verdict, "score": score, "xp_gained": xp_gained,
        "raid_boss_hp": guild.raid_boss_hp,
        "raid_boss_damage": guild.raid_boss_damage,
        "raid_complete": raid_complete,
        "feedback": judge.get("feedback", ""),
    }


@router.get("/guild/raid/status")
async def get_raid_status(
    guild_id: str,
    db: Session = Depends(get_db),
    principal: BoundPrincipal = Depends(
        require_permission_dependency(Permission.PLAYER_SELF_READ)
    ),
):
    """Get current raid state for a guild."""
    guild = db.query(Guild).filter(Guild.guild_id == guild_id).first()
    if not guild:
        raise HTTPException(status_code=404, detail="Guild not found")
    _require_guild_member(principal, guild, db)
    members = db.query(Player).filter(Player.guild_id == guild_id).all()
    return {
        "guild_id": guild.guild_id,
        "raid_active": guild.raid_active,
        "raid_boss_id": guild.raid_boss_id,
        "raid_boss_hp": guild.raid_boss_hp or 0,
        "raid_boss_damage": guild.raid_boss_damage or 0,
        "topic_assignments": guild.raid_topic_assignments or {},
        "members": [{"player_id": m.player_id, "username": m.username} for m in members],
    }


@router.get("/guild/{guild_id}", response_model=GuildResponse)
async def get_guild(
    guild_id: str,
    db: Session = Depends(get_db),
    principal: BoundPrincipal = Depends(
        require_permission_dependency(Permission.PLAYER_SELF_READ)
    ),
):
    """Fetch a guild and its members."""
    guild = db.query(Guild).filter(Guild.guild_id == guild_id).first()
    if not guild:
        raise HTTPException(status_code=404, detail="Guild not found")
    _require_guild_member(principal, guild, db)
    members = db.query(Player).filter(Player.guild_id == guild_id).all()
    return GuildResponse(guild_id=guild.guild_id, name=guild.name,
                         members=[{"player_id": m.player_id, "username": m.username} for m in members],
                         raid_active=guild.raid_active, raid_boss_id=guild.raid_boss_id)
