extends SceneTree
## Slice B battery (growing_battery law): ride / burrow / style stub / wave 2.
## Run with:
##   godot --headless --path . --script res://tests/slice_b_tests.gd
## Must pass TWICE consecutively. Exit code 0 = all green.
## Slice A suites must keep passing untouched.

var passed := 0
var failed := 0


func _initialize() -> void:
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
	print("=== STAR VISITOR slice B battery ===")
	await _test_feel_b_constants()
	await _test_style_tracker()
	await _test_mount_trigger()
	await _test_mount_flail()
	await _test_friendly_fire()
	await _test_rider_still_mortal()
	await _test_bite_scares()
	await _test_throw()
	await _test_dismount()
	await _test_burrow_enter_exit()
	await _test_burrow_invulnerable()
	await _test_burrow_drag()
	await _test_suffocation()
	await _test_life_spot()
	await _test_wave2()
	await _test_hud_style()
	print("=== SLICE B BATTERY: %d passed, %d failed ===" % [passed, failed])
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


func spawn_agent(a: Node2D, pos: Vector2) -> EnemyAgent:
	var e: EnemyAgent = (load("res://scenes/enemy.tscn") as PackedScene).instantiate()
	e.position = pos
	a.add_child(e)
	return e


func enemy_bullet(a: Node2D, pos: Vector2, dir: Vector2, spd := 120.0) -> Bullet:
	var b := Bullet.new()
	a.add_child(b)
	b.launch(pos, dir, spd, Feel.ENEMY_BULLET_DAMAGE, false, 0)
	return b


func mount_above(a: Node2D, p: Player, enemy_x := 0.0) -> EnemyAgent:
	var e := spawn_agent(a, Vector2(enemy_x, 457))
	e.target = null
	if absf(p.global_position.x - enemy_x) > Feel.RIDE_GRAB_RADIUS_X:
		p.global_position.x = enemy_x  # rider starts above the mount
	await wait_frames(5)
	p.try_jump()
	await wait_frames(10)
	expect(not p.is_on_floor(), "player airborne above the enemy")
	Input.action_press("move_down")
	p.try_jump()
	await wait_frames(3)
	Input.action_release("move_down")
	if not p.ride.active:
		print("  WARN  mount_above failed to mount (enemy_x=%.0f)" % enemy_x)
	return e


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


# -- tests --

func _test_feel_b_constants() -> void:
	print("[feel milestone-B constants]")
	expect(Feel.RIDE_FLAIL_SPEED > 0.0 and Feel.BITE_RADIUS > 0.0 and Feel.THROW_SPEED > 0.0
			and Feel.BURROW_ENTER_TIME > 0.0 and Feel.BURROW_DRAG_RADIUS > 0.0,
			"ride/burrow tuning constants defined")
	expect(Feel.STYLE_RIDE == 150 and Feel.STYLE_THROW_HIT == 200 and Feel.STYLE_BURROW_KILL == 250
			and Feel.STYLE_PER_LIFE == 5000 and Feel.WAVE2_AGENT_COUNT == 6 and Feel.WAVE2_DELAY > 0.0
			and not Feel.LIFE_SPOT_RECT.size == Vector2.ZERO,
			"style/wave2/life-spot constants per spec")


func _test_style_tracker() -> void:
	print("[style tracker]")
	var s := StyleTracker.new()
	var lives := [0]
	s.life_granted.connect(func(_t: int): lives[0] += 1)
	s.add("ride")
	s.add("throw_hit")
	s.add("burrow_kill")
	expect(s.points == Feel.STYLE_RIDE + Feel.STYLE_THROW_HIT + Feel.STYLE_BURROW_KILL,
			"ride+throw+burrow accumulate (%d)" % s.points)
	s.add("nope")
	expect(s.points == 600, "unknown kind adds nothing (%d)" % s.points)
	expect(lives[0] == 0 and s.lives_granted == 0, "no life below STYLE_PER_LIFE")
	for i in 30:
		s.add("ride")  # 600 + 30*150 = 5100 -> one crossing
	expect(lives[0] == 1 and s.lives_granted == 1, "crossing %d grants exactly +1 life" % Feel.STYLE_PER_LIFE)
	for i in 31:
		s.add("burrow_kill")  # 5100 + 31*250 = 12850 -> second crossing at 10000
	expect(lives[0] == 2 and s.lives_granted == 2, "every further %d grants another life (%d)" % [Feel.STYLE_PER_LIFE, s.lives_granted])


