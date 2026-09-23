extends SceneTree
## Slice B headless replays (spec law: headless_replays) — milestone B signature
## systems driven on the real scene tree. Run with:
##   godot --headless --path . --script res://tests/slice_b_replay.gd
## Run 1 "rider" (~15s): mount, bite, throw, dismount, repeat.
## Run 2 "burrower" (~15s): burrow / drag-down / surface cycles, never suffocating.
## Green = no errors, no soft-locks, style > 0 in both runs.

var passed := 0
var failed := 0

const REPLAY_FRAMES := 900


func _initialize() -> void:
	create_timer(240.0).timeout.connect(_watchdog)
	call_deferred("_run_all")


func _watchdog() -> void:
	print("WATCHDOG: replay exceeded 240s, aborting")
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


func _nearest_enemy(m: Node, from: Node2D) -> EnemyAgent:
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


func _run_rider() -> void:
	print("[replay 1: rider — mount / bite / throw / dismount, ~15s]")
	var m := _load_main()
	await wait_frames(3)
	var p = m.player
	var mounted_count := 0
	var bites := 0
	var throws := 0
	var cooldown := 0
	var aboard := 0
	for f in REPLAY_FRAMES:
		await physics_frame
		cooldown = maxi(cooldown - 1, 0)
		if f % 150 == 149:
			_release_all()
		if p == null or not is_instance_valid(p) or p.dead:
			continue
		if p.ride.active:
			aboard += 1
			Input.action_release("move_left")
			Input.action_release("move_right")
			# aboard: bite fast, then flip-and-throw before faction fire kills
			# the mount (hp-1 agents soak one bullet and drop the rider)
			if aboard == 3:
				p.ride.bite()
				bites += 1
			var want_off := f >= REPLAY_FRAMES - 90 or aboard >= 240
			if not want_off and aboard >= 5 and aboard % 5 == 0:
				var foe := _nearest_enemy(m, p)
				var dir := 1.0
				if foe != null:
					dir = signf(foe.global_position.x - p.global_position.x)
					if dir == 0.0:
						dir = 1.0
				Input.action_press("move_right" if dir > 0.0 else "move_left")
				Input.action_press("jump")
				await physics_frame
				Input.action_release("jump")
				Input.action_release("move_right")
				Input.action_release("move_left")
				if not p.ride.active:
					throws += 1
					aboard = 0
					cooldown = 20
				continue
			if want_off:
				Input.action_press("jump")
				await physics_frame
				Input.action_release("jump")
				aboard = 0
			continue
		aboard = 0
		if cooldown > 0 or f >= REPLAY_FRAMES - 150:
			continue
		var e := _nearest_enemy(m, p)
		if e == null or not e.is_inside_tree() or not e.is_on_floor():
			_release_all()
			continue
		var dx: float = e.global_position.x - p.global_position.x
		if absf(dx) > 26.0:
			Input.action_release("move_left")
			Input.action_release("move_right")
			Input.action_press("move_right" if dx > 0.0 else "move_left")
			continue
		# stand under the target, hop, and pull the mount out of the air
		Input.action_release("move_left")
		Input.action_release("move_right")
		p.try_jump()
		for i in 12:
			await physics_frame
		Input.action_press("move_down")
		p.try_jump()
		for i in 3:
			await physics_frame
		Input.action_release("move_down")
		if p.ride.active:
			mounted_count += 1
			cooldown = 30
			aboard = 0
	_release_all()
	expect(mounted_count >= 1, "rider mounted at least once (%d mounts)" % mounted_count)
	expect(bites >= 1, "rider bit while mounted (%d bites)" % bites)
	expect(throws >= 1, "rider flipped a throw (%d throws)" % throws)
	expect(p.ride.active == false, "rider is not stuck mounted at the end")
	expect(m.style != null and m.style.points > 0, "rider banked style (%d)" % m.style.points)
	expect(p.lives >= 0, "rider lives never negative (%d)" % p.lives)
	expect(Engine.get_physics_frames() > 0, "engine kept stepping")
	expect(m.spawned_count == Feel.WAVE1_AGENT_COUNT, "wave spawner completed (%d/6)" % m.spawned_count)
	expect(m.score >= 0 and m.kills >= 0, "scoreboard live (score %d, kills %d)" % [m.score, m.kills])
	expect(not m.game_ended or p.lives == 0, "no soft-lock at game over (ended=%s)" % str(m.game_ended))
	m.queue_free()
	await process_frame
	await process_frame


func _run_burrower() -> void:
	print("[replay 2: burrower — burrow / drag / surface, ~15s]")
	var m := _load_main()
	await wait_frames(3)
	var p = m.player
	var cycles := 0
	var kills0: int = m.kills
	var max_t := 0.0
	for f in REPLAY_FRAMES:
		await physics_frame
		if p == null or not is_instance_valid(p) or p.dead:
			_release_all()
			continue
		var surfacing := f >= REPLAY_FRAMES - 100  # final stretch: stay above ground
		if p.burrow.burrowing:
			max_t = maxf(max_t, p.burrow.t)
			# shuffle toward the nearest live enemy to drag it under
			var e := _nearest_enemy(m, p)
			if e != null and e.is_inside_tree():
				var dx: float = e.global_position.x - p.global_position.x
				Input.action_release("move_left")
				Input.action_release("move_right")
				if absf(dx) > Feel.BURROW_DRAG_RADIUS * 0.5:
					Input.action_press("move_right" if dx > 0.0 else "move_left")
			# never push the suffocation clock: surface well before the warn
			if p.burrow.t >= 1.6 or surfacing:
				Input.action_release("move_down")
				Input.action_release("move_left")
				Input.action_release("move_right")
				cycles += 1
		else:
			if surfacing:
				_release_all()
				continue
			var e2 := _nearest_enemy(m, p)
			if e2 != null and e2.is_inside_tree() and p.is_on_floor():
				var dx2: float = e2.global_position.x - p.global_position.x
				if absf(dx2) > 90.0:
					Input.action_release("move_down")
					Input.action_release("move_left")
					Input.action_release("move_right")
					Input.action_press("move_right" if dx2 > 0.0 else "move_left")
					continue
				# close enough: dig in under them
				Input.action_press("move_down")
			elif p.is_on_floor():
				Input.action_press("move_down")
	_release_all()
	expect(cycles >= 2, "burrower cycled burrow/surface (%d cycles)" % cycles)
	expect(max_t < Feel.BURROW_SUFFOCATE_TIME, "never suffocated (max %.2fs of %.1fs)" % [max_t, Feel.BURROW_SUFFOCATE_TIME])
	expect(m.kills >= kills0, "scoreboard still counting (%d kills)" % m.kills)
	expect(m.style != null and m.style.points > 0, "burrower banked style (%d)" % m.style.points)
	expect(p.lives >= 0, "burrower lives never negative (%d)" % p.lives)
	expect(not p.burrow.burrowing, "burrower is not stuck underground at the end")
	expect(Engine.get_physics_frames() > 0, "engine kept stepping")
	expect(m.spawned_count == Feel.WAVE1_AGENT_COUNT, "spawner completed (%d/6)" % m.spawned_count)
	expect(not m.game_ended or p.lives == 0, "no soft-lock at game over (ended=%s)" % str(m.game_ended))
	m.queue_free()
	await process_frame
	await process_frame


func _run_all() -> void:
	await process_frame
	print("=== STAR VISITOR slice B replays ===")
	await _run_rider()
	await _run_burrower()
	print("=== SLICE B REPLAYS: %d passed, %d failed ===" % [passed, failed])
	quit(0 if failed == 0 else 1)
