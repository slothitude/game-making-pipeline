class_name Stage
extends Node2D
## The Act stage simulation (m2 = act 1; m3 adds act 2): spawn table -> boss
## -> clear -> act banner -> next act's mixed waves -> its boss -> final
## tally. Owns both bullet pools, the enemies, the boss, the pickups and the
## player's weapon state. Everything is hand-stepped (advance()) — no physics,
## no RNG outside the seeded stage generator — so tests and replays are
## deterministic. main.gd calls advance(delta, tilt_output) once per frame;
## tests call it directly.

signal score_changed(score: int)
signal lives_changed(lives: int)
signal tier_changed(tier: int)
signal charge_changed(kills: int, needed: int)
signal bombs_changed(count: int)
signal boss_entered
signal act_started(act: int)
signal pickup_progress_changed(collected: int, needed: int)
signal game_over
signal stage_cleared(tally: Dictionary)

const ST_WAVES := 0
const ST_BOSS := 1
const ST_CLEAR := 2
const ST_BANNER := 3
const ST_OVER := 4

const STATE_NAMES := {
	ST_WAVES: "waves",
	ST_BOSS: "boss",
	ST_CLEAR: "clear",
	ST_BANNER: "act_banner",
	ST_OVER: "game_over",
}

var state := ST_WAVES
var time := 0.0
var act := 1
var act_time := 0.0              # clock inside the current act (spawn tables)
var transition_title := ""       # act banner text while in ST_BANNER
var score := 0
var kills := 0
var lives := Feel.PLAYER_LIVES
var bombs := Feel.BOMB_START_COUNT
var damage_taken := 0
var iframe_left := 0.0
var invincible := false          # test/replay harness hook: hits are ignored

var plane: PlayerPlane = null
var weapon: Weapon = null
var player_bullets: BulletPool
var enemy_bullets: BulletPool
var enemies: Array[Enemy] = []
var pickups: Array[Pickup] = []
var boss = null                  # BossFortress (act 1) or ProtoMech (act 2)

var fighters_spawned := 0
var bombers_spawned := 0
var jets_spawned := 0
var turrets_spawned := 0
var pickups_spawned := 0
var pickups_collected := 0
var pickup_progress := 0         # pickups banked toward the current tier gate
var tally := {}
var tally_shown := false

var rng := RandomNumberGenerator.new()
var _fighter_timer := Feel.STAGE_FIGHTER_FIRST_DELAY
var _bomber_timer := Feel.STAGE_BOMBER_FIRST_DELAY
var _jet_timer := Feel.STAGE_JET_FIRST_DELAY
var _turret_timer := Feel.STAGE_TURRET_FIRST_DELAY
var _clear_timer := 0.0
var _banner_timer := 0.0
var _beam_acc := 0.0
var _bomb_flash := 0.0
var _fx: Array[Dictionary] = []


## ---------------------------------------------------------------- setup --

func _ready() -> void:
	player_bullets = BulletPool.new()
	player_bullets.name = "PlayerBullets"
	player_bullets.setup(true, Feel.BULLET_POOL_PLAYER_CAP)
	add_child(player_bullets)
	enemy_bullets = BulletPool.new()
	enemy_bullets.name = "EnemyBullets"
	enemy_bullets.setup(false, Feel.BULLET_POOL_ENEMY_CAP)
	add_child(enemy_bullets)


func bind_plane(p: PlayerPlane) -> void:
	plane = p
	weapon = p.weapon
	if weapon != null:
		weapon.pool = player_bullets
	p.set_physics_process(false)  # Stage.advance owns stepping


## Fresh run: deterministic spawn order from the seed.
func start() -> void:
	rng.seed = Feel.STAGE_RNG_SEED
	rng.state = Feel.STAGE_RNG_SEED
	state = ST_WAVES
	time = 0.0
	act = 1
	act_time = 0.0
	transition_title = ""
	score = 0
	kills = 0
	lives = Feel.PLAYER_LIVES
	bombs = Feel.BOMB_START_COUNT
	damage_taken = 0
	iframe_left = 0.0
	tally = {}
	tally_shown = false
	fighters_spawned = 0
	bombers_spawned = 0
	jets_spawned = 0
	turrets_spawned = 0
	pickups_spawned = 0
	pickups_collected = 0
	pickup_progress = 0
	_fighter_timer = Feel.STAGE_FIGHTER_FIRST_DELAY
	_bomber_timer = Feel.STAGE_BOMBER_FIRST_DELAY
	_jet_timer = Feel.STAGE_JET_FIRST_DELAY
	_turret_timer = Feel.STAGE_TURRET_FIRST_DELAY
	_clear_timer = 0.0
	_banner_timer = 0.0
	_beam_acc = 0.0
	_bomb_flash = 0.0
	_fx.clear()
	player_bullets.reset()
	enemy_bullets.reset()
	if weapon != null:
		weapon.tier = Feel.WEAPON_TIER_MIN
		weapon.kills_charged = 0
		weapon.super_time_left = 0.0
		weapon.fire_cooldown = 0.0


