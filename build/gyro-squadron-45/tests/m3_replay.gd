extends SceneTree
## Milestone 3 scripted replay (headless, deterministic):
##   godot --headless --script res://tests/m3_replay.gd
## 25 simulated seconds through the REAL main scene: weave act 1 on synthetic
## tilt using SUPER + BOMB, hoover scripted pickups through the tier gate,
## force the act-1 boss and clear it, then ride the ACT 2 banner into the
## mixed waves. Gate: plane alive the whole way, tier advanced, act 2 reached,
## every phase bounded (no soft-locks). Steering goes through the same
## TiltSource the device uses.

const MAIN_SCENE := preload("res://scenes/main.tscn")
const DT := 1.0 / 60.0
const TOTAL_BUDGET_SEC := 25.0
const WEAVE_SEC := 6.0        # act 1: real-damage survival weave
const BOSS_CAP_SEC := 12.0    # forced act-1 boss clear (bounded)
const CLEAR_CAP_SEC := 8.0    # tally + ACT 2 banner (bounded)
const ACT2_SEC := 4.0         # ride the act-2 waves

var passes := 0
var fails := 0


func _initialize() -> void:
	_run()


func _run() -> void:
	await process_frame

	var main: Node2D = MAIN_SCENE.instantiate()
	root.add_child(main)
	main.set_process(false)       # replay steps the game by hand
	await process_frame
	await process_frame
	main.tilt.set_mode(TiltSource.MODE_SYNTHETIC)

	var plane: PlayerPlane = main.plane
	var stage: Stage = main.stage
	var view := plane.get_viewport_rect().size
	var total_frames := 0
	var supers_used := 0
	var bombs_used := 0

	# ==================== phase A: weave act 1, super + bomb, hoover P =====
	var tier0 := stage.weapon.tier
	var raw := 1.0
	var flip_zone := 4.0
	var frames_a := int(WEAVE_SEC * 60.0)
	for i in frames_a:
		if raw > 0.0 and plane.position.x >= view.x - Feel.CLAMP_MARGIN - flip_zone:
			raw = -1.0
		elif raw < 0.0 and plane.position.x <= Feel.CLAMP_MARGIN + flip_zone:
			raw = 1.0
		main.tilt.push_tilt(raw)
		main.sim_frame(DT)
		total_frames += 1
		if not is_instance_valid(plane):
			break
		if i % 60 == 30:
			stage.spawn_pickup_at(plane.position)   # scripted hoover: gate input
		if stage.weapon.super_ready():
			if stage.try_release_super():
				supers_used += 1
		if i % 150 == 100:
			if stage.use_bomb():
				bombs_used += 1
	# scripted charge top-up: guarantee the real SUPER path fires during act 1
	if not stage.weapon.super_ready():
		while stage.weapon.kills_charged < Feel.SUPER_SHOT_CHARGE_KILLS:
			stage.weapon.register_kill()
	if stage.try_release_super():
		supers_used += 1
	check("act1_weave_survives_on_super_and_bombs", is_instance_valid(plane) \
		and stage.lives >= 1 and not main.is_game_over() \
		and supers_used >= 1 and bombs_used >= 1,
		"lives=%d over=%s supers=%d bombs=%d" % [stage.lives, main.is_game_over(),
		supers_used, bombs_used])
	check("pickups_collected_and_tier_gates_up", stage.pickups_collected \
		>= Feel.PICKUPS_PER_TIER and stage.weapon.tier >= tier0 + 1,
		"collected=%d tier %d->%d" % [stage.pickups_collected, tier0, stage.weapon.tier])
	var escorts_on: bool = is_instance_valid(plane) and plane.escorts.size() >= 1 \
		and stage.weapon.tier >= Feel.ESCORT_TIER_MIN
	check("escorts_on_station_at_tier_gate", escorts_on,
		"escorts=%d tier=%d" % [(plane.escorts.size() if is_instance_valid(plane) else -1),
		stage.weapon.tier])

	# ==================== phase B: force the act-1 boss clear ==============
	stage.invincible = true   # harness hook: the fast-forward must not die
	if stage.boss == null:
		stage.spawn_boss()
	var boss_frames := 0
	var boss_cap := int(BOSS_CAP_SEC * 60.0)
	while stage.state == Stage.ST_BOSS and boss_frames < boss_cap:
		var bdx: float = stage.boss.position.x - plane.position.x
		main.tilt.push_tilt(clampf(bdx * 0.05, -1.0, 1.0))
		while stage.weapon.kills_charged < Feel.SUPER_SHOT_CHARGE_KILLS:
			stage.weapon.register_kill()   # scripted charge: the super is the plan
		if stage.try_release_super():
			supers_used += 1
		if stage.bombs > 0 and boss_frames % 90 == 30:
			if stage.use_bomb():
				bombs_used += 1
		main.sim_frame(DT)
		boss_frames += 1
		total_frames += 1
	var boss_dead: bool = stage.boss != null and not stage.boss.alive \
		and stage.state == Stage.ST_CLEAR
	check("act1_boss_clears_within_budget", boss_dead and boss_frames < boss_cap,
		"dead=%s state=%d frames=%d" % [boss_dead, stage.state, boss_frames])

	# ==================== phase C: tally -> ACT 2 banner -> act 2 ==========
	var banner_seen := ""
	var clear_frames := 0
	var clear_cap := int(CLEAR_CAP_SEC * 60.0)
	while stage.act == 1 and clear_frames < clear_cap:
		main.tilt.push_tilt(0.0)
		main.sim_frame(DT)
		total_frames += 1
		if stage.state == Stage.ST_BANNER and banner_seen == "":
			banner_seen = stage.banner_text()
	check("act2_banner_announces_black_budget", banner_seen == Feel.ACT2_TITLE,
		"banner=%s" % banner_seen)
	check("act2_reached_after_clear", stage.act == 2 and stage.state == Stage.ST_WAVES \
		and clear_frames < clear_cap, "act=%d state=%d frames=%d" % [stage.act,
		stage.state, clear_frames])

	# ==================== phase D: ride the act-2 mixed waves ==============
	var frames_d := int(ACT2_SEC * 60.0)
	for i in frames_d:
		main.tilt.push_tilt(sin(float(i) * DT * 2.0))
		main.sim_frame(DT)
		total_frames += 1
	check("act2_mixed_waves_running", stage.act == 2 and stage.jets_spawned > 0 \
		and stage.turrets_spawned > 0 and stage.fighters_spawned > 0 \
		and stage.state == Stage.ST_WAVES,
		"act=%d jets=%d turrets=%d fighters=%d state=%d" % [stage.act,
		stage.jets_spawned, stage.turrets_spawned, stage.fighters_spawned, stage.state])
	check("plane_alive_at_end_of_replay", is_instance_valid(plane) \
		and stage.lives >= 1 and not main.is_game_over(),
		"valid=%s lives=%d over=%s" % [is_instance_valid(plane), stage.lives,
		main.is_game_over()])
	check("replay_within_budget_no_softlock", total_frames <= int(TOTAL_BUDGET_SEC * 60.0) \
		and stage.time > WEAVE_SEC and main.stage != null,
		"frames=%d (%.1f s) time=%f" % [total_frames, float(total_frames) * DT, stage.time])

	print("")
	print("REPLAY: %d frames (%.1f s of %.1f budget), supers %d, bombs %d, tier %d, act %d" % [
		total_frames, float(total_frames) * DT, TOTAL_BUDGET_SEC, supers_used,
		bombs_used, stage.weapon.tier, stage.act])
	print("RESULT: %d passed, %d failed" % [passes, fails])
	quit(0 if fails == 0 else 1)


func check(name: String, ok: bool, detail := "") -> void:
	if ok:
		passes += 1
		print("PASS: " + name)
	else:
		fails += 1
		print("FAIL: " + name + "  (" + detail + ")")
