extends SceneTree
## Slice C battery (growing_battery law): heavy enemy / miniboss robot / mixed wave 2.
## Run with:
##   godot --headless --path . --script res://tests/slice_c_tests.gd
## Must pass TWICE consecutively. Exit code 0 = all green.
## Slice A and B suites must keep passing.

var passed := 0
var failed := 0


func _initialize() -> void:
	create_timer(240.0).timeout.connect(_watchdog)
	call_deferred("_run_all")


func _watchdog() -> void:
	print("WATCHDOG: battery exceeded 240s, aborting")
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


func _run_all() -> void:
	await process_frame
	print("=== STAR VISITOR slice C battery ===")
	await _test_feel_c_constants()
	await _test_heavy_stats()
	await _test_heavy_toughness()
	await _test_heavy_burst()
	await _test_heavy_slow_advance()
	await _test_heavy_visual()
	await _test_miniboss_stats()
	await _test_miniboss_stationary()
	await _test_miniboss_attacks()
	await _test_shockwave()
	await _test_laser_sweep()
	await _test_miniboss_phase2()
	await _test_miniboss_contact()
	await _test_boss_signature_immunity()
	await _test_wave_flow_c()
	print("=== SLICE C BATTERY: %d passed, %d failed ===" % [passed, failed])
	quit(0 if failed == 0 else 1)


# -- fixtures (arena floor at real room ground height y=480) --

func make_arena(tag: String) -> Node2D:
	var a := Node2D.new()
	a.name = tag
	root.add_child(a)
	add_block(a, Rect2(-480, 480, 1920, 60))
	return a


func add_block(parent: Node, r: Rect2) -> void:
	var body := StaticBody2D.new()
	body.collision_layer = 1
	body.collision_mask = 0
	body.add_to_group("world")
	var cs := CollisionShape2D.new()
	var box := RectangleShape2D.new()
	box.size = r.size
	cs.shape = box
	body.add_child(cs)
	body.position = r.get_center()
	parent.add_child(body)
	var rect := ColorRect.new()
	rect.position = r.position
	rect.size = r.size
	rect.color = Feel.COLOR_WORLD
	parent.add_child(rect)


func spawn_player(a: Node2D, pos: Vector2 = Vector2(0, 456)) -> Player:
	var p: Player = (load("res://scenes/player.tscn") as PackedScene).instantiate()
	p.position = pos
	a.add_child(p)
	return p


func spawn_heavy(a: Node2D, pos: Vector2) -> HeavyAgent:
	var e: HeavyAgent = (load("res://scenes/heavy.tscn") as PackedScene).instantiate()
	e.position = pos
	a.add_child(e)
	return e


func spawn_agent(a: Node2D, pos: Vector2) -> EnemyAgent:
	var e: EnemyAgent = (load("res://scenes/enemy.tscn") as PackedScene).instantiate()
	e.position = pos
	a.add_child(e)
	return e


func spawn_boss(a: Node2D, pos: Vector2 = Vector2(700, 414)) -> MiniBossRobot:
	var b: MiniBossRobot = (load("res://scenes/miniboss.tscn") as PackedScene).instantiate()
	b.position = pos
	a.add_child(b)
	return b


func make_dummy(a: Node2D, pos: Vector2) -> Node2D:
	var d := Node2D.new()
	d.position = pos
	a.add_child(d)
	return d


func drop(n: Node) -> void:
	Input.action_release("move_down")
	Input.action_release("move_left")
	Input.action_release("move_right")
	Input.action_release("fire")
	Input.action_release("jump")
	if is_instance_valid(n):
		n.queue_free()
	await process_frame
	await process_frame


func hazards_of(tree: SceneTree, kind: String) -> Array:
	var out: Array = []
	for h in tree.get_nodes_in_group(Feel.HAZARD_GROUP):
		if is_instance_valid(h) and h.get("kind") == kind:
			out.append(h)
	return out


# -- tests --

