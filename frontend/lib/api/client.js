// The single browser-to-backend boundary. Components and stores consume the
// stable frontend view models below and never depend on FastAPI wire shapes.

import { API_BASE_URL } from '../config';
import { TOPIC_GRAPH, TOPIC_LABELS } from '../statMap';
import { monsterForTopic } from '../sprites/monsterSprites';

const SESSION_KEY = 'prism-api-session';

let live = {
  playerId: null,
  sessionId: null,
  dungeon: null,
  combat: null,
  // A real, verified Keycloak bearer token -- see routes/dev_auth.py.
  // Absent whenever ENABLE_DEV_LOGIN isn't set on the backend (the default),
  // in which case /learning/* calls correctly 401 until the real browser
  // OIDC/PKCE flow exists (README.md, SIH26101_MASTER_CHECKLIST.md 5.1).
  authToken: null,
};

function hydrateLiveState() {
  if (typeof window === 'undefined') return;
  try {
    live = { ...live, ...JSON.parse(window.localStorage.getItem(SESSION_KEY) || '{}') };
  } catch {
    window.localStorage.removeItem(SESSION_KEY);
  }
}

function persistLiveState() {
  if (typeof window !== 'undefined') {
    window.localStorage.setItem(SESSION_KEY, JSON.stringify(live));
  }
}

function clearLiveState() {
  live = { playerId: null, sessionId: null, dungeon: null, combat: null, authToken: null };
  if (typeof window !== 'undefined') window.localStorage.removeItem(SESSION_KEY);
}

hydrateLiveState();

const inFlightRequests = new Map();

// Collapses concurrent calls sharing the same key into a single underlying
// request. Without this, React StrictMode's dev-only double-invoke of
// effects (and any accidental fast double-click) fires two full round trips
// through the AI service for one logical action -- doubling latency and
// burning through Gemini's free-tier quota twice as fast.
function dedupe(key, run) {
  if (inFlightRequests.has(key)) return inFlightRequests.get(key);
  const promise = run().finally(() => inFlightRequests.delete(key));
  inFlightRequests.set(key, promise);
  return promise;
}

async function request(path, { method = 'GET', body, headers } = {}) {
  let response;
  try {
    response = await fetch(`${API_BASE_URL}${path}`, {
      method,
      headers: {
        'Content-Type': 'application/json',
        ...(live.authToken ? { Authorization: `Bearer ${live.authToken}` } : {}),
        ...headers,
      },
      body: body !== undefined ? JSON.stringify(body) : undefined,
    });
  } catch (cause) {
    const error = new Error('Could not reach the backend. Is it running?');
    error.code = 0;
    error.cause = cause;
    throw error;
  }

  const data = await response.json().catch(() => ({}));
  if (!response.ok) {
    const detail = Array.isArray(data?.detail)
      ? data.detail.map((item) => item.msg).join(', ')
      : data?.detail;
    const error = new Error(data?.error || detail || `Request failed (${response.status})`);
    error.code = data?.code ?? response.status;
    throw error;
  }
  return data;
}

function rememberPlayer(player) {
  live.playerId = player.player_id;
  persistLiveState();
  return { player };
}

async function currentPlayer() {
  hydrateLiveState();
  if (!live.playerId) {
    const error = new Error('Not authenticated');
    error.code = 401;
    throw error;
  }
  // fetchMe() fires after every answer submission (see app/combat, app/boss)
  // plus once on app mount -- dedupe concurrent calls for the same player.
  return dedupe(`current-player:${live.playerId}`, () => request(`/game/player/${live.playerId}`));
}

function accuracyMap(player) {
  return Object.fromEntries(
    (player.accuracy_history || []).map((entry) => [entry.topic, entry.recent_accuracy])
  );
}

function damageDealtMap(player) {
  return Object.fromEntries(
    (player.accuracy_history || []).map((entry) => [entry.topic, entry.damage_dealt ?? 0])
  );
}

// A topic counts as "proven" (satisfies downstream prerequisites) either of
// two ways: the accuracy ratchet (mastered, or currently above the unlock
// threshold), or having actually cleared that topic's own room -- enough
// cumulative damage to drop its boss to 0 HP (room.enemy_count, repurposed
// as the boss's total HP pool). Without the second path, a player who
// clears a room via a rough patch mixed into an otherwise-winning run (60%
// rolling accuracy, since recent_accuracy is a last-5 window, not a
// room-clear measure) would find downstream rooms still locked even though
// the room's villain is dead and the victory screen already fired.
function provenMap(player, rooms) {
  const damageDealt = damageDealtMap(player);
  const bossMaxHpByTopic = Object.fromEntries((rooms || []).map((r) => [r.topic, r.enemy_count]));
  return Object.fromEntries(
    (player.accuracy_history || []).map((entry) => {
      const accuracyProven = Boolean(entry.mastered) || entry.recent_accuracy > 0.65;
      const required = bossMaxHpByTopic[entry.topic];
      const cleared = typeof required === 'number' && required > 0 && damageDealt[entry.topic] >= required;
      return [entry.topic, accuracyProven || cleared];
    })
  );
}