## One full frame: steer + fire the plane, then run the world.
func advance(delta: float, tilt_output: float) -> void:
	if plane != null and is_instance_valid(plane):
		plane.step(clampf(tilt_output, -1.0, 1.0), delta)
	step(delta)


## ------------------------------------------------------------- main loop --

func is_game_over() -> bool:
	return state == ST_OVER


func step(delta: float) -> void:
	_step_fx(delta)
	if state == ST_OVER:
		return
	if state == ST_CLEAR:
		_step_pools(delta)
		_step_pickups(delta)
		_cull()
		_clear_timer -= delta
		if _clear_timer <= 0.0 and not tally_shown:
			_finish_tally()
		if tally_shown and has_next_act():
			_begin_banner()
		return
	if state == ST_BANNER:
		_step_pools(delta)
		_step_pickups(delta)
		_cull()
		_banner_timer -= delta
		if _banner_timer <= 0.0:
			_begin_next_act()
		return
	time += delta
	act_time += delta
	iframe_left = maxf(0.0, iframe_left - delta)
	if state == ST_WAVES:
		_spawn_waves(delta)
		if act_time >= Feel.STAGE_LEN_SEC and boss == null:
			spawn_boss()
	_step_pools(delta)
	_step_enemies(delta)
	_step_boss(delta)
	_step_pickups(delta)
	if plane != null and is_instance_valid(plane):
		if weapon != null and weapon.super_active():
			_apply_super_beam(delta)
		_collide()
	_cull()


## --------------------------------------------------------- spawn table --

## Spawn intervals shrink a step every STAGE_ESCALATE_EVERY_SEC, floor-clamped.
## Escalation runs per act (act_time), so act 2 opens at full intervals.
func wave_scale() -> float:
	var steps := int(act_time / Feel.STAGE_ESCALATE_EVERY_SEC)
	return maxf(Feel.STAGE_ESCALATE_MIN_SCALE, pow(Feel.STAGE_ESCALATE_SCALE, float(steps)))


func _spawn_waves(delta: float) -> void:
	var esc := wave_scale()
	_fighter_timer -= delta
	if _fighter_timer <= 0.0:
		_fighter_timer += Feel.STAGE_FIGHTER_WAVE_SEC * esc
		for i in Feel.STAGE_FIGHTERS_PER_WAVE:
			_spawn_fighter()
	_bomber_timer -= delta
	if _bomber_timer <= 0.0:
		_bomber_timer += Feel.STAGE_BOMBER_WAVE_SEC * esc
		_spawn_bomber()
	if act < 2:
		return
	_jet_timer -= delta
	if _jet_timer <= 0.0:
		_jet_timer += Feel.STAGE_JET_WAVE_SEC * esc
		_spawn_jet()
	_turret_timer -= delta
	if _turret_timer <= 0.0:
		_turret_timer += Feel.STAGE_TURRET_WAVE_SEC * esc
		_spawn_turret()


func _spawn_fighter() -> void:
	var view := get_viewport_rect().size
	var e := Enemy.FighterStraight.new()
	e.pool = enemy_bullets
	e.target = plane
	var inset := Feel.STAGE_FIGHTER_SPAWN_INSET_PX
	e.position = Vector2(rng.randf_range(inset, view.x - inset), Feel.STAGE_SPAWN_Y)
	track_enemy(e)


func _spawn_bomber() -> void:
	var view := get_viewport_rect().size
	var e := Enemy.BomberSlow.new()
	e.pool = enemy_bullets
	e.target = plane
	var from_left := rng.randf() < 0.5
	e.dir_x = 1.0 if from_left else -1.0
	var y := rng.randf_range(Feel.BOMBER_LANE_MIN_Y, Feel.BOMBER_LANE_MAX_Y)
	e.position = Vector2(-e.radius if from_left else view.x + e.radius, y)
	track_enemy(e)


