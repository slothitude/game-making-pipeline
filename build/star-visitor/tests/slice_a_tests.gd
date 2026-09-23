extends SceneTree
## Slice A battery (growing_battery law). Run with:
##   godot --headless --path . --script res://tests/slice_a_tests.gd
## Must pass TWICE consecutively. Exit code 0 = all green.

var passed := 0
var failed := 0


func _initialize() -> void:
	# watchdog: if any await hangs, bail with a distinctive code instead of freezing
	create_timer(180.0).timeout.connect(_watchdog)
	call_deferred("_run_all")


func _watchdog() -> void:
	print("WATCHDOG: battery exceeded 180s, aborting")
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
	print("=== STAR VISITOR slice A battery ===")
	await _test_feel_constants()
	await _test_player_run()
	await _test_player_jump()
	await _test_player_duck()
	await _test_player_aim()
	await _test_bullet_cap()
	await _test_bullet_wall()
	await _test_charge_shot()
	await _test_bullet_kills_agent()
	await _test_enemy_cycle()
	await _test_player_death_respawn()
	await _test_wave_clears()
	await _test_game_over()
	print("=== SLICE A BATTERY: %d passed, %d failed ===" % [passed, failed])
	quit(0 if failed == 0 else 1)


# -- fixtures --

func make_arena(tag: String) -> Node2D:
	var a := Node2D.new()
	a.name = tag
	root.add_child(a)
	add_block(a, Rect2(-480, 100, 1920, 80))
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


func spawn_player(a: Node2D, pos: Vector2 = Vector2(0, 76)) -> Player:
	var p: Player = (load("res://scenes/player.tscn") as PackedScene).instantiate()
	p.position = pos
	a.add_child(p)
	return p


func spawn_agent(a: Node2D, pos: Vector2) -> EnemyAgent:
	var e: EnemyAgent = (load("res://scenes/enemy.tscn") as PackedScene).instantiate()
	e.position = pos
	a.add_child(e)
	return e


func drop(n: Node) -> void:
	if is_instance_valid(n):
		n.queue_free()
	await process_frame
	await process_frame


func count_bullets(a: Node2D, player_owned: bool) -> Array:
	var out: Array = []
	for c in a.get_children():
		if c is Bullet and c.from_player == player_owned and c.active:
			out.append(c)
	return out


# -- tests --

func _test_feel_constants() -> void:
	print("[feel constants]")
	expect(Feel.RUN_SPEED == 260.0, "RUN_SPEED == 260.0")
	expect(Feel.JUMP_VELOCITY == -420.0, "JUMP_VELOCITY == -420.0")
	expect(Feel.GRAVITY == 1100.0, "GRAVITY == 1100.0")
	expect(Feel.BULLET_CAP == 3, "BULLET_CAP == 3")
	expect(Feel.BULLET_SPEED == 620.0, "BULLET_SPEED == 620.0")
	expect(Feel.CHARGE_TIME == 0.8, "CHARGE_TIME == 0.8")
	expect(Feel.ENEMY_SPEED == 90.0, "ENEMY_SPEED == 90.0")
	expect(Feel.ENEMY_HP == 1, "ENEMY_HP == 1")
	expect(Feel.ENEMY_BULLET_SPEED > 0.0, "ENEMY_BULLET_SPEED defined (%.0f)" % Feel.ENEMY_BULLET_SPEED)
	expect(Feel.PLAYER_LIVES == 10, "PLAYER_LIVES == 10")
	expect(Feel.RESPAWN_IFRAMES == 1.5, "RESPAWN_IFRAMES == 1.5")
	expect(Feel.SCORE_PER_KILL == 100, "SCORE_PER_KILL == 100")
	expect(Feel.WAVE1_AGENT_COUNT == 6, "WAVE1_AGENT_COUNT == 6")
	expect(Feel.GRENADE_START_COUNT == 3 and Feel.BURROW_SUFFOCATE_TIME == 4.0, "milestone-B consts carried")
	expect(Feel.AIM_FORWARD.is_normalized() and Feel.AIM_FORWARD_UP.is_normalized(), "aim dirs normalized")