function normalizeDungeon(dungeon, player) {
  const accuracies = accuracyMap(player);
  const damageDealt = damageDealtMap(player);
  const proven = provenMap(player, dungeon.rooms);
  const rooms = (dungeon.rooms || []).filter((room) => room.topic in TOPIC_GRAPH).map((room) => {
    const recentAccuracy = accuracies[room.topic] ?? 0;
    // The map tile's percentage is room-clear progress (cumulative damage
    // dealt vs. the boss's HP pool), not rolling accuracy -- a player who
    // finishes a room expects to see it read as done, and recent_accuracy
    // (a last-5-answers window) doesn't reliably reach 100% just because
    // the boss actually died.
    const completion = room.enemy_count > 0
      ? Math.min(1, (damageDealt[room.topic] ?? 0) / room.enemy_count)
      : 0;
    const prerequisites = TOPIC_GRAPH[room.topic] || [];
    const isUnlocked = prerequisites.every((topic) => proven[topic]);
    let status = 'unlocked';
    if (!isUnlocked) status = 'locked';
    // A fully-cleared room always reads as gold/"mastered", regardless of
    // rolling accuracy -- otherwise a room a player just beat could still
    // show the same teal "in progress" border as one they haven't touched.
    else if (completion >= 1) status = 'mastered';
    else if (recentAccuracy >= 0.9) status = 'mastered';
    else if (recentAccuracy > 0 && recentAccuracy < 0.5) status = 'weak';

    return {
      ...room,
      label: TOPIC_LABELS[room.topic] || room.topic,
      status,
      recent_accuracy: recentAccuracy,
      completion,
      prerequisites,
    };
  });
  const candidates = rooms.filter((room) => room.status !== 'locked' && !room.is_boss);
  const nextRoom = candidates.sort((a, b) => a.recent_accuracy - b.recent_accuracy)[0];

  return {
    dungeon_id: dungeon.dungeon_id,
    name: dungeon.name,
    domain: dungeon.domain,
    rooms,
    next_topic: nextRoom?.topic ?? null,
    boss_unlocked: Object.keys(TOPIC_GRAPH).every((topic) => proven[topic]),
  };
}

async function startDungeonSession(requestedDungeonId) {
  // Keyed on a fixed name, not requestedDungeonId: getDungeon('') and
  // enterRoom()'s no-session fallback can both call this for the same
  // player at nearly the same time and must collapse into one session.
  return dedupe('session-start', async () => {
    const player = await currentPlayer();
    let dungeon;
    try {
      dungeon = await request(`/game/dungeon/${requestedDungeonId}`);
    } catch (error) {
      if (error.code !== 404) throw error;
      const available = await request('/game/dungeons');
      if (!available.length) throw new Error('No dungeon has been seeded.');
      dungeon = await request(`/game/dungeon/${available[0].dungeon_id}`);
    }

    const session = await request('/game/session/start', {
      method: 'POST',
      body: { player_id: player.player_id, dungeon_id: dungeon.dungeon_id },
    });
    live.sessionId = session.session_id;
    live.dungeon = dungeon;
    persistLiveState();
    return normalizeDungeon(dungeon, player);
  });
}

// Best-effort only: obtains a real verified bearer token from
// routes/dev_auth.py, a local-dev-only bridge (see that file's docstring and
// .env.example's ENABLE_DEV_LOGIN). If the backend hasn't enabled it, or a
// local Keycloak isn't running, this fails silently -- but as of Lane 5's
// game-route auth pass, almost every /game/* endpoint now 401s without a
// token too (only player/create and player/by-username stay open), and
// /ai_real.py has no auth at all yet either way. /learning/* correctly keeps
// 401ing until the real browser OIDC/PKCE flow exists (README.md,
// SIH26101_MASTER_CHECKLIST.md 5.1). Never let this block or fail the demo
// login itself -- but be aware local Keycloak is now required for nearly
// the entire demo to function, not just the professional/Academy side.
async function tryDevLogin(playerId) {
  try {
    const { access_token: accessToken } = await request('/auth/dev-login', {
      method: 'POST',
      body: { player_id: playerId },
    });
    live.authToken = accessToken;
    persistLiveState();
  } catch (e) {
    live.authToken = null;
    if (typeof window !== 'undefined') {
      console.warn(
        '[auth] Continuing without a /learning/* bearer token -- local dev login unavailable:',
        e.message
      );
    }
  }
}

