extends SceneTree
## Milestone 3 battery — powerup gating + escorts + Act 2 content. Run headless:
##   godot --headless --script res://tests/m3_tests.gd
## Done = 2x consecutive green, alongside tilt_tests + m2_tests (spec law
## "growing_battery"). Covers: the spec 3-pickups-per-tier weapon gate, escort
## option planes (tier-gated count, small-copy visuals, fixed-radius orbit,
## parallel fire cadence), the unmarked_jet fast dive + 2-shot bolt burst, the
## energy_turret edge beam with its sweeping telegraph, the proto_mech entry +
## missile-arc / laser-sweep cycle + phase 2 @35 running both, act 2 loading
## after the act-1 boss clear (banner + mixed waves + proto_mech), and the
## seeded pickup drop rate.

const PLANE_SCENE := preload("res://scenes/plane.tscn")
const DT := 1.0 / 60.0

var passes := 0
var fails := 0


func _initialize() -> void:
	_run()


func _run() -> void:
	await process_frame

	# ==================== powerup gating (spec: 3 pickups per tier) ========
	var s := await make_stage()
	var t0 := s.weapon.tier
	s.spawn_pickup_at(s.plane.position)
	s.advance(DT, 0.0)
	var after_one := s.weapon.tier
	s.spawn_pickup_at(s.plane.position)
	s.advance(DT, 0.0)
	var after_two := s.weapon.tier
	s.spawn_pickup_at(s.plane.position)
	s.advance(DT, 0.0)
	check("pickup_gate_needs_three_to_advance", t0 == Feel.WEAPON_TIER_MIN \
		and after_one == t0 and after_two == t0 and s.weapon.tier == t0 + 1 \
		and s.pickups_collected == Feel.PICKUPS_PER_TIER and s.pickup_progress == 0,
		"tier %d->%d->%d->%d collected=%d progress=%d" % [t0, after_one, after_two,
		s.weapon.tier, s.pickups_collected, s.pickup_progress])

	for i in Feel.PICKUPS_PER_TIER:   # the gate repeats for the next tier
		s.spawn_pickup_at(s.plane.position)
		s.advance(DT, 0.0)
	check("pickup_gate_repeats_per_tier", s.weapon.tier == Feel.WEAPON_TIER_MIN + 2 \
		and s.pickups_collected == 2 * Feel.PICKUPS_PER_TIER and s.pickup_progress == 0,
		"tier=%d collected=%d progress=%d" % [s.weapon.tier, s.pickups_collected,
		s.pickup_progress])

	s.weapon.tier = Feel.WEAPON_TIERS
	s.spawn_pickup_at(s.plane.position)
	s.advance(DT, 0.0)
	check("pickup_gate_capped_at_max_tier", s.weapon.tier == Feel.WEAPON_TIERS \
		and s.pickup_progress == 0 and s.pickups_collected == 2 * Feel.PICKUPS_PER_TIER + 1,
		"tier=%d progress=%d collected=%d" % [s.weapon.tier, s.pickup_progress,
		s.pickups_collected])

	# ==================== escorts (spec: tier-gated option planes) =========
	check("escort_counts_by_tier", PlayerPlane.escort_count_for_tier(1) == 0 \
		and PlayerPlane.escort_count_for_tier(2) == 0 \
		and PlayerPlane.escort_count_for_tier(3) == 1 \
		and PlayerPlane.escort_count_for_tier(4) == 2,
		"t1=%d t2=%d t3=%d t4=%d" % [PlayerPlane.escort_count_for_tier(1),
		PlayerPlane.escort_count_for_tier(2), PlayerPlane.escort_count_for_tier(3),
		PlayerPlane.escort_count_for_tier(4)])

	var p: PlayerPlane = PLANE_SCENE.instantiate()
	root.add_child(p)
	await process_frame
	await process_frame
	p.set_physics_process(false)
	p.position = Vector2(270.0, 800.0)
	p.weapon.tier = 2
	p.step(0.0, DT)
	var at2 := p.escorts.size()
	p.weapon.tier = 3
	p.step(0.0, DT)
	var at3 := p.escorts.size()
	p.weapon.tier = 4
	p.step(0.0, DT)
	var at4 := p.escorts.size()
	p.weapon.tier = 1
	p.step(0.0, DT)
	check("escort_nodes_follow_tier_up_and_down", at2 == 0 and at3 == 1 and at4 == 2 \
		and p.escorts.size() == 0,
		"t2=%d t3=%d t4=%d back=%d" % [at2, at3, at4, p.escorts.size()])

	p.weapon.tier = 4
	p.step(0.0, DT)
	var visual_ok: bool = p.escorts.size() == 2
	for e in p.escorts:
		visual_ok = visual_ok and e._sprite != null \
			and is_equal_approx(e._sprite.scale.x, Feel.PLANE_SPRITE_SCALE * Feel.ESCORT_SCALE)
	var radii_ok := true
	for i in 30:
		p.step(0.0, DT)
		for e in p.escorts:
			radii_ok = radii_ok \
				and absf(e.position.length() - Feel.ESCORT_ORBIT_RADIUS_PX) < 0.01
	var opposite: bool = p.escorts[0].position.dot(p.escorts[1].position) < 0.0
	check("escort_small_copy_orbits_fixed_radius", visual_ok and radii_ok and opposite,
		"visual=%s radii=%s opposite=%s" % [visual_ok, radii_ok, opposite])

	var pool_e := BulletPool.new()
	pool_e.setup(true, 64)
	root.add_child(pool_e)
	p.weapon.pool = pool_e
	var volleys0 := p.weapon.volleys_fired
	var shots0 := p.escort_shots
	pool_e.clear_all()
	p.weapon.fire_cooldown = 0.0
	p.step(0.0, DT)   # one volley: main guns + both options
	var spots: Array[Vector2] = []
	for e in p.escorts:
		spots.append(e.global_position)   # positions set before the fire beat
	var from_escorts := 0
	var straight_up := true
	for b in pool_e.live_bullets():
		var at_spot := false
		for sp in spots:
			if b.position.distance_to(sp) < 1.0:
				at_spot = true
		if at_spot:
			from_escorts += 1
			straight_up = straight_up and b.velocity.x == 0.0 and b.velocity.y < 0.0
	check("escort_fire_parallel_to_main", p.weapon.volleys_fired == volleys0 + 1 \
		and from_escorts == 2 and straight_up and p.escort_shots == shots0 + 2 \
		and pool_e.active_count() == 3 + 2,
		"volleys=%d escort_bullets=%d straight=%s escort_shots=%d live=%d" % [
		p.weapon.volleys_fired - volleys0, from_escorts, straight_up,
		p.escort_shots - shots0, pool_e.active_count()])

	var v1 := p.weapon.volleys_fired
	var s1 := p.escort_shots
	pool_e.clear_all()
	for i in 60:
		p.step(0.0, DT)
	check("escort_fire_cadence_matches_main", p.weapon.volleys_fired - v1 == int(Feel.FIRE_RATE) \
		and p.escort_shots - s1 == 2 * int(Feel.FIRE_RATE),
		"volleys=%d escort_shots=%d rate=%f" % [p.weapon.volleys_fired - v1,
		p.escort_shots - s1, Feel.FIRE_RATE])
	p.queue_free()

	# ==================== act 2: unmarked_jet ==============================
	var tgt := Node2D.new()
	tgt.position = Vector2(400.0, 700.0)
	root.add_child(tgt)
	var jpool := BulletPool.new()
	jpool.setup(false, 32)
	root.add_child(jpool)
	var jet := Enemy.UnmarkedJet.new()
	jet.pool = jpool
	jet.target = tgt
	jet.position = Vector2(270.0, Feel.STAGE_SPAWN_Y)
	root.add_child(jet)
	var jy0 := jet.position.y
	for i in 120:   # 2 s: burst at 0.7 s, the next would be due at ~2.9 s
		jet.step(DT)
	var bolts := jpool.live_bullets()
	var aimed_unit := Vector2.ZERO
	if bolts.size() > 0:
		aimed_unit = bolts[0].velocity.normalized()
	var jet_at_fire := Vector2(270.0, jy0 + Feel.JET_SPEED * jet.burst_times[0])
	var to_target := (tgt.position - jet_at_fire).normalized()
	var burst_gap := 0.0
	if jet.burst_times.size() == 2:
		burst_gap = jet.burst_times[1] - jet.burst_times[0]
	check("unmarked_jet_fast_dives_two_bolt_burst", Feel.JET_SPEED > Feel.FIGHTER_SPEED * 1.5 \
		and jet.kind == "jet" and jet.shots_fired == 2 and bolts.size() == 2 \
		and aimed_unit.dot(to_target) > 0.99 \
		and nearf(jet.position.y - jy0, Feel.JET_SPEED * 2.0, 0.5) \
		and nearf(burst_gap, Feel.JET_BURST_GAP_SEC, DT),
		"speed=%f shots=%d live=%d dot=%f gap=%f" % [Feel.JET_SPEED, jet.shots_fired,
		bolts.size(), aimed_unit.dot(to_target), burst_gap])
	jet.queue_free()

	# ==================== act 2: energy_turret =============================
	var tur := Enemy.EnergyTurret.new()
	tur.position = Vector2(Feel.TURRET_EDGE_INSET_PX, 400.0)
	tur.edge_dir = 1.0
	root.add_child(tur)
	var tpos := tur.position
	var telegraph_seen := 0.0
	var beam_frames := 0
	var telegraph_frames := 0
	for i in 300:   # 5 s: charge 1.0 -> telegraph 1.0 -> beam 1.2 -> recharge
		tur.step(DT)
		if tur.state == Enemy.EnergyTurret.ST_TELEGRAPH:
			telegraph_frames += 1
			telegraph_seen = tur.telegraph_progress()
		if tur.is_beam_active():
			beam_frames += 1
	check("turret_stationary_then_sweeps_telegraph_to_beam", tur.position == tpos \
		and telegraph_frames == int(Feel.TURRET_TELEGRAPH_SEC * 60.0) \
		and beam_frames == int(Feel.TURRET_BEAM_SEC * 60.0) \
		and tur.beam_shots == 1 and tur.state == Enemy.EnergyTurret.ST_CHARGE \
		and telegraph_seen > 0.9,
		"telegraphs=%d beams=%d shots=%d state=%d prog=%f" % [telegraph_frames,
		beam_frames, tur.beam_shots, tur.state, telegraph_seen])

	tur.state = Enemy.EnergyTurret.ST_BEAM
	tur.beam_y = 400.0
	var row_hit: bool = tur.beam_hits(Vector2(270.0, 400.0 + Feel.TURRET_BEAM_WIDTH_PX * 0.5))
	var row_miss: bool = tur.beam_hits(Vector2(270.0, 400.0 + Feel.TURRET_BEAM_WIDTH_PX * 0.5 + 60.0))
	tur.state = Enemy.EnergyTurret.ST_CHARGE
	var cold: bool = not tur.beam_hits(Vector2(270.0, 400.0))
	check("turret_beam_burns_only_its_row_when_hot", row_hit and not row_miss and cold,
		"hit=%s miss=%s cold=%s" % [row_hit, not row_miss, cold])
	tur.queue_free()

	# ==================== act 2 boss: proto_mech ===========================
	var mpool := BulletPool.new()
	mpool.setup(false, 96)
	root.add_child(mpool)
	var mech := ProtoMech.new()
	mech.pool = mpool
	mech.target = tgt
	mech.position = Vector2(270.0, Feel.STAGE_BOSS_SPAWN_Y)
	root.add_child(mech)
	var guard := 0
	while mech.phase == ProtoMech.PH_ENTER and guard < 2000:
		mech.step(DT)
		guard += 1
	var entered: bool = mech.phase == ProtoMech.PH_ONE \
		and nearf(mech.position.y, Feel.PROTO_ENTER_Y, 1.0) and mech.max_hp == Feel.PROTO_HP
	guard = 0
	while mech.missile_volleys < 1 and guard < 600:
		mech.step(DT)
		guard += 1
	for i in 36:   # let the volley finish spawning (3 gaps x ~10 frames)
		mech.step(DT)
	var lobbed := 0
	var grav_ok := true
	var lobbed_up := true
	for b in mpool.live_bullets():
		lobbed += 1
		grav_ok = grav_ok and b.gravity == Feel.PROTO_MISSILE_GRAVITY
		lobbed_up = lobbed_up and b.velocity.y < 0.0
	check("proto_mech_enters_and_opens_with_missile_arc", entered \
		and mech.missile_volleys == 1 and lobbed == Feel.PROTO_MISSILE_COUNT \
		and grav_ok and lobbed_up and mech.laser_fired == 0 \
		and mech.telegraphs_started == 0,
		"entered=%s volleys=%d lobbed=%d grav=%s up=%s lasers=%d tgs=%d" % [
		entered, mech.missile_volleys, lobbed, grav_ok, lobbed_up,
		mech.laser_fired, mech.telegraphs_started])

	var volleys_at_laser := mech.missile_volleys
	guard = 0
	while mech.telegraphs_started < 1 and guard < 1200:
		mech.step(DT)
		guard += 1
	var telegraph_started: bool = mech.telegraphs_started == 1 \
		and mech.missile_volleys == volleys_at_laser   # phase 1 alternates, never both
	guard = 0
	while not mech.is_beam_active() and guard < 1200:
		mech.step(DT)
		guard += 1
	var beam_row: bool = mech.beam_hits(Vector2(60.0, mech.laser_y)) \
		and not mech.beam_hits(Vector2(60.0, mech.laser_y + 200.0))
	check("proto_mech_cycles_missiles_then_telegraphed_laser", telegraph_started \
		and mech.laser_fired == 1 and mech.is_beam_active() and beam_row,
		"alternated=%s lasers=%d beam=%s row=%s" % [telegraph_started,
		mech.laser_fired, mech.is_beam_active(), beam_row])

	var volleys_at_p2 := mech.missile_volleys
	var was_p2: bool = mech.phase2()
	var phase_swapped: bool = not was_p2 and not mech.take_damage(Feel.PROTO_PHASE2_HP) \
		and mech.phase2() and mech.hp == Feel.PROTO_PHASE2_HP
	guard = 0
	var saw_both := false
	while not saw_both and guard < 2400:
		mech.step(DT)
		saw_both = mech.is_beam_active() and mech.missile_volleys > volleys_at_p2
		guard += 1
	check("proto_mech_phase2_at_half_runs_both_patterns", phase_swapped and saw_both,
		"swapped=%s both=%s hp=%d" % [phase_swapped, saw_both, mech.hp])
	mech.queue_free()

	# ==================== act 2 loads after the act-1 boss clear ===========
	var s2 := await make_stage()
	s2.invincible = true
	s2.spawn_boss()
	var boss1 = s2.boss
	s2.boss.take_damage(Feel.BOSS1_HP * 3)
	var banner_seen := ""
	guard = 0
	while s2.act == 1 and guard < 6000:
		s2.step(DT)
		if s2.state == Stage.ST_BANNER and banner_seen == "":
			banner_seen = s2.banner_text()
		guard += 1
	check("act2_loads_after_act1_boss_clear", s2.act == 2 and s2.state == Stage.ST_WAVES \
		and s2.act_time < DT * 2.0 and boss1 is BossFortress \
		and banner_seen == Feel.ACT2_TITLE,
		"act=%d state=%d act_time=%f boss1=%s banner=%s" % [s2.act, s2.state,
		s2.act_time, boss1 is BossFortress, banner_seen])

	while s2.act_time < 12.0:
		s2.step(DT)
	check("act2_waves_mix_act1_and_act2_types", s2.jets_spawned > 0 \
		and s2.turrets_spawned > 0 and s2.fighters_spawned > 0 and s2.bombers_spawned > 0,
		"jets=%d turrets=%d fighters=%d bombers=%d" % [s2.jets_spawned,
		s2.turrets_spawned, s2.fighters_spawned, s2.bombers_spawned])

	guard = 0
	while s2.boss == null and guard < 20000:
		s2.step(DT)
		guard += 1
	check("act2_stage_sends_proto_mech_after_stage_len", s2.boss is ProtoMech \
		and s2.boss.max_hp == Feel.PROTO_HP and s2.state == Stage.ST_BOSS \
		and s2.act == 2 and s2.act_time >= Feel.STAGE_LEN_SEC - DT,
		"boss=%s hp=%d state=%d act=%d act_time=%f" % [s2.boss is ProtoMech,
		s2.boss.max_hp, s2.state, s2.act, s2.act_time])

	# ==================== pickup drop rate (seeded, statistical) ===========
	var s3 := await make_stage()
	for i in 300:
		var e := Enemy.FighterStraight.new()
		s3.track_enemy(e)
		e.take_damage(3)
	var rate := float(s3.pickups_spawned) / float(maxi(s3.kills, 1))
	var expect := Feel.PICKUP_DROP_CHANCE
	check("pickup_drop_rate_seeded_statistical", s3.kills == 300 \
		and is_equal_approx(expect, 0.22) and rate > expect * 0.6 and rate < expect * 1.4,
		"kills=%d drops=%d rate=%f expect=%f" % [s3.kills, s3.pickups_spawned, rate, expect])

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


func check(name: String, ok: bool, detail := "") -> void:
	if ok:
		passes += 1
		print("PASS: " + name)
	else:
		fails += 1
		print("FAIL: " + name + "  (" + detail + ")")


func nearf(a: float, b: float, eps: float) -> bool:
	return absf(a - b) < eps