func _test_player_run() -> void:
	print("[player run]")
	var a := make_arena("t_run")
	var p := spawn_player(a)
	await wait_frames(5)
	expect(p.is_on_floor(), "player settles on the floor")
	var x0 := p.global_position.x
	Input.action_press("move_right")
	await wait_frames(30)
	Input.action_release("move_right")
	var dx_right := p.global_position.x - x0
	expect(dx_right > 100.0, "runs right (+%.1f px in 0.5s @ %.0f)" % [dx_right, Feel.RUN_SPEED])
	var x1 := p.global_position.x
	Input.action_press("move_left")
	await wait_frames(30)
	Input.action_release("move_left")
	var dx_left := p.global_position.x - x1
	expect(dx_left < -100.0, "runs left (%.1f px)" % dx_left)
	await drop(a)


func _test_player_jump() -> void:
	print("[player jump]")
	var a := make_arena("t_jump")
	var p := spawn_player(a)
	await wait_frames(5)
	var y_floor := p.global_position.y
	p.try_jump()
	await wait_frames(2)
	expect(p.velocity.y < -300.0, "jump velocity applied (vy %.0f)" % p.velocity.y)
	var min_y := p.global_position.y
	for i in 50:
		await physics_frame
		min_y = minf(min_y, p.global_position.y)
		if i == 30:
			expect(p.velocity.y > 0.0, "gravity pulls back down (vy %.0f at f30)" % p.velocity.y)
	expect(min_y < y_floor - 40.0, "left the ground (rose %.0f px)" % (y_floor - min_y))
	await wait_frames(15)
	expect(p.is_on_floor(), "landed again")
	expect(absf(p.global_position.y - y_floor) < 2.0, "returned to floor height")
	await drop(a)


func _test_player_duck() -> void:
	print("[player duck]")
	var a := make_arena("t_duck")
	var p := spawn_player(a)
	await wait_frames(5)
	Input.action_press("move_down")
	Input.action_press("move_right")
	var x0 := p.global_position.x
	await wait_frames(6)
	expect(p.ducking, "ducking while Down held on ground")
	expect(is_equal_approx(p.collider_height(), Feel.PLAYER_DUCK_HEIGHT), "collider shrinks to duck height")
	await wait_frames(20)
	expect(absf(p.global_position.x - x0) < 1.0, "no horizontal movement while ducking")
	Input.action_release("move_down")
	Input.action_release("move_right")
	await wait_frames(6)
	expect(not p.ducking, "stands back up")
	expect(is_equal_approx(p.collider_height(), Feel.PLAYER_SIZE.y), "collider restored")
	await drop(a)


func _test_player_aim() -> void:
	print("[player aim]")
	var a := make_arena("t_aim")
	var p := spawn_player(a)
	await wait_frames(5)
	expect(p.aim_dir() == Feel.AIM_FORWARD, "grounded default = forward")
	Input.action_press("move_up")
	expect(p.aim_dir() == Feel.AIM_UP, "grounded Up = straight up")
	Input.action_press("move_right")
	expect(p.aim_dir() == Feel.AIM_FORWARD_UP, "grounded Up+dir = forward-up")
	Input.action_release("move_up")
	Input.action_release("move_right")
	p.try_jump()
	await wait_frames(8)
	expect(not p.is_on_floor(), "airborne for the down-aim check")
	Input.action_press("move_down")
	expect(p.aim_dir() == Feel.AIM_DOWN, "airborne Down = down (4th airborne direction)")
	Input.action_release("move_down")
	await wait_frames(50)
	expect(p.is_on_floor(), "landed after aim checks")
	await drop(a)


func _test_bullet_cap() -> void:
	print("[bullet cap]")
	var a := make_arena("t_cap")
	var p := spawn_player(a)
	await wait_frames(5)
	var results: Array = []
	for i in 5:
		results.append(p.weapon.on_fire_pressed(Vector2(1, 0)))
	expect(bool(results[0]) and bool(results[1]) and bool(results[2]), "first 3 presses fire")
	expect(not bool(results[3]) and not bool(results[4]), "4th and 5th presses do nothing")
	expect(p.weapon.live_count() == Feel.BULLET_CAP, "exactly BULLET_CAP live bullets (%d)" % p.weapon.live_count())
	await wait_frames(6)
	expect(p.weapon.live_count() == Feel.BULLET_CAP, "cap still holds a few frames later (%d)" % p.weapon.live_count())
	await drop(a)