export const auth = {
  register: (username) =>
    request('/game/player/create', { method: 'POST', body: { username } })
      .then(rememberPlayer)
      .then(async (result) => {
        await tryDevLogin(result.player.player_id);
        return result;
      }),

  // /game/player/by-username/{username} deliberately returns only
  // {player_id, username} (see routes/game.py) -- it's the unauthenticated
  // bootstrap step, not a source of profile data. Mint a real token via
  // dev-login, then fetch the actual profile through the properly
  // authenticated GET /player/{player_id} (currentPlayer()), which only the
  // now-bound identity can read.
  login: (username) =>
    request(`/game/player/by-username/${encodeURIComponent(username)}`).then(
      async ({ player_id: playerId }) => {
        await tryDevLogin(playerId);
        live.playerId = playerId;
        persistLiveState();
        return { player: await currentPlayer() };
      }
    ),

  logout: async () => {
    clearLiveState();
    return { ok: true };
  },

  me: async () => rememberPlayer(await currentPlayer()),

  setHero: async (playerId, heroId) =>
    request(`/game/player/${playerId}/hero`, { method: 'POST', body: { hero_id: heroId } }),
};

export const game = {
  getDungeon: (dungeonId) => startDungeonSession(dungeonId),

  // Raw dungeon list (id/name/domain/slug/room_count) -- unlike getDungeon(),
  // does not start a session or fetch rooms. The Academy uses this purely to
  // map a curriculum slug (services/curricula.py) to its "START QUEST" link.
  listDungeons: () => request('/game/dungeons'),

  enterRoom: async (topic) => {
    // Each call to enterRoom(topic) triggers a real (potentially multi-second)
    // Gemini question-generation round trip -- collapse duplicate concurrent
    // calls for the same topic into one.
    return dedupe(`enterRoom:${topic}`, async () => {
      hydrateLiveState();
      if (!live.sessionId || !live.dungeon) await startDungeonSession('');
      const room =
        topic === 'boss'
          ? live.dungeon.rooms.find((candidate) => candidate.is_boss)
          : live.dungeon.rooms.find((candidate) => candidate.topic === topic);
      if (!room) throw new Error(`No room exists for ${topic}.`);

      const response = await request('/game/room/enter', {
        method: 'POST',
        body: { session_id: live.sessionId, room_id: room.room_id },
      });
      // hits_required/hits_landed are recomputed server-side from real
      // AnswerSubmission rows on every call -- always the ground truth, so
      // there's no client-side "is this a continuing fight?" guess to get
      // wrong. The HP bar is hits remaining, not the old flat difficulty HP
      // pool, so it always reaches empty exactly when the room clears.
      const enemyHp = response.hits_required - response.hits_landed;
      live.combat = {
        roomId: room.room_id,
        topic,
        enemyHp,
        enemyHpMax: response.hits_required,
        playerHp: live.combat?.roomId === room.room_id ? live.combat.playerHp : 100,
      };
      persistLiveState();
      return {
        ...response.question,
        enemy_hp: enemyHp,
        enemy_hp_max: live.combat.enemyHpMax,
        enemy_name: monsterForTopic(topic).name,
      };
    });
  },

  submitAnswer: async (payload) => {
    const player = await currentPlayer();
    const result = await request('/game/answer/submit', {
      method: 'POST',
      body: { ...payload, player_id: player.player_id },
    });
    const damageTaken = result.verdict === 'incorrect' ? 18 : result.verdict === 'partial' ? 8 : 0;
    live.combat = live.combat || { enemyHp: 0, enemyHpMax: 0, playerHp: 100 };
    if (typeof result.hits_required === 'number') {
      live.combat.enemyHpMax = result.hits_required;
      live.combat.enemyHp = Math.max(0, result.hits_required - result.hits_landed);
    }
    live.combat.playerHp = Math.max(0, live.combat.playerHp - damageTaken);
    persistLiveState();
    return {
      ...result,
      player_hp_after: live.combat.playerHp,
      enemy_hp_after: live.combat.enemyHp,
    };
  },

  getPlayer: async (playerId) => {
    const player = await request(`/game/player/${playerId}`);
    return { ...player, topic_accuracies: accuracyMap(player) };
  },

  useHint: async (playerId, questionId) =>
    request('/game/hint/use', {
      method: 'POST',
      body: { player_id: playerId, question_id: questionId },
    }),

  joinGuildRaid: async (guildId) => {
    const player = await currentPlayer();
    let activeGuildId = guildId || player.guild_id;
    if (!activeGuildId) {
      const created = await request('/game/guild/create', {
        method: 'POST',
        body: { name: `${player.username}'s Guild`, creator_player_id: player.player_id },
      });
      activeGuildId = created.guild_id;
    }
    const joined = await request('/game/guild/raid/join', {
      method: 'POST',
      body: { guild_id: activeGuildId, player_id: player.player_id },
    });
    const [guild, status] = await Promise.all([
      request(`/game/guild/${activeGuildId}`),
      request(`/game/guild/raid/status?guild_id=${activeGuildId}`),
    ]);
    return {
      ...guild,
      raid_active: joined.raid_active,
      raid_boss_hp: Math.max(0, status.raid_boss_hp - status.raid_boss_damage),
      raid_boss_hp_max: status.raid_boss_hp,
      members: status.members.map((member) => ({
        ...member,
        topic: status.topic_assignments[member.player_id] || 'arrays',
      })),
    };
  },

  getLeaderboard: async () => ({ leaderboard: await request('/game/leaderboard') }),

  respawn: async () => {
    live.combat = null;
    persistLiveState();
    return { ok: true };
  },

  usePowerup: async (playerId, questionId) => {
    const result = await request('/game/powerup/use', {
      method: 'POST',
      body: { player_id: playerId, question_id: questionId },
    });
    // force_correct/force_correct_heal don't touch HP immediately -- they
    // queue a guaranteed-correct verdict the backend applies on the next
    // /answer/submit, which then reports the real hits_required/hits_landed.
    // heal_to_full is the one immediate effect (player HP has no server pool).
    if (live.combat && result.heal_to_full) {
      live.combat.playerHp = live.combat.playerHpMax ?? 100;
      persistLiveState();
    }
    return { ...result, enemy_hp_after: live.combat?.enemyHp, player_hp_after: live.combat?.playerHp };
  },
};

