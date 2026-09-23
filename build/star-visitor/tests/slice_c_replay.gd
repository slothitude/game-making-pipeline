extends SceneTree
## Slice C headless replays (spec law: headless_replays) — the boss stage driven
## on the real scene tree with scripted input. Run with:
##   godot --headless --path . --script res://tests/slice_c_replay.gd
## Run 1 "slayer" (~60s): shoot through wave 1, duel the robot (dodge shockwaves
##   and the laser slam), drop it, and meet wave 2's heavies.
## Run 2 "turtle" (~45s): no shooting at all — burrow-drag wave 1, then survive
##   the robot underground. Green = no errors, no soft-locks, no cheap kills.

var passed := 0
var failed := 0

const SLAYER_FRAMES := 4500
const TURTLE_FRAMES := 3600


func _initialize() -> void:
	create_timer(420.0).timeout.connect(_watchdog)
	call_deferred("_run_all")


func _watchdog() -> void:
	print("WATCHDOG: replay exceeded 420s, aborting")
	quit(2)


func expect(cond: bool, label: String) -> void:
	if cond:
		passed += 1
		print("  PASS  " + label)
	else:
		failed += 1
		print("  FAIL  " + label)


func wait_frames(n: int) -> void:
	for i in n:
		await physics_frame


func _load_main() -> Node:
	var m: Node = (load("res://scenes/main.tscn") as PackedScene).instantiate()
	root.add_child(m)
	return m


func _release_all() -> void:
	for a in ["move_left", "move_right", "move_up", "move_down", "fire", "jump"]:
		Input.action_release(a)


func _hazards(m: Node) -> Array:
	return m.get_tree().get_nodes_in_group(Feel.HAZARD_GROUP)


func _count_distinct_hazards(m: Node, seen: Dictionary, kind: String) -> void:
	for hz in _hazards(m):
		if is_instance_valid(hz) and hz.get("kind") == kind:
			seen[hz.get_instance_id()] = true


func _threat_jump(m: Node, p: Player) -> bool:
	# true when a hazard is about to be where the hero stands: jump it
	for hz in _hazards(m):
		if not is_instance_valid(hz):
			continue
		if hz.get("kind") == "shockwave":
			var dx: float = p.global_position.x - hz.global_position.x
			if absf(dx) < 160.0 and signf(dx) == signf(float(hz.dir)):
				return true
		elif hz.get("kind") == "laser":
			if hz.h < 80.0:
				return true
	return false


func _run_slayer() -> void:
	print("[replay 1: slayer — shoot wave 1, duel the robot, meet the heavies]")
	var m := _load_main()
	await wait_frames(3)
	var p = m.player
	var boss_seen := false
	var max_phase := 1
	var waves_seen := {}
	var laser_seen := {}
	var saw_heavy := false
	var wave2_f := -1
	var f := 0
	while f < SLAYER_FRAMES:
		await physics_frame
		f += 1
		if not boss_seen and m.boss != null and is_instance_valid(m.boss):
			boss_seen = true
		if boss_seen and is_instance_valid(m.boss):
			max_phase = maxi(max_phase, m.boss.phase)
		_count_distinct_hazards(m, waves_seen, "shockwave")
		_count_distinct_hazards(m, laser_seen, "laser")
		if m.wave2_started:
			if wave2_f < 0:
				wave2_f = f
			if not saw_heavy:
				for e in m.wave_enemies:
					if is_instance_valid(e) and e is HeavyAgent:
						saw_heavy = true
			if f > wave2_f + 240:
				break
		if p == null or not is_instance_valid(p) or p.dead:
			continue
		if _threat_jump(m, p) and p.is_on_floor():
			p.try_jump()
		if f % 26 == 0:
			Input.action_press("fire")
		elif f % 26 == 18:
			Input.action_release("fire")
	_release_all()
	print("  [slayer] frames=%d boss=%s phase=%d waves=%d lasers=%d score=%d kills=%d lives=%d" %
			[f, str(boss_seen), max_phase, waves_seen.size(), laser_seen.size(), m.score, m.kills, p.lives])
	expect(m.spawned_count == Feel.WAVE1_AGENT_COUNT, "wave 1 spawner completed (%d/6)" % m.spawned_count)
	expect(boss_seen, "slayer reached the miniboss stage")
	expect(max_phase == 2, "slayer pushed the robot into phase 2 (max %d)" % max_phase)
	expect(waves_seen.size() >= 1, "dodged (or ate) a stomp shockwave (%d)" % waves_seen.size())
	expect(laser_seen.size() >= 1, "dodged (or ate) a laser sweep (%d)" % laser_seen.size())
	expect(m.boss_done, "robot dropped (boss_done)")
	expect(m.boss == null or not is_instance_valid(m.boss) or m.boss.hp <= 0, "robot is off the field")
	expect(m.score >= Feel.SCORE_PER_KILL * Feel.WAVE1_AGENT_COUNT + Feel.BOSS_BONUS,
			"boss bonus banked (score %d)" % m.score)
	expect(m.kills >= Feel.WAVE1_AGENT_COUNT + 1, "kill counter includes the robot (%d)" % m.kills)
	expect(m.wave2_started, "wave 2 rolled in behind the robot")
	expect(m.wave2_spawned == Feel.WAVE2_AGENT_COUNT and m.wave2_heavy_spawned == Feel.WAVE2_HEAVY_COUNT,
			"wave 2 roster complete (%d agents + %d heavies)" % [m.wave2_spawned, m.wave2_heavy_spawned])
	expect(saw_heavy, "heavies on the field")
	expect(p.lives >= 0, "lives never negative (%d)" % p.lives)
	expect(not m.game_ended or p.lives == 0, "no soft-lock at game over (ended=%s)" % str(m.game_ended))
	expect(Engine.get_physics_frames() > 0, "engine kept stepping")
	m.queue_free()
	await process_frame
	await process_frame