func _spawn_jet() -> void:
	var view := get_viewport_rect().size
	var e := Enemy.UnmarkedJet.new()
	e.pool = enemy_bullets
	e.target = plane
	var inset := Feel.STAGE_FIGHTER_SPAWN_INSET_PX
	e.position = Vector2(rng.randf_range(inset, view.x - inset), Feel.STAGE_SPAWN_Y)
	track_enemy(e)


func _spawn_turret() -> void:
	var view := get_viewport_rect().size
	var e := Enemy.EnergyTurret.new()
	e.pool = enemy_bullets
	e.target = plane
	var from_left := rng.randf() < 0.5
	e.edge_dir = 1.0 if from_left else -1.0
	var y := rng.randf_range(Feel.TURRET_LANE_MIN_Y, Feel.TURRET_LANE_MAX_Y)
	e.position = Vector2(Feel.TURRET_EDGE_INSET_PX if from_left \
			else view.x - Feel.TURRET_EDGE_INSET_PX, y)
	track_enemy(e)


func track_enemy(e: Enemy) -> void:
	e.died.connect(_on_enemy_died)
	add_child(e)
	enemies.append(e)
	match e.kind:
		"bomber":
			bombers_spawned += 1
		"jet":
			jets_spawned += 1
		"turret":
			turrets_spawned += 1
		_:
			fighters_spawned += 1


func spawn_boss() -> void:
	var view := get_viewport_rect().size
	if act >= 2:
		boss = ProtoMech.new()
	else:
		boss = BossFortress.new()
	boss.pool = enemy_bullets
	boss.target = plane
	boss.position = Vector2(view.x * 0.5, Feel.STAGE_BOSS_SPAWN_Y)
	boss.defeated.connect(_on_boss_defeated)
	add_child(boss)
	state = ST_BOSS
	boss_entered.emit()


## False once the final content act has been cleared.
func has_next_act() -> bool:
	return act < Feel.FINAL_ACT


## Act banner text (held while the state is ST_BANNER, else empty).
func banner_text() -> String:
	return transition_title


## ------------------------------------------------------- player actions --

func try_release_super() -> bool:
	if weapon == null or state == ST_OVER or state == ST_CLEAR:
		return false
	if weapon.release_super():
		charge_changed.emit(weapon.kills_charged, Feel.SUPER_SHOT_CHARGE_KILLS)
		return true
	return false


func use_bomb() -> bool:
	if state == ST_OVER or state == ST_CLEAR or bombs <= 0:
		return false
	bombs -= 1
	bombs_changed.emit(bombs)
	enemy_bullets.clear_all()
	for e in enemies:
		if e.alive:
			e.take_damage(Feel.BOMB_DAMAGE)
	if boss != null and boss.alive:
		boss.take_damage(Feel.BOMB_DAMAGE)
	_bomb_flash = Feel.BOMB_FLASH_SEC
	return true


func player_hit() -> void:
	if invincible or iframe_left > 0.0:
		return
	if state == ST_OVER or state == ST_CLEAR:
		return
	damage_taken += 1
	lives -= 1
	lives_changed.emit(lives)
	if weapon != null:
		_set_tier(weapon.tier - Feel.PLAYER_HIT_TIER_LOSS)
	iframe_left = Feel.PLAYER_HIT_IFRAME_SEC
	if plane != null:
		_add_fx("boom", plane.position, Feel.EXPLOSION_SMALL_RADIUS, Feel.EXPLOSION_DURATION)
	if lives <= 0:
		state = ST_OVER
		game_over.emit()


func _set_tier(t: int) -> void:
	if weapon == null:
		return
	t = clampi(t, Feel.WEAPON_TIER_MIN, Feel.WEAPON_TIERS)
	if t != weapon.tier:
		weapon.tier = t
		tier_changed.emit(t)


## ----------------------------------------------------------- world sim --

func _step_pools(delta: float) -> void:
	player_bullets.step(delta)
	enemy_bullets.step(delta)


func _step_enemies(delta: float) -> void:
	for e in enemies:
		if e.alive:
			e.step(delta)


func _step_boss(delta: float) -> void:
	if boss != null and boss.alive:
		boss.step(delta)


func _step_pickups(delta: float) -> void:
	for p in pickups:
		p.step(delta)


func _apply_super_beam(delta: float) -> void:
	_beam_acc += Feel.SUPER_BEAM_DPS * delta
	var dmg := int(_beam_acc)
	if dmg <= 0:
		return
	_beam_acc -= float(dmg)
	var half := Feel.SUPER_BEAM_WIDTH_PX * 0.5
	for e in enemies:
		if e.alive and _under_beam(e.position, half, e.radius):
			e.take_damage(dmg)
	if boss != null and boss.alive and _under_beam(boss.position, half, boss.radius):
		boss.take_damage(dmg)