func _test_mount_trigger() -> void:
	print("[mount trigger: Down+Jump above an enemy]")
	var a := make_arena("t_mount")
	var p := spawn_player(a)
	p.style = StyleTracker.new()
	var e := spawn_agent(a, Vector2(0, 457))
	e.target = null
	await wait_frames(5)
	p.try_jump()
	await wait_frames(8)
	p.try_jump()  # no Down held: no mount, no mid-air jump
	await wait_frames(3)
	expect(not p.ride.active, "Jump alone above the enemy does not mount")
	expect(e.mounted == false, "enemy unmounted after plain Jump")
	Input.action_press("move_down")
	p.try_jump()
	await wait_frames(3)
	Input.action_release("move_down")
	expect(p.ride.active and p.ride.mount == e and e.mounted and e.rider == p,
			"Down+Jump above the enemy mounts it")
	expect(p.style.points == Feel.STYLE_RIDE, "riding banks +%d style (%d)" % [Feel.STYLE_RIDE, p.style.points])
	await drop(a)

	var b := make_arena("t_mount_none")
	var p2 := spawn_player(b)
	var e2 := spawn_agent(b, Vector2(400, 457))
	e2.target = null
	await wait_frames(5)
	p2.try_jump()
	await wait_frames(8)
	Input.action_press("move_down")
	p2.try_jump()
	await wait_frames(3)
	Input.action_release("move_down")
	expect(not p2.ride.active and not e2.mounted, "Down+Jump with no enemy below does not mount")
	await drop(b)


func _test_mount_flail() -> void:
	print("[mounted flail]")
	var a := make_arena("t_flail")
	var p := spawn_player(a)
	var e := await mount_above(a, p)
	var x0 := e.global_position.x
	var min_x := x0
	var max_x := x0
	var anchor_ok := true
	for i in 150:
		await physics_frame
		if not p.ride.active:
			break
		min_x = minf(min_x, e.global_position.x)
		max_x = maxf(max_x, e.global_position.x)
		if absf(p.global_position.y - (e.global_position.y - Feel.ENEMY_SIZE.y * 0.5 - Feel.PLAYER_SIZE.y * 0.5)) > 2.0:
			anchor_ok = false
	expect(max_x - min_x > 20.0, "mount flails around (%.0f px swept)" % (max_x - min_x))
	expect(min_x >= Feel.ROOM_MIN_X - 2.0 and max_x <= Feel.ROOM_MAX_X + 2.0,
			"flail stays in room bounds (%.0f..%.0f)" % [min_x, max_x])
	expect(anchor_ok, "rider stays glued to the mount's back")
	await drop(a)


func _test_friendly_fire() -> void:
	print("[friendly fire soaks the mount]")
	var a := make_arena("t_ffire")
	var p := spawn_player(a)
	var e := await mount_above(a, p)
	var lives0: int = p.lives
	var b := enemy_bullet(a, e.global_position + Vector2(-8, 0), Vector2(1, 0))
	var mount_died := false
	for i in 10:
		await physics_frame
		if not is_instance_valid(e) or e.hp <= 0:
			mount_died = true
			break
	expect(mount_died and not p.ride.active, "enemy bullet kills the mount under the rider")
	expect(p.lives == lives0, "rider unharmed by the mount's faction bullet (%d)" % p.lives)
	expect(is_instance_valid(b) and not b.active, "bullet consumed by the mount")
	await drop(a)