func _test_bullet_wall() -> void:
	print("[bullet travel + wall despawn]")
	var a := make_arena("t_wall")
	add_block(a, Rect2(300, -40, 40, 220))
	await wait_frames(2)
	var b := Bullet.new()
	a.add_child(b)
	b.launch(Vector2(200, 76), Vector2(1, 0), Feel.BULLET_SPEED, Feel.BULLET_DAMAGE, true, 0)
	expect(b.active, "bullet live at launch")
	await wait_frames(30)
	expect(is_instance_valid(b) and not b.active, "bullet despawned on the wall")
	if is_instance_valid(b):
		expect(b.global_position.x > 200.0, "traveled right (%.0f px)" % (b.global_position.x - 200.0))
		expect(b.global_position.x < 340.0, "stopped at the wall face (x %.0f)" % b.global_position.x)
	await drop(a)


func _test_charge_shot() -> void:
	print("[charge shot]")
	var a := make_arena("t_charge")
	add_block(a, Rect2(300, -40, 40, 220))
	var p := spawn_player(a)
	await wait_frames(5)
	p.weapon.on_fire_pressed(Vector2(1, 0))
	await wait_frames(5)
	expect(p.weapon.charge_t > 0.0 and p.weapon.charge_t < Feel.CHARGE_TIME,
			"charge timer runs while held (%.2fs)" % p.weapon.charge_t)
	expect(not p.weapon.on_fire_released(Vector2(1, 0)), "early release fires no big shot")
	await wait_frames(20)
	p.weapon.on_fire_pressed(Vector2(1, 0))
	await wait_frames(ceili(Feel.CHARGE_TIME * 60.0) + 3)
	expect(p.weapon.charge_t >= Feel.CHARGE_TIME, "charge full after CHARGE_TIME (%.2fs)" % p.weapon.charge_t)
	expect(p.weapon.on_fire_released(Vector2(1, 0)), "full release fires the big shot")
	var big: Array = []
	for c in p.weapon.pool():
		if is_instance_valid(c) and c.active and c.damage == Feel.BULLET_DAMAGE * Feel.CHARGE_DAMAGE_MULT:
			big.append(c)
	expect(big.size() >= 1, "big bullet exists (damage x%d)" % Feel.CHARGE_DAMAGE_MULT)
	if big.size() >= 1:
		expect(big[0].pierce_left == Feel.CHARGE_PIERCE, "big bullet pierces %d" % Feel.CHARGE_PIERCE)
	await drop(a)


func _test_bullet_kills_agent() -> void:
	print("[bullet kills agent]")
	var a := make_arena("t_kill")
	var p := spawn_player(a)
	var e := spawn_agent(a, Vector2(220, 76))
	e.target = p
	var got := [-1]
	e.died.connect(func(pts: int): got[0] = pts)
	await wait_frames(5)
	p.weapon.on_fire_pressed(Vector2(1, 0))
	await wait_frames(25)
	expect(got[0] == Feel.SCORE_PER_KILL, "agent died in 1 hit awarding +%d (got %s)" % [Feel.SCORE_PER_KILL, str(got[0])])
	expect(p.weapon.live_count() == 0, "normal bullet consumed (no pierce)")
	await drop(a)