func _under_beam(pos: Vector2, half: float, extra: float) -> bool:
	if plane == null:
		return false
	return absf(pos.x - plane.position.x) <= half + extra and pos.y < plane.position.y


func _collide() -> void:
	# player bullets vs enemies, then boss
	for b in player_bullets.live_bullets():
		if not b.active:
			continue
		for e in enemies:
			if e.alive and b.position.distance_to(e.position) <= b.radius + e.radius:
				player_bullets.retire(b)
				e.take_damage(b.damage)
				break
		if not b.active:
			continue
		if boss != null and boss.alive and b.position.distance_to(boss.position) <= b.radius + boss.radius:
			player_bullets.retire(b)
			boss.take_damage(b.damage)
	# enemy bullets vs plane
	if iframe_left <= 0.0 and not invincible:
		for b in enemy_bullets.live_bullets():
			if b.active and b.position.distance_to(plane.position) <= b.radius + Feel.PLAYER_RADIUS:
				enemy_bullets.retire(b)
				player_hit()
				break
		if iframe_left <= 0.0:
			for e in enemies:
				if e.alive and e.position.distance_to(plane.position) <= e.radius + Feel.PLAYER_RADIUS:
					player_hit()
					break
			if boss != null and boss.alive \
					and boss.position.distance_to(plane.position) <= boss.radius + Feel.PLAYER_RADIUS:
				player_hit()
		# laser beams (act 2 turret + proto_mech rows)
		if iframe_left <= 0.0:
			for e in enemies:
				if e.alive and e.is_beam_active() and e.beam_hits(plane.position):
					player_hit()
					break
			if boss != null and boss.alive and boss.is_beam_active() \
					and boss.beam_hits(plane.position):
				player_hit()
	# pickups vs plane
	for p in pickups:
		if not p.collected and p.position.distance_to(plane.position) <= Feel.PICKUP_RADIUS + Feel.PLAYER_RADIUS:
			p.collected = true
			_collect_pickup(p)


func _cull() -> void:
	var kept: Array[Enemy] = []
	for e in enemies:
		if not e.alive or e.escaped():
			e.queue_free()
		else:
			kept.append(e)
	enemies = kept
	var kept_p: Array[Pickup] = []
	var view := get_viewport_rect().size
	for p in pickups:
		if p.collected or p.position.y > view.y + Feel.BULLET_OFFSCREEN_MARGIN_PX:
			p.queue_free()
		else:
			kept_p.append(p)
	pickups = kept_p


## --------------------------------------------------------------- events --

func _on_enemy_died(e: Enemy) -> void:
	kills += 1
	score += e.score_value
	score_changed.emit(score)
	if weapon != null:
		weapon.register_kill()
		charge_changed.emit(weapon.kills_charged, Feel.SUPER_SHOT_CHARGE_KILLS)
	_add_fx("boom", e.position, Feel.EXPLOSION_SMALL_RADIUS, Feel.EXPLOSION_DURATION)
	if rng.randf() < Feel.PICKUP_DROP_CHANCE:
		_spawn_pickup(e.position)


func _on_boss_defeated() -> void:
	kills += 1
	score += boss.score_value
	score_changed.emit(score)
	_add_fx("boom", boss.position, Feel.EXPLOSION_BIG_RADIUS, Feel.EXPLOSION_DURATION)
	_clear_timer = Feel.STAGE_CLEAR_DELAY_SEC
	state = ST_CLEAR


## ------------------------------------------------------- act transitions --

## Wipe the screen and hold the act card before the next act's waves.
func _begin_banner() -> void:
	transition_title = Feel.ACT2_TITLE
	_banner_timer = Feel.STAGE_BANNER_SEC
	enemy_bullets.clear_all()
	for e in enemies:
		e.queue_free()
	enemies.clear()
	if boss != null:      # the fallen boss comes off the board between acts
		boss.queue_free()
		boss = null
	state = ST_BANNER
	act_started.emit(act + 1)


func _begin_next_act() -> void:
	act += 1
	act_time = 0.0
	transition_title = ""
	_fighter_timer = Feel.STAGE_FIGHTER_FIRST_DELAY
	_bomber_timer = Feel.STAGE_BOMBER_FIRST_DELAY
	_jet_timer = Feel.STAGE_JET_FIRST_DELAY
	_turret_timer = Feel.STAGE_TURRET_FIRST_DELAY
	state = ST_WAVES