func _test_rider_still_mortal() -> void:
	print("[rider hit = dismount + normal death rules]")
	var a := make_arena("t_rider")
	var p := spawn_player(a, Vector2(300, 456))
	var e := await mount_above(a, p, 300.0)
	var lives0: int = p.lives
	var b := enemy_bullet(a, p.global_position, Vector2(1, 0))
	await wait_frames(6)
	expect(p.dead and p.lives == lives0 - 1, "a bullet that reaches the rider kills them (%d -> %d)" % [lives0, p.lives])
	expect(not p.ride.active and not e.mounted, "death dismounts the rider")
	await drop(a)


func _test_bite_scares() -> void:
	print("[bite scares enemies in radius]")
	var a := make_arena("t_bite")
	var p := spawn_player(a)
	var e := await mount_above(a, p, 500.0)
	var near1 := spawn_agent(a, Vector2(380, 457))
	var near2 := spawn_agent(a, Vector2(620, 457))
	var far := spawn_agent(a, Vector2(80, 457))
	for other in [near1, near2, far]:
		other.target = null
	await wait_frames(3)
	p.ride.bite()
	expect(near1.state == EnemyAgent.State.FLEEING and near2.state == EnemyAgent.State.FLEEING,
			"enemies within BITE_RADIUS flee")
	expect(far.state != EnemyAgent.State.FLEEING, "enemy outside the radius does not flee")
	var x1 := near1.global_position.x
	await wait_frames(40)
	expect(signf(near1.global_position.x - x1) == signf(x1 - e.global_position.x)
			and absf(near1.global_position.x - x1) > 30.0,
			"fleeing enemies run away from the mount (%.0f px)" % absf(near1.global_position.x - x1))
	await drop(a)


func _test_throw() -> void:
	print("[flip-and-throw]")
	var a := make_arena("t_throw")
	var p := spawn_player(a, Vector2(500, 456))
	p.style = StyleTracker.new()
	var e := await mount_above(a, p, 500.0)
	var victim := spawn_agent(a, Vector2(680, 457))
	victim.target = null
	var victim_got := [-1]
	victim.died.connect(func(pts: int): victim_got[0] = pts)
	await wait_frames(3)
	Input.action_press("move_right")
	Input.action_press("jump")
	var thrown := false
	for i in 4:
		await physics_frame
		if is_instance_valid(e) and not p.ride.active and e.state == EnemyAgent.State.THROWN:
			thrown = true
			break
	Input.action_release("jump")
	Input.action_release("move_right")
	expect(thrown, "jump + direction flips the mount into a projectile")
	var victim_died := false
	for i in 60:
		await physics_frame
		if victim_got[0] == Feel.SCORE_PER_KILL:
			victim_died = true
			break
	expect(victim_died, "thrown mount damages the enemy it hits (died +%d)" % victim_got[0])
	expect(p.style.points >= Feel.STYLE_RIDE + Feel.STYLE_THROW_HIT,
			"throwing-hit banks +%d style (%d)" % [Feel.STYLE_THROW_HIT, p.style.points])
	var guard := 0
	while is_instance_valid(e) and e.hp > 0 and guard < 180:
		await physics_frame
		guard += 1
	expect(not is_instance_valid(e) or e.hp <= 0, "thrown mount expires on the wall (%d frames)" % guard)
	await drop(a)


func _test_dismount() -> void:
	print("[dismount on directionless jump]")
	var a := make_arena("t_dismount")
	var p := spawn_player(a)
	var e := await mount_above(a, p)
	Input.action_press("jump")
	await wait_frames(2)
	Input.action_release("jump")
	expect(not p.ride.active and not e.mounted and e.state != EnemyAgent.State.THROWN,
			"jump with no direction dismounts (mount unharmed)")
	expect(p.velocity.y < 0.0, "rider pops upward off the mount (vy %.0f)" % p.velocity.y)
	await drop(a)