export const ai = {
  getDashboard: async (playerId) => {
    const data = await request(`/ai/dashboard/${playerId}`);
    const nodes = Object.entries(TOPIC_GRAPH).map(([topic]) => ({
      id: topic,
      label: TOPIC_LABELS[topic] || topic,
      accuracy: data.topic_accuracies?.[topic] ?? 0,
      status: data.graph_state?.[topic] || 'locked',
    }));
    const edges = Object.entries(TOPIC_GRAPH).flatMap(([target, prerequisites]) =>
      prerequisites.map((source) => ({ source, target }))
    );
    return { ...data, graph: { nodes, edges } };
  },
};

// Multipart requests (file upload) can't go through request() above -- the
// browser must set its own multipart boundary in the Content-Type header,
// which request()'s hardcoded 'application/json' would clobber.
async function requestMultipart(path, formData) {
  let response;
  try {
    response = await fetch(`${API_BASE_URL}${path}`, { method: 'POST', body: formData });
  } catch (cause) {
    const error = new Error('Could not reach the backend. Is it running?');
    error.code = 0;
    error.cause = cause;
    throw error;
  }
  const data = await response.json().catch(() => ({}));
  if (!response.ok) {
    const detail = Array.isArray(data?.detail)
      ? data.detail.map((item) => item.msg).join(', ')
      : data?.detail;
    const error = new Error(data?.error || detail || `Request failed (${response.status})`);
    error.code = data?.code ?? response.status;
    throw error;
  }
  return data;
}

export const learning = {
  getCurricula: () => request('/learning/curricula'),

  getIntegrationStatus: () => request('/learning/integrations/status'),

  getProfile: (playerId) => request(`/learning/profile/${playerId}`),

  updateProfile: (playerId, profile) =>
    request(`/learning/profile/${playerId}`, { method: 'PUT', body: profile }),

  assess: (playerId, curriculumSlug, selfRatings) =>
    request(`/learning/assessment/${playerId}`, {
      method: 'POST',
      body: { curriculum_slug: curriculumSlug, self_ratings: selfRatings },
    }),

  getPathway: (playerId, curriculumSlug) =>
    request(`/learning/pathway/${playerId}?curriculum_slug=${encodeURIComponent(curriculumSlug)}`),

  generateQuiz: async ({ playerId, title, difficulty, language, questionCount, file }) => {
    const form = new FormData();
    form.append('player_id', playerId);
    form.append('title', title);
    form.append('difficulty', difficulty);
    form.append('language', language);
    form.append('question_count', String(questionCount));
    form.append('file', file);
    return requestMultipart('/learning/quiz/generate', form);
  },

  listQuizzes: (playerId) => request(`/learning/quiz/${playerId}`),

  getAdminOverview: () => request('/learning/admin/overview'),
};
