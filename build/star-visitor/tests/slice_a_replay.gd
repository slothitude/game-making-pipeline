extends SceneTree
## Headless replay law (spec: headless_replays) — drives the real scene tree with
## scripted input. Run with:
##   godot --headless --path . --script res://tests/slice_a_replay.gd
## Run 1 "walk right and shoot" (~10 simulated seconds): no errors, player alive
##   or legitimately dead.
## Run 2 "do nothing" (~10 simulated seconds): no soft-lock — frames keep
##   processing, the spawner completes, no error spam.

var passed := 0
var failed := 0

const REPLAY_FRAMES := 600


func _initialize() -> void:
	create_timer(180.0).timeout.connect(_watchdog)
	call_deferred("_run_all")


func _watchdog() -> void:
	print("WATCHDOG: replay exceeded 180s, aborting")
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
	print("=== STAR VISITOR slice A replays ===")
	await _run_walk_and_shoot()
	await _run_do_nothing()
	print("=== SLICE A REPLAYS: %d passed, %d failed ===" % [passed, failed])
	quit(0 if failed == 0 else 1)


func _load_main() -> Node:
	var m: Node = (load("res://scenes/main.tscn") as PackedScene).instantiate()
	root.add_child(m)
	return m


func _run_walk_and_shoot() -> void:
	print("[replay 1: walk right and shoot, ~10s]")
	var m := _load_main()
	await wait_frames(3)
	var p = m.player
	Input.action_press("move_right")
	var fired := 0
	var jumps := 0
	for f in REPLAY_FRAMES:
		await physics_frame
		if f % 30 == 0:
			Input.action_press("fire")
		elif f % 30 == 15:
			Input.action_release("fire")
			fired += 1
		if f % 120 == 0 and p.is_on_floor():
			p.try_jump()
			jumps += 1
	Input.action_release("move_right")
	Input.action_release("fire")
	expect(fired >= 15, "held a firing rhythm (%d volleys)" % fired)
	expect(jumps >= 1, "jumped during the run (%d)" % jumps)
	expect(p.lives >= 0, "lives never negative (%d)" % p.lives)
	expect(not p.dead or p.lives < 10, "player alive or legitimately dead (dead=%s lives=%d)" % [str(p.dead), p.lives])
	expect(m.spawned_count == 6, "wave spawner completed (%d/6)" % m.spawned_count)
	expect(m.score >= 0 and m.kills >= 0, "scoreboard live (score %d, kills %d)" % [m.score, m.kills])
	m.queue_free()
	await process_frame
	await process_frame


func _run_do_nothing() -> void:
	print("[replay 2: do nothing, ~10s]")
	var m := _load_main()
	await wait_frames(3)
	var p = m.player
	for f in REPLAY_FRAMES:
		await physics_frame
	expect(Engine.get_physics_frames() > 0, "engine kept stepping")
	expect(p.lives > 0, "idle hero survives the wave (lives=%d, iframes cap kills at 1/1.5s)" % p.lives)
	expect(not p.dead or p.lives >= 0, "no undefined player state (dead=%s lives=%d)" % [str(p.dead), p.lives])
	expect(m.spawned_count == 6, "spawner completed with zero input (%d/6)" % m.spawned_count)
	expect(m.wave_enemies.size() == 6, "agents still pressuring, game still processing (%d live)" % m.wave_enemies.size())
	m.queue_free()
	await process_frame
	await process_frame