func _run_turtle() -> void:
	print("[replay 2: turtle — burrow-only survival through the robot]")
	var m := _load_main()
	await wait_frames(3)
	var p = m.player
	var cycles := 0
	var max_t := 0.0
	var waves_seen := {}
	var laser_seen := {}
	var boss_seen := false
	var boss_f := -1
	var f := 0
	while f < TURTLE_FRAMES:
		await physics_frame
		f += 1
		if not boss_seen and m.boss != null and is_instance_valid(m.boss):
			boss_seen = true
			boss_f = f
		_count_distinct_hazards(m, waves_seen, "shockwave")
		_count_distinct_hazards(m, laser_seen, "laser")
		var hazards_alive := _hazards_live(m, "shockwave") or _hazards_live(m, "laser")
		if p == null or not is_instance_valid(p) or p.dead:
			_release_all()
			continue
		if p.burrow.burrowing:
			max_t = maxf(max_t, p.burrow.t)
			# surface only into a hazard-free gap; the clock never gets close to suffocation
			if (p.burrow.t >= 1.5 and not hazards_alive) or f >= TURTLE_FRAMES - 120:
				Input.action_release("move_down")
				Input.action_release("move_left")
				Input.action_release("move_right")
				cycles += 1
			continue
		var agent := _nearest_agent(m, p)
		if agent != null:
			# wave 1: walk under them and drag them down
			var dx: float = agent.global_position.x - p.global_position.x
			if absf(dx) > 24.0:
				Input.action_release("move_down")
				Input.action_release("move_left")
				Input.action_release("move_right")
				Input.action_press("move_right" if dx > 0.0 else "move_left")
			else:
				Input.action_release("move_left")
				Input.action_release("move_right")
				Input.action_press("move_down")
		else:
			# robot stage (or the gap before it): hold underground, surface only when clear
			Input.action_release("move_left")
			Input.action_release("move_right")
			Input.action_press("move_down")
	_release_all()
	print("  [turtle] frames=%d boss=%s cycles=%d max_t=%.2f waves=%d lasers=%d lives=%d" %
			[f, str(boss_seen), cycles, max_t, waves_seen.size(), laser_seen.size(), p.lives])
	expect(boss_seen, "turtle survived into the miniboss stage")
	expect(waves_seen.size() >= 1, "rode out a stomp underground (%d seen)" % waves_seen.size())
	expect(laser_seen.size() >= 1, "rode out a laser underground (%d seen)" % laser_seen.size())
	expect(cycles >= 2, "turtle cycled burrow/surface (%d cycles)" % cycles)
	expect(max_t < Feel.BURROW_SUFFOCATE_TIME, "never suffocated (max %.2fs of %.1fs)" % [max_t, Feel.BURROW_SUFFOCATE_TIME])
	expect(is_instance_valid(m.boss) and m.boss.hp == Feel.BOSS_ROBOT_HP,
			"no cheap damage: robot still at full hp under a pacifist (hp %d)" % (m.boss.hp if is_instance_valid(m.boss) else -1))
	expect(m.boss.phase == 1, "pacifist never triggers phase 2")
	expect(p.lives > 0, "turtle lives through the robot (lives %d)" % p.lives)
	expect(not p.burrow.burrowing, "turtle is not stuck underground at the end")
	expect(not m.game_ended, "no game over on a pure-burrow run")
	expect(Engine.get_physics_frames() > 0, "engine kept stepping")
	m.queue_free()
	await process_frame
	await process_frame


func _hazards_live(m: Node, kind: String) -> bool:
	return not _hazards_of_kind(m, kind).is_empty()


func _hazards_of_kind(m: Node, kind: String) -> Array:
	var out: Array = []
	for hz in _hazards(m):
		if is_instance_valid(hz) and hz.get("kind") == kind:
			out.append(hz)
	return out


func _nearest_agent(m: Node, from: Node2D) -> EnemyAgent:
	var best: EnemyAgent = null
	var best_d := INF
	for e in m.get_tree().get_nodes_in_group("enemy"):
		var enemy := e as EnemyAgent
		if enemy == null or enemy.hp <= 0 or enemy.mounted or enemy.state == EnemyAgent.State.THROWN:
			continue
		var d: float = absf(enemy.global_position.x - from.global_position.x)
		if d < best_d:
			best_d = d
			best = enemy
	return best


func _run_all() -> void:
	await process_frame
	print("=== STAR VISITOR slice C replays ===")
	await _run_slayer()
	await _run_turtle()
	print("=== SLICE C REPLAYS: %d passed, %d failed ===" % [passed, failed])
	quit(0 if failed == 0 else 1)