func _finish_tally() -> void:
	var bonus := Feel.NO_DAMAGE_BONUS if damage_taken == 0 else 0
	score += bonus
	score_changed.emit(score)
	tally = {
		"score": score,
		"kills": kills,
		"no_damage_bonus": bonus,
		"lives_left": maxi(lives, 0),
	}
	tally_shown = true
	stage_cleared.emit(tally)


## -------------------------------------------------------------- pickups --

func _spawn_pickup(pos: Vector2) -> void:
	var p := Pickup.new()
	p.position = pos
	add_child(p)
	pickups.append(p)
	pickups_spawned += 1


## Spec powerups gating: PICKUPS_PER_TIER pickups bank into one tier step
## (m2 shipped a v1 +1-per-pickup simplification; this is the spec law).
func _collect_pickup(_p: Pickup) -> void:
	pickups_collected += 1
	if weapon == null:
		return
	if weapon.tier >= Feel.WEAPON_TIERS:
		pickup_progress = 0   # maxed out: the pickup is spent, the tier holds
	elif pickup_progress + 1 >= Feel.PICKUPS_PER_TIER:
		pickup_progress = 0
		_set_tier(weapon.tier + 1)
	else:
		pickup_progress += 1
	pickup_progress_changed.emit(pickup_progress, Feel.PICKUPS_PER_TIER)


## Test seam: force a pickup into the world (drop chance stays untested RNG).
func spawn_pickup_at(pos: Vector2) -> void:
	_spawn_pickup(pos)


## ------------------------------------------------------------------- fx --

func _add_fx(kind: String, pos: Vector2, radius: float, duration: float) -> void:
	_fx.append({"kind": kind, "pos": pos, "radius": radius, "t": 0.0, "dur": duration})


func _step_fx(delta: float) -> void:
	_bomb_flash = maxf(0.0, _bomb_flash - delta)
	var kept: Array[Dictionary] = []
	for fx in _fx:
		fx.t = fx.t + delta
		if fx.t < fx.dur:
			kept.append(fx)
	_fx = kept
	queue_redraw()


func bomb_flash_active() -> bool:
	return _bomb_flash > 0.0


func fx_count() -> int:
	return _fx.size()


func _draw() -> void:
	var view := get_viewport_rect().size
	if weapon != null and weapon.super_active() and plane != null and is_instance_valid(plane):
		var half := Feel.SUPER_BEAM_WIDTH_PX * 0.5
		draw_rect(Rect2(plane.position.x - half, 0.0, Feel.SUPER_BEAM_WIDTH_PX,
				maxf(plane.position.y + Feel.MUZZLE_OFFSET.y, 0.0)), Feel.SUPER_BEAM_COLOR)
	for fx in _fx:
		var k := float(fx.t) / float(fx.dur)
		var r := float(fx.radius) * (0.35 + 0.65 * k)
		draw_circle(fx.pos, r, Color(1.0, 0.72, 0.25, 1.0 - k))
		draw_arc(fx.pos, r * 1.3, 0.0, TAU, 24, Color(1.0, 0.95, 0.8, 0.8 * (1.0 - k)), 6.0)
	if _bomb_flash > 0.0:
		draw_rect(Rect2(Vector2.ZERO, view),
				Color(Feel.BOMB_FLASH_COLOR, (_bomb_flash / Feel.BOMB_FLASH_SEC)))


## ---------------------------------------------------------------- inner --

class Pickup:
	extends Node2D
	## P powerup: falls straight down; PICKUPS_PER_TIER of them advance the
	## weapon one tier (spec powerups gating, milestone 3).
	## Procedurally drawn (spec law "original_assets").

	var collected := false

	func step(delta: float) -> void:
		position.y += Feel.PICKUP_FALL_SPEED * delta
		rotation += 1.6 * delta

	func _draw() -> void:
		draw_circle(Vector2.ZERO, Feel.PICKUP_RADIUS, Feel.PICKUP_OUTLINE_COLOR)
		draw_circle(Vector2.ZERO, Feel.PICKUP_RADIUS - 4.0, Feel.PICKUP_COLOR)
		var font := ThemeDB.fallback_font
		draw_string(font, Vector2(-8.0, 8.0), "P", HORIZONTAL_ALIGNMENT_CENTER, 16.0, 22.0,
				Feel.PICKUP_OUTLINE_COLOR)