func _test_burrow_enter_exit() -> void:
	print("[burrow enter / exit]")
	var a := make_arena("t_burrow")
	var p := spawn_player(a)
	await wait_frames(5)
	Input.action_press("move_down")
	await wait_frames(6)
	expect(not p.burrow.burrowing and p.ducking, "Down first ducks (no instant dig)")
	await wait_frames(ceili(Feel.BURROW_ENTER_TIME * 60.0) + 4)
	expect(p.burrow.burrowing, "holding Down on the ground burrows after BURROW_ENTER_TIME")
	expect(p.collision_layer == 0, "buried hero drops out of the collision layers")
	Input.action_release("move_down")
	await wait_frames(3)
	expect(not p.burrow.burrowing, "releasing Down surfaces")
	expect(p.collision_layer == 1 << 1, "surfaced hero is solid again")
	await drop(a)


func _test_burrow_invulnerable() -> void:
	print("[buried = bullets pass, no damage]")
	var a := make_arena("t_binvuln")
	var p := spawn_player(a, Vector2(300, 456))
	await wait_frames(5)
	Input.action_press("move_down")
	await wait_frames(ceili(Feel.BURROW_ENTER_TIME * 60.0) + 4)
	expect(p.burrow.burrowing, "buried for the invulnerability check")
	var b := enemy_bullet(a, p.global_position + Vector2(-40, 0), Vector2(1, 0))
	var lives0: int = p.lives
	await wait_frames(60)
	expect(p.lives == lives0 and not p.dead and not p.hit(), "buried hero takes no damage (lives %d)" % p.lives)
	expect(is_instance_valid(b) and b.active and b.global_position.x > p.global_position.x,
			"enemy bullet passed through the buried hero")
	Input.action_release("move_down")
	await drop(a)


func _test_burrow_drag() -> void:
	print("[buried drag-down kill]")
	var a := make_arena("t_drag")
	var p := spawn_player(a, Vector2(500, 456))
	p.style = StyleTracker.new()
	var e := spawn_agent(a, Vector2(500, 457))
	e.target = null
	await wait_frames(5)
	expect(e.is_on_floor(), "victim stands on the ground above the soon-buried hero")
	Input.action_press("move_down")
	await wait_frames(ceili(Feel.BURROW_ENTER_TIME * 60.0) + 6)
	expect(not is_instance_valid(e) or e.hp <= 0, "enemy standing on the buried hero is dragged down (instant kill)")
	expect(p.style.points == Feel.STYLE_BURROW_KILL, "burrow-kill banks +%d style (%d)" % [Feel.STYLE_BURROW_KILL, p.style.points])
	Input.action_release("move_down")
	await drop(a)


func _test_suffocation() -> void:
	print("[burrow suffocation: warn at %.1fs, death at %.1fs]" % [Feel.BURROW_WARN_AT, Feel.BURROW_SUFFOCATE_TIME])
	var a := make_arena("t_suffocate")
	var p := spawn_player(a)
	var warns := [0]
	p.burrow.warned.connect(func(): warns[0] += 1)
	await wait_frames(5)
	var lives0: int = p.lives
	Input.action_press("move_down")
	await wait_frames(ceili(Feel.BURROW_ENTER_TIME * 60.0) + 2)
	expect(p.burrow.burrowing, "buried for the suffocation clock")
	var f := 0
	while f < ceili(Feel.BURROW_WARN_AT * 60.0) - 8 and p.burrow.burrowing:
		await physics_frame
		f += 1
	expect(p.burrow.burrowing and not p.burrow.warning_active,
			"no warning before BURROW_WARN_AT (%.2fs in)" % [f / 60.0])
	while f < ceili(Feel.BURROW_WARN_AT * 60.0) + 8 and p.burrow.burrowing:
		await physics_frame
		f += 1
	expect(p.burrow.warning_active and warns[0] == 1, "warning flashes through the last second")
	while p.burrow.burrowing and f < ceili(Feel.BURROW_SUFFOCATE_TIME * 60.0) + 20:
		await physics_frame
		f += 1
	expect(p.dead and p.lives == lives0 - 1, "suffocation kills at %.1fs (%.2fs, lives %d)" % [Feel.BURROW_SUFFOCATE_TIME, f / 60.0, p.lives])
	expect(not p.burrow.burrowing, "suffocation surfaces the body first")
	Input.action_release("move_down")
	await drop(a)