func _test_feel_c_constants() -> void:
	print("[feel slice-C constants]")
	expect(Feel.ENEMY_HEAVY_HP == 3 and Feel.ENEMY_HEAVY_SPEED == 60.0,
			"heavy hp/speed per roster_v1 (hp %d, speed %.0f)" % [Feel.ENEMY_HEAVY_HP, Feel.ENEMY_HEAVY_SPEED])
	expect(Feel.ENEMY_HEAVY_BURST == 3 and Feel.ENEMY_HEAVY_BURST_GAP > 0.0,
			"heavy burst-fire 3 with a gap clock")
	expect(Feel.BOSS_ROBOT_HP == 30 and Feel.BOSS_ROBOT_PHASE2_AT_HP == 15,
			"miniboss hp 30, phase2 at 15 per bosses_v1")
	expect(Feel.MINIBOSS_SHOCKWAVE_SPEED > 0.0 and Feel.MINIBOSS_LASER_TIME > 0.0
			and Feel.MINIBOSS_LASER_HIGH_Y > Feel.MINIBOSS_LASER_LOW_Y
			and Feel.MINIBOSS_ATTACK_CYCLE > Feel.MINIBOSS_PHASE2_ATTACK_CYCLE,
			"shockwave/laser/cycle tuning sane (phase2 faster)")
	expect(Feel.WAVE2_AGENT_COUNT == 6 and Feel.WAVE2_HEAVY_COUNT == 4
			and Feel.WAVE2_TOTAL == 10 and Feel.MINIBOSS_DELAY > 0.0,
			"wave 2 goes mixed agents+heavies x10 after the miniboss")


func _test_heavy_stats() -> void:
	print("[heavy stats]")
	var a := make_arena("t_hstats")
	var e := spawn_heavy(a, Vector2(0, 457))
	await wait_frames(5)
	expect(e is EnemyAgent and e.hp == Feel.ENEMY_HEAVY_HP and e.speed == Feel.ENEMY_HEAVY_SPEED,
			"heavy is an EnemyAgent with roster_v1 stats")
	expect(e.can_be_mounted(), "heavy keeps the ride hook (mountable)")
	await drop(a)


func _test_heavy_toughness() -> void:
	print("[heavy toughness: 3 hits to drop]")
	var a := make_arena("t_htough")
	var e := spawn_heavy(a, Vector2(0, 457))
	e.target = null
	await wait_frames(5)
	var deaths := [0]
	e.died.connect(func(_pts: int): deaths[0] += 1)
	e.take_hit(Feel.BULLET_DAMAGE)
	e.take_hit(Feel.BULLET_DAMAGE)
	expect(is_instance_valid(e) and e.hp == 1 and deaths[0] == 0,
			"two pistol rounds do not drop a heavy (hp %d)" % e.hp)
	e.take_hit(Feel.BULLET_DAMAGE)
	await wait_frames(3)
	expect(deaths[0] == 1 and not is_instance_valid(e), "third round drops it, died emitted once")
	await drop(a)


func _test_heavy_burst() -> void:
	print("[heavy burst-fire 3]")
	for setup in [["heavy", true], ["agent", false]]:
		var a := make_arena("t_burst_%s" % setup[0])
		var dummy := make_dummy(a, Vector2(100, 457))
		var e: EnemyAgent = spawn_heavy(a, Vector2(0, 457)) if setup[1] else spawn_agent(a, Vector2(0, 457))
		e.target = dummy
		await wait_frames(5)
		var guard := 0
		while e.state != EnemyAgent.State.SHOOT and guard < 300:
			await physics_frame
			guard += 1
		var seen := {}
		var f := 0
		while e.state == EnemyAgent.State.SHOOT and f < 120:
			await physics_frame
			for b in a.get_tree().get_nodes_in_group("bullet"):
				if b is Bullet and not b.from_player:
					seen[b.get_instance_id()] = true
			f += 1
		var want := Feel.ENEMY_HEAVY_BURST if setup[1] else 1
		expect(seen.size() == want, "%s fires %d round(s) per SHOOT (saw %d)" % [setup[0], want, seen.size()])
		await drop(a)


func _test_heavy_slow_advance() -> void:
	print("[heavy trades speed for armor]")
	var disp := {}
	for setup in [["agent", false], ["heavy", true]]:
		var a := make_arena("t_adv_%s" % setup[0])
		var dummy := make_dummy(a, Vector2(-400, 457))
		var e: EnemyAgent = spawn_heavy(a, Vector2(0, 457)) if setup[1] else spawn_agent(a, Vector2(0, 457))
		e.target = dummy
		await wait_frames(5)
		var x0 := e.global_position.x
		for i in 30:
			await physics_frame
		disp[setup[0]] = absf(e.global_position.x - x0)
		await drop(a)
	expect(disp["agent"] > disp["heavy"] + 5.0,
			"agent outruns the heavy (%.0f vs %.0f px)" % [disp["agent"], disp["heavy"]])
	expect(disp["heavy"] > 15.0, "heavy still advances (%.0f px)" % disp["heavy"])


