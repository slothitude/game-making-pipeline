extends SceneTree
## Milestone 2 battery — Act 1 slice. Run headless:
##   godot --headless --script res://tests/m2_tests.gd
## Done = 2x consecutive green, alongside tilt_tests (spec law "growing_battery").
## Covers: weapon tiers + pooling, charge + super suspension, bomb clear/damage,
## fighter dive + aimed shot, bomber sine + 3-spread, boss entry/sweep/phase2/
## bounded death, hit penalty + iframes + death at 0, pickup tier cap, stage
## spawn table -> boss -> clear tally.

const PLANE_SCENE := preload("res://scenes/plane.tscn")
const DT := 1.0 / 60.0

var passes := 0
var fails := 0
var boss_entered_count := 0
var game_over_count := 0
var clear_count := 0
var defeated_count := 0


func _initialize() -> void:
	_run()


func _run() -> void:
	await process_frame

	# ======================== weapon: tier volley shapes + pooling =========
	var w := Weapon.new()
	var pool := BulletPool.new()
	pool.setup(true, 64)
	root.add_child(pool)
	w.pool = pool
	var muzzle := Vector2(270.0, 900.0)

	w.tier = 1
	check("weapon_t1_fires_single_shot", w.fire_now(muzzle) == 1 and pool.active_count() == 1,
		"spawned=%d live=%d" % [w.shots_spawned, pool.active_count()])

	w.tier = 2
	pool.clear_all()
	check("weapon_t2_fires_twin", w.fire_now(muzzle) == 2 and pool.active_count() == 2,
		"live=%d" % pool.active_count())

	w.tier = 3
	pool.clear_all()
	var t3_landed := w.fire_now(muzzle)
	var t3_angles: Array[float] = []
	for b in pool.live_bullets():
		t3_angles.append(snappedf(atan2(b.velocity.x, -b.velocity.y), 0.001))
	var t3_ok := t3_landed == 4 and t3_angles.has(0.0) \
		and t3_angles.has(snappedf(-Feel.TIER_ANGLED_RAD, 0.001)) \
		and t3_angles.has(snappedf(Feel.TIER_ANGLED_RAD, 0.001))
	check("weapon_t3_fires_twin_plus_angled_pair", t3_ok, "n=%d angles=%s" % [t3_landed, t3_angles])

	w.tier = 4
	pool.clear_all()
	var t4_landed := w.fire_now(muzzle)
	var t4_straight := true
	for b in pool.live_bullets():
		t4_straight = t4_straight and absf(atan2(b.velocity.x, -b.velocity.y)) < 0.0001
	check("weapon_t4_fires_triple_stream", t4_landed == 3 and pool.active_count() == 3 and t4_straight,
		"n=%d live=%d straight=%s" % [t4_landed, pool.active_count(), t4_straight])

	# auto-fire cadence: tier 1 for 1 simulated second -> FIRE_RATE volleys
	var w2 := Weapon.new()
	var pool2 := BulletPool.new()
	pool2.setup(true, 96)
	root.add_child(pool2)
	w2.pool = pool2
	for i in 60:
		w2.step(DT, muzzle)
	check("weapon_auto_fire_matches_fire_rate", w2.volleys_fired == int(Feel.FIRE_RATE),
		"volleys=%d rate=%f" % [w2.volleys_fired, Feel.FIRE_RATE])

	# pooling: cap holds, the freed slot is reused
	var capped := BulletPool.new()
	capped.setup(true, 8)
	root.add_child(capped)
	var filled := true
	for i in 8:
		filled = filled and capped.spawn(Vector2.ZERO, Vector2.UP, 1, 8.0) != null
	var ninth := capped.spawn(Vector2.ZERO, Vector2.UP, 1, 8.0)
	capped.retire(capped.live_bullets()[0])
	var reuse := capped.spawn(Vector2.ZERO, Vector2.UP, 1, 8.0)
	check("weapon_pool_respects_cap_and_reuses", filled and ninth == null \
		and capped.active_count() == 8 and reuse != null,
		"ninth=%s live=%d reuse=%s" % [ninth != null, capped.active_count(), reuse != null])

	# ======================== charge + super shot ==========================
	var w3 := Weapon.new()
	var pool3 := BulletPool.new()
	pool3.setup(true, 96)
	root.add_child(pool3)
	w3.pool = pool3
	for i in Feel.SUPER_SHOT_CHARGE_KILLS - 1:
		w3.register_kill()
	check("super_locked_below_full_charge", not w3.super_ready() and not w3.release_super(),
		"charge=%d" % w3.kills_charged)
	w3.register_kill()
	var filled_ok := w3.super_ready()
	for i in 5:
		w3.register_kill()
	check("charge_fills_per_kill_and_saturates", filled_ok
		and w3.kills_charged == Feel.SUPER_SHOT_CHARGE_KILLS,
		"charge=%d needed=%d" % [w3.kills_charged, Feel.SUPER_SHOT_CHARGE_KILLS])

	var released := w3.release_super() and w3.super_active() and w3.kills_charged == 0
	var spawned_during := 0
	var frames := 0
	while w3.super_active() and frames < 1000:
		spawned_during += w3.step(DT, muzzle)
		frames += 1
	var resumed := w3.step(DT, muzzle) > 0
	var dur_frames := int(Feel.SUPER_SHOT_DURATION * 60.0)
	check("super_release_suspends_normal_fire_full_duration", released and spawned_during == 0
		and resumed and frames >= dur_frames and frames <= dur_frames + 1,
		"released=%s during=%d resumed=%s frames=%d" % [released, spawned_during, resumed, frames])

	# ======================== bomb =========================================
	var s := await make_stage()
	for i in 20:
		s.enemy_bullets.spawn(Vector2(60.0 + i * 20.0, 300.0), Vector2(0, 200), 1,
			Feel.ENEMY_BULLET_RADIUS, false)
	for i in 5:
		s.player_bullets.spawn(Vector2(100.0 + i * 20.0, 500.0), Vector2(0, -700), 1,
			Feel.PLAYER_BULLET_RADIUS, true)
	var bomb_ok := s.use_bomb() and s.enemy_bullets.active_count() == 0 \
		and s.player_bullets.active_count() == 5 and s.bombs == Feel.BOMB_START_COUNT - 1
	check("bomb_clears_all_enemy_bullets", bomb_ok, "enemy=%d player=%d bombs=%d" % [
		s.enemy_bullets.active_count(), s.player_bullets.active_count(), s.bombs])

	var fighter := Enemy.FighterStraight.new()
	fighter.position = Vector2(120.0, 200.0)
	s.track_enemy(fighter)
	var bomber := Enemy.BomberSlow.new()
	bomber.position = Vector2(360.0, 240.0)
	s.track_enemy(bomber)
	s.spawn_boss()
	var boss_hp0: int = s.boss.hp   # (m3: stage.boss holds either boss class -> untyped)
	var bomb2 := s.use_bomb()          # bombs the boss + both enemies
	var third := s.use_bomb()          # empty: refused
	var fourth := s.use_bomb()
	check("bomb_damages_all_and_refuses_when_empty", bomb2 and not fighter.alive \
		and not bomber.alive and fourth == false and third == false and s.bombs == 0 \
		and s.boss.hp == boss_hp0 - Feel.BOMB_DAMAGE,
		"fighter=%s bomber=%s bombs=%d hp=%d/%d" % [fighter.alive, bomber.alive,
		s.bombs, s.boss.hp, boss_hp0])

	# ======================== fighter / bomber patterns ====================
	var tgt := Node2D.new()
	tgt.position = Vector2(400.0, 700.0)
	root.add_child(tgt)
	var epool := BulletPool.new()
	epool.setup(false, 32)
	root.add_child(epool)
	var f2 := Enemy.FighterStraight.new()
	f2.pool = epool
	f2.target = tgt
	f2.position = Vector2(270.0, Feel.STAGE_SPAWN_Y)
	root.add_child(f2)
	var f_y0 := f2.position.y
	var aimed_unit := Vector2.ZERO
	var f_pos_at_fire := Vector2.ZERO
	for i in 120:  # 2 s: first aimed shot at 0.9 s, next would be 2.8 s
		f2.step(DT)
		if epool.active_count() == 1 and aimed_unit == Vector2.ZERO:
			aimed_unit = epool.live_bullets()[0].velocity.normalized()
			f_pos_at_fire = f2.position
	var to_target := (tgt.position - f_pos_at_fire).normalized()
	check("fighter_dives_and_fires_single_aimed_shot", f2.shots_fired == 1
		and epool.active_count() == 1 and aimed_unit.dot(to_target) > 0.999
		and near(f2.position.y - f_y0, Feel.FIGHTER_SPEED * 2.0, 0.5),
		"shots=%d live=%d dot=%f dy=%f" % [f2.shots_fired, epool.active_count(),
		aimed_unit.dot(to_target), f2.position.y - f_y0])

	epool.clear_all()
	var b2 := Enemy.BomberSlow.new()
	b2.pool = epool
	b2.position = Vector2(80.0, 200.0)
	root.add_child(b2)
	var b_x0 := b2.position.x
	for i in 120:  # 2 s: first spread at 1.2 s, next would be 3.5 s
		b2.step(DT)
	var spread := epool.live_bullets()
	var vx_signs := {}
	var spread_ok := b2.shots_fired == 3 and spread.size() == 3
	for b in spread:
		spread_ok = spread_ok and b.velocity.y > 0.0  # spread drops downward
		vx_signs[signi(int(b.velocity.x * 1000.0))] = true
	spread_ok = spread_ok and vx_signs.size() == 3  # left, straight, right arms
	check("bomber_spreads_downward_and_drifts_sine", spread_ok and b2.min_y < b2.max_y
		and near(b2.position.x - b_x0, Feel.BOMBER_SPEED * 2.0, 0.5),
		"shots=%d live=%d signs=%s yrange=%f dx=%f" % [b2.shots_fired, spread.size(),
		vx_signs, b2.max_y - b2.min_y, b2.position.x - b_x0])

	# ======================== boss =========================================
	var bpool := BulletPool.new()
	bpool.setup(false, 96)
	root.add_child(bpool)
	var boss := BossFortress.new()
	boss.pool = bpool
	boss.target = tgt
	boss.position = Vector2(270.0, 0.0)
	root.add_child(boss)
	defeated_count = 0
	boss.defeated.connect(func() -> void: defeated_count += 1)
	var guard := 0
	while boss.phase == BossFortress.PH_ENTER and guard < 1000:
		boss.step(DT)
		guard += 1
	var entry_ok := boss.phase == BossFortress.PH_ONE \
		and nearf(boss.position.y, Feel.BOSS1_ENTER_Y, 1.0)
	var pre := bpool.active_count()
	for i in int(Feel.BOSS1_SWEEP_INTERVAL * 60.0) + 1:
		boss.step(DT)
	check("boss_enters_then_sweeps_five_way", entry_ok and boss.sweeps_fired == 1
		and bpool.active_count() - pre == Feel.BOSS1_SWEEP_COUNT and boss.bursts_fired == 0,
		"phase=%d y=%f sweeps=%d bullets=%d bursts=%d" % [boss.phase, boss.position.y,
		boss.sweeps_fired, bpool.active_count() - pre, boss.bursts_fired])

	check("boss_phase2_swaps_at_hp_threshold", boss.sweep_interval() == Feel.BOSS1_SWEEP_INTERVAL \
		and not boss.phase2() and not boss.take_damage(Feel.BOSS1_PHASE2_HP) \
		and boss.phase2() and boss.hp == Feel.BOSS1_PHASE2_HP \
		and boss.sweep_interval() == Feel.BOSS1_SWEEP_INTERVAL_P2,
		"hp=%d interval=%f" % [boss.hp, boss.sweep_interval()])

	var p1_twin := BossFortress.new()
	p1_twin.pool = bpool
	root.add_child(p1_twin)
	for i in 162:  # 2.7 s of both bosses side by side
		boss.step(DT)
		p1_twin.step(DT)
	check("boss_phase2_adds_bursts_and_faster_sweeps", boss.bursts_fired >= Feel.BOSS1_BURST_COUNT \
		and p1_twin.bursts_fired == 0 and boss.sweeps_fired > p1_twin.sweeps_fired,
		"p2 bursts=%d sweeps=%d / p1 bursts=%d sweeps=%d" % [boss.bursts_fired,
		boss.sweeps_fired, p1_twin.bursts_fired, p1_twin.sweeps_fired])
	var hits := 0
	while boss.alive and hits < Feel.BOSS1_HP + 10:
		boss.take_damage(1)
		hits += 1
	check("boss_dies_within_bounded_hits", not boss.alive and hits <= Feel.BOSS1_HP
		and defeated_count == 1, "hits=%d hp=%d defeated=%d" % [hits, boss.hp, defeated_count])

	# ======================== player hit penalty + lives ===================
	var s2 := await make_stage()
	s2.weapon.tier = 3
	s2.player_hit()
	var after_first := s2.lives == Feel.PLAYER_LIVES - 1 and s2.weapon.tier == 2 \
		and s2.iframe_left > 0.0
	s2.player_hit()  # iframes absorb the immediate next hit
	check("player_hit_drops_tier_grants_iframes_ignores_repeat", after_first
		and s2.lives == Feel.PLAYER_LIVES - 1 and s2.weapon.tier == 2,
		"lives=%d tier=%d iframes=%f" % [s2.lives, s2.weapon.tier, s2.iframe_left])
	s2.iframe_left = 0.0
	s2.player_hit()  # tier 2 -> 1, down to the last life
	var at_floor := s2.weapon.tier == Feel.WEAPON_TIER_MIN and s2.lives == 1
	s2.iframe_left = 0.0
	game_over_count = 0
	s2.game_over.connect(func() -> void: game_over_count += 1)
	s2.player_hit()  # life 0 -> game over
	check("hits_at_tier_floor_cost_lives_and_die_at_zero", at_floor and s2.lives <= 0
		and s2.is_game_over() and game_over_count == 1,
		"lives=%d over=%s signals=%d" % [s2.lives, s2.is_game_over(), game_over_count])

	# ======================== pickups ======================================
	# (m3 revision: the spec 3-per-tier gate replaced the v1 +1-per-pickup,
	# so this check now banks Feel.PICKUPS_PER_TIER pickups to advance.)
	var s3 := await make_stage()
	for i in Feel.PICKUPS_PER_TIER:
		s3.spawn_pickup_at(s3.plane.position + Vector2(0.0, -4.0))
		s3.advance(DT, 0.0)
	var s3_tier_after := s3.weapon.tier
	s3.weapon.tier = Feel.WEAPON_TIERS
	s3.spawn_pickup_at(s3.plane.position)
	s3.advance(DT, 0.0)
	check("pickup_gate_advances_tier_and_caps_at_max", s3_tier_after == Feel.WEAPON_TIER_MIN + 1
		and s3.pickups_spawned == Feel.PICKUPS_PER_TIER + 1 and s3.weapon.tier == Feel.WEAPON_TIERS,
		"tier=%d->%d pickups=%d" % [Feel.WEAPON_TIER_MIN + 1, s3.weapon.tier, s3.pickups_spawned])
	var p_fall := Stage.Pickup.new()
	p_fall.position = Vector2(270.0, 100.0)
	root.add_child(p_fall)
	var py0 := p_fall.position.y
	p_fall.step(DT)
	check("pickup_falls_at_const_speed", near(p_fall.position.y - py0, Feel.PICKUP_FALL_SPEED * DT, 0.001),
		"dy=%f" % (p_fall.position.y - py0))
	p_fall.queue_free()

	# ======================== stage: table -> boss -> tally ================
	var s4 := await make_stage()
	s4.invincible = true  # harness hook: survive to the boss regardless of fire
	boss_entered_count = 0
	clear_count = 0
	s4.boss_entered.connect(func() -> void: boss_entered_count += 1)
	s4.stage_cleared.connect(func(_t: Dictionary) -> void: clear_count += 1)
	sim_seconds(s4, 3.5)  # fighter waves at 0.6 s and 2.8 s, bomber due at 4.0 s
	var waves_early := s4.fighters_spawned == 2 * Feel.STAGE_FIGHTERS_PER_WAVE \
		and s4.bombers_spawned == 0
	sim_seconds(s4, 1.0)
	check("stage_spawn_table_pairs_then_bomber", waves_early and s4.fighters_spawned == 4
		and s4.bombers_spawned == 1, "fighters=%d bombers=%d" % [
		s4.fighters_spawned, s4.bombers_spawned])
	var guard2 := 0
	while s4.state != Stage.ST_BOSS and s4.time < Feel.STAGE_LEN_SEC + 2.0 and guard2 < 10000:
		s4.step(DT)
		guard2 += 1
	check("stage_reaches_boss_after_stage_timer", s4.state == Stage.ST_BOSS
		and s4.boss != null and boss_entered_count == 1
		and s4.time >= Feel.STAGE_LEN_SEC - DT,
		"state=%d time=%f boss=%s" % [s4.state, s4.time, s4.boss != null])
	var scale_mid := s4.wave_scale()
	s4.boss.take_damage(Feel.BOSS1_HP * 3)
	var clear_window := s4.state == Stage.ST_CLEAR and not s4.tally_shown
	sim_seconds(s4, Feel.STAGE_CLEAR_DELAY_SEC + 0.2)
	check("stage_escalates_and_clears_with_tally", scale_mid < 1.0 \
		and scale_mid >= Feel.STAGE_ESCALATE_MIN_SCALE and clear_window \
		and s4.tally_shown and clear_count == 1 \
		and int(s4.tally.get("no_damage_bonus", 0)) == Feel.NO_DAMAGE_BONUS \
		and int(s4.tally.get("score", 0)) == s4.score,
		"scale=%f window=%s tally=%s" % [scale_mid, clear_window, s4.tally])

	print("")
	print("RESULT: %d passed, %d failed" % [passes, fails])
	quit(0 if fails == 0 else 1)


## ------------------------------------------------------------------ utils --

func make_stage() -> Stage:
	var s := Stage.new()
	root.add_child(s)
	await process_frame
	var p: PlayerPlane = PLANE_SCENE.instantiate()
	root.add_child(p)
	await process_frame
	p.set_physics_process(false)
	p.position = Vector2(270.0, 800.0)
	s.bind_plane(p)
	s.start()
	return s


func sim_seconds(s: Stage, seconds: float) -> void:
	for i in int(seconds * 60.0):
		s.step(DT)


func check(name: String, ok: bool, detail := "") -> void:
	if ok:
		passes += 1
		print("PASS: " + name)
	else:
		fails += 1
		print("FAIL: " + name + "  (" + detail + ")")


func near(a: float, b: float, eps := 0.01) -> bool:
	return absf(a - b) < eps


func nearf(a: float, b: float, eps: float) -> bool:
	return absf(a - b) < eps