func _test_life_spot() -> void:
	print("[buried extra-life spot]")
	var a := make_arena("t_spot")
	var p := spawn_player(a, Vector2(Feel.LIFE_SPOT_RECT.get_center().x, 456))
	var spot := Burrow.LifeSpot.new()
	spot.position = Feel.LIFE_SPOT_RECT.get_center()
	a.add_child(spot)
	p.life_spot = spot
	await wait_frames(5)
	var lives0: int = p.lives
	Input.action_press("move_down")
	await wait_frames(ceili(Feel.BURROW_ENTER_TIME * 60.0) + 4)
	expect(p.lives == lives0 + 1, "burrowing onto the spot uncovers +1 life (%d -> %d)" % [lives0, p.lives])
	expect(not is_instance_valid(spot) or spot.taken, "spot consumed on first find")
	Input.action_release("move_down")
	await wait_frames(3)
	var lives1: int = p.lives
	Input.action_press("move_down")
	await wait_frames(ceili(Feel.BURROW_ENTER_TIME * 60.0) + 6)
	expect(p.lives == lives1, "no second life from the same spot (%d)" % p.lives)
	Input.action_release("move_down")
	await drop(a)


func _test_wave2() -> void:
	print("[wave 2: 6 more agents + extra platform]")
	var m: Node = (load("res://scenes/main.tscn") as PackedScene).instantiate()
	root.add_child(m)
	var clears := [0]
	m.wave_cleared.connect(func(): clears[0] += 1)
	var f := 0
	while m.spawned_count < Feel.WAVE1_AGENT_COUNT and f < 600:
		await physics_frame
		f += 1
	var roster: Array = m.wave_enemies.duplicate()
	for e in roster:
		if is_instance_valid(e):
			e.take_hit(Feel.BULLET_DAMAGE)
	await wait_frames(3)
	expect(clears[0] == 1, "wave 1 clear emitted once")
	# slice C: the miniboss gates wave 2 — drop the robot, then the wave rolls
	f = 0
	while m.boss == null and f < ceili(Feel.MINIBOSS_DELAY * 60.0) + 60:
		await physics_frame
		f += 1
	expect(m.boss != null, "miniboss gates wave 2 (spawned in %d frames)" % f)
	if m.boss != null and is_instance_valid(m.boss):
		m.boss.take_hit(m.boss.hp)
	await wait_frames(3)
	f = 0
	while not m.wave2_started and f < ceili(Feel.WAVE2_DELAY * 60.0) + 30:
		await physics_frame
		f += 1
	expect(m.wave2_started and m.wave == 2, "wave 2 starts after the delay (wave=%d)" % m.wave)
	f = 0
	while m.wave2_spawned < Feel.WAVE2_AGENT_COUNT and f < 600:
		await physics_frame
		f += 1
	expect(m.wave2_spawned == Feel.WAVE2_AGENT_COUNT, "wave 2 spawned %d agents (%d)" % [Feel.WAVE2_AGENT_COUNT, m.wave2_spawned])
	expect(m._extra_platform_built, "wave 2 brings the extra platform")
	var roster2: Array = m.wave_enemies.duplicate()
	for e in roster2:
		while is_instance_valid(e) and e.hp > 0:
			e.take_hit(Feel.BULLET_DAMAGE)  # heavies take 3 rounds to drop
	await wait_frames(3)
	expect(clears[0] == 2 and m.wave2_done, "wave 2 clear emitted (clears %d)" % clears[0])
	m.queue_free()
	await process_frame
	await process_frame


func _test_hud_style() -> void:
	print("[HUD style counter]")
	var m: Node = (load("res://scenes/main.tscn") as PackedScene).instantiate()
	root.add_child(m)
	await wait_frames(3)
	m.style.add("ride")
	await wait_frames(2)
	var lbl: Label = m._hud_style
	expect(lbl != null and lbl.text == "STYLE %d" % Feel.STYLE_RIDE,
			"HUD shows the style counter (%s)" % (lbl.text if lbl != null else "<none>"))
	m.queue_free()
	await process_frame
	await process_frame