func _test_heavy_visual() -> void:
	print("[heavy visual swap]")
	var a := make_arena("t_hvis")
	var e := spawn_heavy(a, Vector2(0, 457))
	await wait_frames(4)
	var tex_ok := false
	for c in e.vis.get_children():
		if c is Sprite2D and c.texture != null and c.texture.resource_path == Feel.TEX_ENEMY_HEAVY:
			tex_ok = true
	expect(tex_ok and e.vis.get_child_count() == 1, "heavy wears the heavy art (agent sprite swapped out)")
	await drop(a)


func _test_miniboss_stats() -> void:
	print("[miniboss stats]")
	var a := make_arena("t_bstats")
	var b := spawn_boss(a)
	await wait_frames(5)
	expect(b.hp == Feel.BOSS_ROBOT_HP and b.phase == 1, "robot arrives at %d hp, phase 1" % Feel.BOSS_ROBOT_HP)
	expect(b.is_in_group("enemy"), "robot is on the enemy roster (player bullets connect)")
	expect(not (b as Object is EnemyAgent), "robot is NOT an EnemyAgent (signature systems pass it by)")
	expect(not b.has_method("can_be_mounted") and not b.has_method("scare"),
			"robot cannot be mounted or scared")
	await drop(a)


func _test_miniboss_stationary() -> void:
	print("[miniboss holds its ground]")
	var a := make_arena("t_bhold")
	var b := spawn_boss(a, Vector2(700, 414))
	await wait_frames(5)
	expect(b.is_on_floor(), "robot stands on the floor")
	var moved := false
	for i in 60:
		await physics_frame
		if absf(b.velocity.x) > 0.1 or absf(b.global_position.x - 700.0) > 2.0:
			moved = true
	expect(not moved, "robot never walks (x %.0f, a floor-skimming target)" % b.global_position.x)
	await drop(a)


func _test_miniboss_attacks() -> void:
	print("[miniboss alternates stomp + laser]")
	var a := make_arena("t_batk")
	var b := spawn_boss(a, Vector2(700, 414))
	var dummy := make_dummy(a, Vector2(200, 457))
	b.target = dummy
	var kinds: Array = []
	b.attacked.connect(func(kind: String): kinds.append(kind))
	var saw_wave := false
	var saw_laser := false
	for i in 520:
		await physics_frame
		if not hazards_of(a.get_tree(), "shockwave").is_empty():
			saw_wave = true
		if not hazards_of(a.get_tree(), "laser").is_empty():
			saw_laser = true
		if kinds.size() >= 2 and not is_instance_valid(b):
			break
	expect(kinds.size() >= 2, "two attacks landed in two cycles (%s)" % str(kinds))
	expect(kinds[0] == "stomp", "the pattern opens with the stomp")
	expect("laser" in kinds, "the laser follows")
	expect(saw_wave and saw_laser, "both hazards spawn (wave=%s laser=%s)" % [str(saw_wave), str(saw_laser)])
	await drop(a)


func _test_shockwave() -> void:
	print("[stomp shockwave: floor skim, jumpable, lethal on the ground]")
	var a := make_arena("t_wave_kill")
	var p := spawn_player(a, Vector2(600, 456))
	await wait_frames(6)
	var w := MiniBossRobot.Shockwave.new(-1)
	a.add_child(w)
	w.position = Vector2(700, Feel.GROUND_RECT.position.y - Feel.MINIBOSS_SHOCKWAVE_SIZE.y * 0.5)
	var x0 := w.position.x
	for i in 20:
		await physics_frame
	var travelled := x0 - w.position.x
	expect(travelled > 80.0 and travelled < 120.0,
			"wave skims at Feel speed (%.0f px in 20 frames)" % travelled)
	var guard := 0
	while not p.dead and guard < 90:
		await physics_frame
		guard += 1
	expect(p.dead and p.lives == Feel.PLAYER_LIVES - 1, "a grounded hero in the path dies (lives %d)" % p.lives)
	await drop(a)

	var b := make_arena("t_wave_jump")
	add_block(b, Rect2(560, 400, 120, 16))
	var p2 := spawn_player(b, Vector2(620, 370))
	await wait_frames(12)
	expect(p2.is_on_floor(), "hero stands on the platform above the skim line")
	var w2 := MiniBossRobot.Shockwave.new(-1)
	b.add_child(w2)
	w2.position = Vector2(700, Feel.GROUND_RECT.position.y - Feel.MINIBOSS_SHOCKWAVE_SIZE.y * 0.5)
	var freed := false
	for i in 200:
		await physics_frame
		if not is_instance_valid(w2):
			freed = true
			break
	expect(freed, "wave expires at the far wall")
	expect(not p2.dead and p2.lives == Feel.PLAYER_LIVES,
			"height beats the shockwave (lives %d)" % p2.lives)
	await drop(b)