func _test_enemy_cycle() -> void:
	print("[enemy state machine]")
	var a := make_arena("t_cycle")
	var p := spawn_player(a)
	var e := spawn_agent(a, Vector2(240, 76))
	e.target = p
	await wait_frames(5)
	var lives0: int = p.lives
	var saw_advance := false
	var saw_stop := false
	var saw_shoot := false
	var returned := false
	var bullets: Array = []
	for i in 240:
		await physics_frame
		match e.state:
			EnemyAgent.State.ADVANCE:
				if saw_stop and saw_shoot:
					returned = true
				elif not saw_stop:
					saw_advance = true
			EnemyAgent.State.STOP:
				saw_stop = true
			EnemyAgent.State.SHOOT:
				saw_shoot = true
				for c in count_bullets(a, false):
					if not bullets.has(c):
						bullets.append(c)
		if returned:
			break
	expect(saw_advance, "saw ADVANCE")
	expect(saw_stop, "saw STOP")
	expect(saw_shoot, "saw SHOOT")
	expect(returned, "cycle returned to ADVANCE (advance-stop-shoot loop)")
	expect(bullets.size() >= 1, "SHOOT emitted a bullet")
	if bullets.size() >= 1:
		var b: Bullet = bullets[0]
		var to_player := signf(p.global_position.x - e.global_position.x)
		expect(signf(b.dir.x) == to_player, "bullet flies toward the player")
	var guard := 0
	while not p.dead and guard < 240:
		await physics_frame
		guard += 1
	expect(p.lives == lives0 - 1 and p.dead, "agent bullet one-hits the player (%d -> %d)" % [lives0, p.lives])
	await drop(a)


func _test_player_death_respawn() -> void:
	print("[player death / respawn / iframes]")
	var a := make_arena("t_death")
	var p := spawn_player(a)
	await wait_frames(5)
	var died_lives := [-1]
	p.died.connect(func(l: int): died_lives[0] = l)
	expect(p.hit(), "hit lands while vulnerable")
	expect(p.dead, "one-hit death")
	expect(p.lives == Feel.PLAYER_LIVES - 1, "lives decremented to %d" % p.lives)
	expect(died_lives[0] == Feel.PLAYER_LIVES - 1, "died(lives_left) signal fired")
	expect(not p.hit(), "hit while dead is ignored")
	p.respawn(Vector2(0, 76))
	expect(not p.dead, "respawned alive")
	expect(p.invulnerable, "iframes active after respawn")
	expect(not p.hit(), "hit during iframes ignored")
	expect(p.lives == Feel.PLAYER_LIVES - 1, "lives unchanged through iframes")
	await wait_frames(ceili(Feel.RESPAWN_IFRAMES * 60.0) + 6)
	expect(not p.invulnerable, "iframes expire after RESPAWN_IFRAMES")
	expect(p.hit(), "vulnerable again after iframes")
	await drop(a)


func _test_wave_clears() -> void:
	print("[wave 1: agents x6 -> wave_cleared]")
	var m: Node = (load("res://scenes/main.tscn") as PackedScene).instantiate()
	root.add_child(m)
	var cleared := [false]
	m.wave_cleared.connect(func(): cleared[0] = true)
	var f := 0
	while m.spawned_count < Feel.WAVE1_AGENT_COUNT and f < 600:
		await physics_frame
		f += 1
	expect(m.spawned_count == Feel.WAVE1_AGENT_COUNT, "spawner spawned 6 agents (%d in %d frames)" % [m.spawned_count, f])
	expect(m.wave_enemies.size() == Feel.WAVE1_AGENT_COUNT, "wave roster holds 6 (%d)" % m.wave_enemies.size())
	var roster: Array = m.wave_enemies.duplicate()
	for e in roster:
		if is_instance_valid(e):
			e.take_hit(Feel.BULLET_DAMAGE)
	await wait_frames(3)
	expect(cleared[0], "wave_cleared emitted after the roster was wiped")
	expect(m.score == Feel.SCORE_PER_KILL * Feel.WAVE1_AGENT_COUNT, "score 600 (%d)" % m.score)
	expect(m.kills == Feel.WAVE1_AGENT_COUNT, "kills 6 (%d)" % m.kills)
	m.queue_free()
	await process_frame
	await process_frame


func _test_game_over() -> void:
	print("[game over]")
	var m: Node = (load("res://scenes/main.tscn") as PackedScene).instantiate()
	root.add_child(m)
	var over := [false]
	m.game_over.connect(func(): over[0] = true)
	await wait_frames(3)
	m.player.lives = 1
	expect(m.player.hit(), "final hit lands at 1 life")
	await wait_frames(3)
	expect(over[0], "game_over emitted at 0 lives")
	expect(m.game_ended, "game ended")
	expect(m.game_over_layer.visible, "game over overlay shown")
	m.queue_free()
	await process_frame
	await process_frame