func _test_laser_sweep() -> void:
	print("[laser sweep: descends from on high, slams the floor line]")
	var a := make_arena("t_laser_kill")
	var p := spawn_player(a, Vector2(600, 456))
	await wait_frames(6)
	var beam := MiniBossRobot.LaserSweep.new(-1, 700, 700)
	a.add_child(beam)
	expect(absf(beam.h - Feel.MINIBOSS_LASER_HIGH_Y) < 0.1, "beam opens high and harmless (%.0f)" % beam.h)
	for i in 30:
		await physics_frame
	expect(beam.h < Feel.MINIBOSS_LASER_HIGH_Y - 20.0, "beam sweeps down (%.0f)" % beam.h)
	var guard := 0
	while not p.dead and guard < 120:
		await physics_frame
		guard += 1
	expect(p.dead, "a standing hero under the slam dies")
	for i in 40:
		await physics_frame
		if not is_instance_valid(beam):
			break
	expect(not is_instance_valid(beam), "beam lifts after MINIBOSS_LASER_TIME")
	await drop(a)

	var b := make_arena("t_laser_burrow")
	var p2 := spawn_player(b, Vector2(600, 456))
	await wait_frames(5)
	Input.action_press("move_down")
	await wait_frames(ceili(Feel.BURROW_ENTER_TIME * 60.0) + 4)
	expect(p2.burrow.burrowing, "hero buried under the sweep line")
	var beam2 := MiniBossRobot.LaserSweep.new(-1, 700, 700)
	b.add_child(beam2)
	beam2.h = Feel.MINIBOSS_LASER_LOW_Y
	await wait_frames(40)
	expect(p2.lives == Feel.PLAYER_LIVES and not p2.dead and not p2.hit(),
			"buried hero is beneath the slam (lives %d)" % p2.lives)
	Input.action_release("move_down")
	await drop(b)


func _test_miniboss_phase2() -> void:
	print("[miniboss phase 2 at hp %d]" % Feel.BOSS_ROBOT_PHASE2_AT_HP)
	var a := make_arena("t_bphase")
	var b := spawn_boss(a)
	var dummy := make_dummy(a, Vector2(200, 457))
	b.target = dummy
	var flips := [0]
	b.phase2_started.connect(func(): flips[0] += 1)
	await wait_frames(3)
	b.take_hit(14)
	expect(b.phase == 1, "hp %d still phase 1" % b.hp)
	b.take_hit(1)
	expect(b.phase == 2 and flips[0] == 1, "at hp %d the robot shifts into phase 2" % b.hp)
	expect(b._cycle() == Feel.MINIBOSS_PHASE2_ATTACK_CYCLE, "phase 2 attacks on the shorter cycle")
	var died_pts := [-1]
	b.died.connect(func(pts: int): died_pts[0] = pts)
	b.take_hit(b.hp)
	await wait_frames(3)
	expect(died_pts[0] == Feel.BOSS_BONUS and not is_instance_valid(b),
			"drop pays the boss bonus (+%d)" % died_pts[0])
	await drop(a)


func _test_miniboss_contact() -> void:
	print("[hull contact kills; buried is untouchable]")
	var a := make_arena("t_btouch")
	var b := spawn_boss(a, Vector2(460, 414))
	var p := spawn_player(a, Vector2(400, 456))
	b.target = p
	await wait_frames(8)
	var guard := 0
	while not p.dead and guard < 30:
		await physics_frame
		guard += 1
	expect(p.dead and p.lives == Feel.PLAYER_LIVES - 1, "touching the hull kills (lives %d)" % p.lives)
	await drop(a)

	var b2 := make_arena("t_btouch_burrow")
	var b3 := spawn_boss(b2, Vector2(460, 414))
	var p2 := spawn_player(b2, Vector2(700, 456))
	await wait_frames(6)
	Input.action_press("move_down")
	await wait_frames(ceili(Feel.BURROW_ENTER_TIME * 60.0) + 4)
	expect(p2.burrow.burrowing, "hero buried for the hull test")
	p2.global_position = b3.global_position + Vector2(-60, 42)
	await wait_frames(10)
	expect(not p2.dead and p2.lives == Feel.PLAYER_LIVES and not p2.hit(),
			"buried hero sits under the hull unharmed (lives %d)" % p2.lives)
	Input.action_release("move_down")
	await drop(b2)


func _test_boss_signature_immunity() -> void:
	print("[signature systems pass the robot by]")
	var a := make_arena("t_bsig")
	var b := spawn_boss(a, Vector2(700, 414))
	var p := spawn_player(a, Vector2(200, 456))
	await wait_frames(6)
	p.try_jump()
	for i in 10:
		await physics_frame
	Input.action_press("move_down")
	var grabbed: EnemyAgent = p.ride.try_mount()
	Input.action_release("move_down")
	expect(grabbed == null and not p.ride.active, "Down+Jump above the only enemy (the robot) mounts nothing")
	var hp0: int = b.hp
	p.burrow.enter()
	p.global_position = b.global_position + Vector2(0, 42)
	await wait_frames(20)
	expect(is_instance_valid(b) and b.hp == hp0, "buried drag-down does not touch the robot (hp %d)" % b.hp)
	expect(b.phase == 1, "robot unbothered, still phase 1")
	await drop(a)


func _test_wave_flow_c() -> void:
	print("[beat sheet: wave1 x6 -> miniboss_robot -> wave2 agents+heavies x10]")
	var m: Node = (load("res://scenes/main.tscn") as PackedScene).instantiate()
	root.add_child(m)
	var clears := [0]
	m.wave_cleared.connect(func(): clears[0] += 1)
	var f := 0
	while m.spawned_count < Feel.WAVE1_AGENT_COUNT and f < 600:
		await physics_frame
		f += 1
	expect(m.spawned_count == Feel.WAVE1_AGENT_COUNT, "wave 1 spawned %d agents" % m.spawned_count)
	var roster: Array = m.wave_enemies.duplicate()
	for e in roster:
		if is_instance_valid(e):
			e.take_hit(Feel.BULLET_DAMAGE)
	await wait_frames(3)
	expect(clears[0] == 1, "wave 1 clear emitted once")
	f = 0
	while m.boss == null and f < ceili(Feel.MINIBOSS_DELAY * 60.0) + 60:
		await physics_frame
		f += 1
	expect(m.boss != null and is_instance_valid(m.boss), "miniboss takes the stage between the waves (%d frames)" % f)
	expect(m.boss.phase == 1, "robot arrives in phase 1")
	var score0: int = m.score
	expect(score0 == Feel.SCORE_PER_KILL * Feel.WAVE1_AGENT_COUNT, "wave 1 banked %d" % score0)
	m.boss.take_hit(m.boss.hp)
	await wait_frames(3)
	expect(m.boss_done and not is_instance_valid(m.boss), "robot down, boss_done set")
	expect(m.score == score0 + Feel.BOSS_BONUS, "boss bonus banked (score %d)" % m.score)
	expect(m.kills == Feel.WAVE1_AGENT_COUNT + 1, "kill counter includes the robot (%d)" % m.kills)
	f = 0
	while not m.wave2_started and f < ceili(Feel.WAVE2_DELAY * 60.0) + 30:
		await physics_frame
		f += 1
	expect(m.wave2_started and m.wave == 2, "wave 2 rolls after the robot falls (wave=%d)" % m.wave)
	f = 0
	while (m.wave2_spawned < Feel.WAVE2_AGENT_COUNT or m.wave2_heavy_spawned < Feel.WAVE2_HEAVY_COUNT) and f < 600:
		await physics_frame
		f += 1
	expect(m.wave2_spawned == Feel.WAVE2_AGENT_COUNT and m.wave2_heavy_spawned == Feel.WAVE2_HEAVY_COUNT,
			"wave 2 spawned %d agents + %d heavies" % [m.wave2_spawned, m.wave2_heavy_spawned])
	expect(m._extra_platform_built, "wave 2 still brings the extra platform")
	var heavies := 0
	for e in m.wave_enemies:
		if is_instance_valid(e) and e is HeavyAgent:
			heavies += 1
	expect(heavies == Feel.WAVE2_HEAVY_COUNT, "roster holds %d heavies (%d)" % [Feel.WAVE2_HEAVY_COUNT, heavies])
	var roster2: Array = m.wave_enemies.duplicate()
	for e in roster2:
		while is_instance_valid(e) and e.hp > 0:
			e.take_hit(Feel.BULLET_DAMAGE)
	await wait_frames(3)
	expect(clears[0] == 2 and m.wave2_done, "wave 2 clear emitted (clears %d)" % clears[0])
	m.queue_free()
	await process_frame
	await process_frame
